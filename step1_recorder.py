"""
AirType - Step 1: ホットキー検知 + 録音開始/停止 + 一時ファイル保存
 
設計:
- pynput でグローバルホットキー (Ctrl+Shift+Space) を監視
- 状態マシン: IDLE → RECORDING → IDLE
- sounddevice でマイク入力をキャプチャ (別スレッド)
- 録音データを WAV 形式で tempfile に保存
- メインスレッドは常にホットキー監視を維持する
"""
 
import wave
import tempfile
import threading
import time
from enum import Enum, auto
from pathlib import Path
 
import numpy as np
import sounddevice as sd
from pynput import keyboard
 
 
# ─────────────────────────────────────
# 定数
# ─────────────────────────────────────
SAMPLE_RATE = 16000       # Whisper が期待するサンプルレート (16kHz)
CHANNELS = 1              # モノラル
DTYPE = "int16"           # 16bit PCM
HOTKEY = {keyboard.Key.ctrl_l, keyboard.Key.shift, keyboard.KeyCode(char=" ")}
# ※ OS・キーボードレイアウトにより ctrl_r / shift_r を追加する場合は HOTKEY を拡張
 
 
# ─────────────────────────────────────
# 状態定義
# ─────────────────────────────────────
class State(Enum):
    IDLE = auto()
    RECORDING = auto()
 
 
# ─────────────────────────────────────
# Recorder クラス
# ─────────────────────────────────────
class Recorder:
    """
    sounddevice の InputStream を使って非同期録音を行う。
    start() で録音開始、stop() で停止して WAV ファイルを返す。
    """
 
    def __init__(self, sample_rate: int = SAMPLE_RATE, channels: int = CHANNELS):
        self.sample_rate = sample_rate
        self.channels = channels
        self._frames: list[np.ndarray] = []
        self._stream: sd.InputStream | None = None
        self._lock = threading.Lock()
 
    def _callback(self, indata: np.ndarray, frames: int, time_info, status):
        """sounddevice のコールバック: 録音バッファを蓄積する"""
        if status:
            print(f"[警告] sounddevice status: {status}")
        with self._lock:
            self._frames.append(indata.copy())
 
    def start(self):
        """録音を開始する"""
        with self._lock:
            self._frames.clear()
 
        self._stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=self.channels,
            dtype=DTYPE,
            callback=self._callback,
        )
        self._stream.start()
        print("[Recorder] 録音開始")
 
    def stop(self) -> Path:
        """
        録音を停止し、WAV ファイルに保存して Path を返す。
        ファイルは tempfile で作成され、呼び出し元が削除責任を持つ。
        """
        if self._stream is None:
            raise RuntimeError("録音が開始されていません")
 
        self._stream.stop()
        self._stream.close()
        self._stream = None
 
        with self._lock:
            frames = self._frames.copy()
 
        if not frames:
            raise RuntimeError("録音データが空です")
 
        audio_data = np.concatenate(frames, axis=0)
 
        # tempfile: delete=False で呼び出し元が後片付けする設計
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp_path = Path(tmp.name)
        tmp.close()
 
        with wave.open(str(tmp_path), "wb") as wf:
            wf.setnchannels(self.channels)
            wf.setsampwidth(2)  # int16 = 2 bytes
            wf.setframerate(self.sample_rate)
            wf.writeframes(audio_data.tobytes())
 
        print(f"[Recorder] 録音停止 → 保存: {tmp_path}  ({len(audio_data) / self.sample_rate:.1f}秒)")
        return tmp_path
 
 
# ─────────────────────────────────────
# HotkeyController クラス
# ─────────────────────────────────────
class HotkeyController:
    """
    pynput を使ってグローバルホットキーを監視し、
    状態マシンに従って Recorder を制御する。
 
    状態遷移:
        IDLE       --[ホットキー]--> RECORDING
        RECORDING  --[ホットキー]--> IDLE (録音停止 + 保存)
    """
 
    def __init__(self, recorder: Recorder, on_recorded=None):
        self.recorder = recorder
        self.on_recorded = on_recorded  # 録音完了時のコールバック (Path を受け取る)
        self.state = State.IDLE
        self._pressed_keys: set = set()
        self._state_lock = threading.Lock()
 
    # ── キー正規化 ────────────────────────────
    @staticmethod
    def _normalize(key) -> object:
        """
        左右修飾キーを区別しない正規化。
        例: Key.ctrl_r → Key.ctrl_l
        """
        _map = {
            keyboard.Key.ctrl_r: keyboard.Key.ctrl_l,
            keyboard.Key.shift_r: keyboard.Key.shift,
            keyboard.Key.alt_r: keyboard.Key.alt,
        }
        return _map.get(key, key)
 
    # ── ホットキー判定 ────────────────────────
    def _is_hotkey(self) -> bool:
        normalized = {self._normalize(k) for k in self._pressed_keys}
        return HOTKEY.issubset(normalized)
 
    # ── イベントハンドラ ──────────────────────
    def _on_press(self, key):
        self._pressed_keys.add(key)
        if self._is_hotkey():
            self._handle_toggle()
 
    def _on_release(self, key):
        self._pressed_keys.discard(key)
 
    # ── 状態遷移 ──────────────────────────────
    def _handle_toggle(self):
        with self._state_lock:
            if self.state == State.IDLE:
                self.state = State.RECORDING
                print("[状態] IDLE → RECORDING")
                # 録音は別スレッドで開始 (メインの監視ループをブロックしない)
                threading.Thread(target=self.recorder.start, daemon=True).start()
 
            elif self.state == State.RECORDING:
                self.state = State.IDLE
                print("[状態] RECORDING → IDLE")
                # 録音停止・保存・コールバック呼び出しも別スレッドで
                threading.Thread(target=self._stop_and_callback, daemon=True).start()
 
    def _stop_and_callback(self):
        try:
            wav_path = self.recorder.stop()
            if self.on_recorded:
                self.on_recorded(wav_path)
        except Exception as e:
            print(f"[エラー] 録音停止中に例外: {e}")
            with self._state_lock:
                self.state = State.IDLE
 
    # ── リスナー起動 ──────────────────────────
    def start_listening(self):
        print(f"[HotkeyController] 監視開始 (Ctrl+Shift+Space で録音トグル)")
        print("[HotkeyController] 終了するには Ctrl+C を押してください")
        with keyboard.Listener(
            on_press=self._on_press,
            on_release=self._on_release,
        ) as listener:
            listener.join()
 
 
# ─────────────────────────────────────
# Step 1 の動作確認用エントリポイント
# ─────────────────────────────────────
def on_recorded(wav_path: Path):
    """
    Step 1 では録音完了後にファイル情報を表示するだけ。
    Step 2 以降でこのコールバックに STT 処理を追加する。
    """
    print(f"[on_recorded] WAV 保存完了: {wav_path}")
    print(f"  ファイルサイズ: {wav_path.stat().st_size / 1024:.1f} KB")
    print("  ※ Step 2 で STT 処理に渡されます")
 
    # Step 1 では後片付けをここで実施 (Step 5 統合後はパイプラインで削除)
    # wav_path.unlink()  # ← 動作確認のためコメントアウト
 
 
def main():
    recorder = Recorder()
    controller = HotkeyController(recorder=recorder, on_recorded=on_recorded)
 
    try:
        controller.start_listening()
    except KeyboardInterrupt:
        print("\n[main] 終了します")
 
 
if __name__ == "__main__":
    main()
 