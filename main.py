"""
AirType - メインスクリプト (全モジュール統合)

データフロー:
  [ホットキー] → 録音開始
  [ホットキー] → 録音停止 → STT (faster-whisper) → 整形 (Ollama) → ペースト

状態マシン:
  IDLE → RECORDING → PROCESSING → IDLE

スレッドモデル:
  メインスレッド : pynput ホットキー監視 (常時稼働)
  ワーカースレッド: 録音 / STT / LLM / ペースト (トリガー毎に起動)
  PROCESSING 中の再トリガーは無視する (二重実行防止)
"""

import signal
import sys
import threading
import time
from enum import Enum, auto
from pathlib import Path

from pynput import keyboard

from step1_recorder import Recorder
from step2_transcriber import WhisperTranscriber
from step3_refiner import RuleBasedRefiner
from step4_paster import Paster


# ─────────────────────────────────────
# 設定
# ─────────────────────────────────────
WHISPER_MODEL = "large-v3"        # tiny / base / small / medium / large-v3
HOTKEY = {
    keyboard.Key.ctrl_l,
    keyboard.Key.shift,
    keyboard.Key.space,
}


# ─────────────────────────────────────
# 状態定義
# ─────────────────────────────────────
class State(Enum):
    IDLE = auto()
    RECORDING = auto()
    PROCESSING = auto()


# ─────────────────────────────────────
# AirType アプリ本体
# ─────────────────────────────────────
class AirType:
    """
    AirType の全処理を統合・管理するクラス。

    - ホットキー検知 → 状態遷移
    - 録音 → STT → LLM 整形 → ペースト のパイプライン実行
    - エラー発生時は IDLE に戻してリトライ可能にする
    """

    def __init__(self):
        print("=" * 50)
        print("  AirType を起動しています...")
        print("=" * 50)

        # 各モジュール初期化
        self.recorder = Recorder()
        self.transcriber = WhisperTranscriber(model_size=WHISPER_MODEL)
        self.refiner = RuleBasedRefiner()
        self.paster = Paster()

        # 状態管理
        self.state = State.IDLE
        self._state_lock = threading.Lock()
        self._pressed_keys: set = set()

        print("\n" + "=" * 50)
        print("  AirType 起動完了")
        print(f"  ホットキー: Ctrl + Shift + Space")
        print(f"  終了: Ctrl + C を2回")
        print("=" * 50 + "\n")

    # ── キー正規化 ────────────────────────
    @staticmethod
    def _normalize(key) -> object:
        _map = {
            keyboard.Key.ctrl_r: keyboard.Key.ctrl_l,
            keyboard.Key.shift_r: keyboard.Key.shift,
        }
        return _map.get(key, key)

    def _is_hotkey(self) -> bool:
        normalized = {self._normalize(k) for k in self._pressed_keys}
        return HOTKEY.issubset(normalized)

    # ── ホットキーイベント ────────────────
    def _on_press(self, key):
        self._pressed_keys.add(key)
        if self._is_hotkey():
            self._handle_toggle()

    def _on_release(self, key):
        self._pressed_keys.discard(key)

    # ── 状態遷移ハンドラ ──────────────────
    def _handle_toggle(self):
        with self._state_lock:
            current = self.state

            if current == State.IDLE:
                self.state = State.RECORDING
                self._log_state("IDLE → RECORDING")
                threading.Thread(target=self.recorder.start, daemon=True).start()

            elif current == State.RECORDING:
                self.state = State.PROCESSING
                self._log_state("RECORDING → PROCESSING")
                threading.Thread(target=self._run_pipeline, daemon=True).start()

            elif current == State.PROCESSING:
                # 処理中の再トリガーは無視
                print("[AirType] 処理中です。完了までお待ちください...")

    # ── パイプライン実行 ──────────────────
    def _run_pipeline(self):
        """
        録音停止 → STT → LLM 整形 → ペースト を順に実行する。
        エラーが発生した場合は状態を IDLE に戻す。
        """
        wav_path: Path | None = None

        try:
            # 1. 録音停止・WAV 保存
            wav_path = self.recorder.stop()

            # 2. 音声認識 (STT)
            raw_text = self.transcriber.transcribe(wav_path)
            if not raw_text.strip():
                print("[AirType] 音声が認識されませんでした")
                return

            # 3. LLM によるテキスト整形
            refined_text = self.refiner.refine(raw_text)
            if not refined_text.strip():
                print("[AirType] LLM が空のテキストを返しました。生テキストを使用します")
                refined_text = raw_text
            elif self._is_ascii_dominant(refined_text) and not self._is_ascii_dominant(raw_text):
                print("[AirType] LLM が英語に変換しました。生テキストを使用します")
                refined_text = raw_text

            # 4. アクティブウィンドウへペースト
            self.paster.paste(refined_text)

            print(f"\n[AirType] 完了: {refined_text!r}\n")

        except Exception as e:
            print(f"[AirType] エラー: {e}")

        finally:
            # 一時ファイルの後片付け
            if wav_path and wav_path.exists():
                wav_path.unlink()
                print(f"[AirType] 一時ファイルを削除: {wav_path.name}")

            # 状態を IDLE に戻す
            with self._state_lock:
                self.state = State.IDLE
            self._log_state("PROCESSING → IDLE")

    # ── ユーティリティ ─────────────────────
    @staticmethod
    def _is_ascii_dominant(text: str) -> bool:
        """テキストの大半がASCII文字（英語など）かどうかを判定する"""
        if not text:
            return False
        ascii_count = sum(1 for c in text if ord(c) < 128 and c.isalpha())
        total_alpha = sum(1 for c in text if c.isalpha())
        return total_alpha > 0 and (ascii_count / total_alpha) > 0.8

    @staticmethod
    def _log_state(transition: str):
        print(f"[状態] {transition}")

    # ── 起動 ──────────────────────────────
    def run(self):
        """ホットキー監視ループを起動する (ブロッキング)"""
        with keyboard.Listener(
            on_press=self._on_press,
            on_release=self._on_release,
        ) as listener:
            while listener.running:
                time.sleep(0.1)  # 短いスリープで SIGINT を受け付ける


# ─────────────────────────────────────
# エントリポイント
# ─────────────────────────────────────
def main():
    app = AirType()
    _last_ctrl_c = [0.0]

    def _on_sigint(signum, frame):
        now = time.time()
        if now - _last_ctrl_c[0] < 2.0:
            print("\n[AirType] 終了します")
            sys.exit(0)
        _last_ctrl_c[0] = now
        print("\n[AirType] もう一度 Ctrl+C を押すと終了します (2秒以内)")

    signal.signal(signal.SIGINT, _on_sigint)

    try:
        app.run()
    except SystemExit:
        pass


if __name__ == "__main__":
    main()
