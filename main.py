"""
AirType v2 - メインスクリプト (VAD 自動録音 + キューによる連続処理)

データフロー:
  [ホットキー] → 監視モード ON
  [マイク] → VAD が発話検出 → Queue に追加 → Worker が順に STT → ペースト
  [ホットキー] → 監視モード OFF

状態マシン:
  IDLE      : VAD 停止中 (ホットキーで LISTENING へ)
  LISTENING : VAD 監視中 + Worker がキューを随時処理

スレッドモデル:
  メインスレッド : pynput ホットキー監視 (常時稼働)
  VAD スレッド  : マイク監視・発話検出 (LISTENING 中に常時稼働)
  Worker スレッド: Queue から WAV を取り出して STT → ペースト (LISTENING 中に常時稼働)

  発話が PROCESSING 中に届いてもキューに積まれるため、音声は一切失われない。
"""

import argparse
import queue
import signal
import sys
import threading
import time
from enum import Enum, auto
from pathlib import Path

from pynput import keyboard

from step1_recorder import VADRecorder
from step2_transcriber import WhisperTranscriber, MODELS, DEFAULT_MODEL
from step3_refiner import RuleBasedRefiner
from step4_paster import Paster


# ─────────────────────────────────────
# 設定
# ─────────────────────────────────────
HOTKEY = {
    keyboard.Key.ctrl_l,
    keyboard.Key.shift,
    keyboard.Key.space,
}

_SENTINEL = None  # Worker スレッド終了シグナル


# ─────────────────────────────────────
# 状態定義
# ─────────────────────────────────────
class State(Enum):
    IDLE = auto()
    LISTENING = auto()


# ─────────────────────────────────────
# AirType アプリ本体
# ─────────────────────────────────────
class AirType:
    """
    AirType v2 の全処理を統合・管理するクラス。

    - ホットキーで監視モード ON/OFF
    - VAD が発話を自動検出 → Queue → Worker が順に STT → ペースト
    - PROCESSING 中の新発話もキューに積まれ、一切失われない
    """

    def __init__(self, model: str = DEFAULT_MODEL):
        print("=" * 50)
        print("  AirType v2 を起動しています...")
        print("=" * 50)

        # 各モジュール初期化
        self.transcriber = WhisperTranscriber(model=model)
        self.refiner = RuleBasedRefiner()
        self.paster = Paster()
        self.recorder = VADRecorder(on_audio_ready=self._on_audio_ready)

        # キュー (VAD → Worker)
        self._audio_queue: queue.Queue[Path | None] = queue.Queue()
        self._worker_thread: threading.Thread | None = None

        # 状態管理
        self.state = State.IDLE
        self._state_lock = threading.Lock()
        self._pressed_keys: set = set()

        print("\n" + "=" * 50)
        print("  AirType v2 起動完了")
        print("  ホットキー: Ctrl+Shift+Space で監視 ON/OFF")
        print("  話し終えると自動的に変換・ペーストされます")
        print("  終了: Ctrl+C を2回")
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
            if self.state == State.IDLE:
                self.state = State.LISTENING
                self._log_state("IDLE → LISTENING")
                self._start_worker()
                self.recorder.start_listening()

            elif self.state == State.LISTENING:
                self.state = State.IDLE
                self._log_state("LISTENING → IDLE")
                self.recorder.stop_listening()
                self._stop_worker()

    # ── Worker スレッド制御 ───────────────
    def _start_worker(self):
        """キューから WAV を取り出して処理するワーカーを起動する。"""
        self._worker_thread = threading.Thread(
            target=self._worker_loop, daemon=True
        )
        self._worker_thread.start()

    def _stop_worker(self):
        """ワーカーに終了シグナルを送る。キューに残る WAV は処理してから停止する。"""
        remaining = self._audio_queue.qsize()
        if remaining > 0:
            print(f"[Worker] 残り {remaining} 件を処理してから停止します")
        self._audio_queue.put(_SENTINEL)

    def _worker_loop(self):
        """LISTENING 中はキューを監視し、WAV が届いたら処理する。"""
        while True:
            try:
                wav_path = self._audio_queue.get()
                if wav_path is _SENTINEL:
                    print("[Worker] 停止")
                    break
                self._run_pipeline(wav_path)
            except Exception as e:
                print(f"[Worker] 予期しないエラー（処理を続行します）: {e}")

    # ── VAD コールバック ──────────────────
    def _on_audio_ready(self, wav_path: Path):
        """
        VADRecorder から発話区間の WAV が届いたときに呼ばれる。
        状態に関わらずキューに積む (Worker が順番に処理する)。
        """
        with self._state_lock:
            if self.state != State.LISTENING:
                if wav_path.exists():
                    wav_path.unlink()
                return

        queue_size = self._audio_queue.qsize()
        if queue_size > 0:
            print(f"[Queue] キューに追加 (待機中: {queue_size}件)")
        self._audio_queue.put(wav_path)

    # ── パイプライン実行 ──────────────────
    def _run_pipeline(self, wav_path: Path):
        """STT → フィラー除去 → ペースト を順に実行する。"""
        try:
            # 1. 音声認識 (STT)
            raw_text = self.transcriber.transcribe(wav_path)
            if not raw_text.strip():
                print("[AirType] 音声が認識されませんでした")
                return

            # 2. フィラー除去
            refined_text = self.refiner.refine(raw_text)
            if not refined_text.strip():
                refined_text = raw_text

            # 3. アクティブウィンドウへペースト
            self.paster.paste(refined_text)
            print(f"\n[AirType] 完了: {refined_text!r}\n")

        except Exception as e:
            print(f"[AirType] エラー: {e}")

        finally:
            if wav_path.exists():
                wav_path.unlink()

    # ── ユーティリティ ─────────────────────
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
                time.sleep(0.1)


# ─────────────────────────────────────
# エントリポイント
# ─────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="AirType v2 - 音声入力ツール")
    parser.add_argument(
        "--model",
        choices=list(MODELS),
        default=DEFAULT_MODEL,
        help=f"使用するWhisperモデル (デフォルト: {DEFAULT_MODEL})\n"
             "  large-v3 = 最高精度・量子化なし (ggml-large-v3.bin)\n"
             "  accurate = 高精度・量子化あり (ggml-large-v3-q5_0.bin)\n"
             "  turbo    = 高速・量子化あり  (ggml-large-v3-turbo-q5_0.bin)",
    )
    args = parser.parse_args()

    app = AirType(model=args.model)
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
