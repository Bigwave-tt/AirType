"""
AirType v2 - メインスクリプト (VAD 自動録音)

データフロー:
  [ホットキー] → 監視モード ON
  [マイク] → VAD が発話検出 → 自動録音 → STT (whisper.cpp) → ペースト
  [ホットキー] → 監視モード OFF

状態マシン:
  IDLE       : VAD 停止中 (ホットキーで LISTENING へ)
  LISTENING  : VAD 監視中、発話を待機 (発話終了で自動的に PROCESSING へ)
  PROCESSING : STT + ペースト実行中 (完了で LISTENING へ戻る)

スレッドモデル:
  メインスレッド : pynput ホットキー監視 (常時稼働)
  VAD スレッド  : マイク監視・発話検出 (LISTENING 中に常時稼働)
  ワーカースレッド: STT / ペースト (発話検出のたびに起動)
  ※ PROCESSING 中に新たな発話が終了しても、そのブロックはスキップする
"""

import signal
import sys
import threading
import time
from enum import Enum, auto
from pathlib import Path

from pynput import keyboard

from step1_recorder import VADRecorder
from step2_transcriber import WhisperTranscriber
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


# ─────────────────────────────────────
# 状態定義
# ─────────────────────────────────────
class State(Enum):
    IDLE = auto()
    LISTENING = auto()
    PROCESSING = auto()


# ─────────────────────────────────────
# AirType アプリ本体
# ─────────────────────────────────────
class AirType:
    """
    AirType v2 の全処理を統合・管理するクラス。

    - ホットキーで監視モード ON/OFF
    - VAD が発話を自動検出 → STT → ペーストのパイプライン実行
    """

    def __init__(self):
        print("=" * 50)
        print("  AirType v2 を起動しています...")
        print("=" * 50)

        # 各モジュール初期化
        self.transcriber = WhisperTranscriber(use_kotoba=True)
        self.refiner = RuleBasedRefiner()
        self.paster = Paster()
        self.recorder = VADRecorder(on_audio_ready=self._on_audio_ready)

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
                self.recorder.start_listening()

            elif self.state == State.LISTENING:
                self.state = State.IDLE
                self._log_state("LISTENING → IDLE")
                self.recorder.stop_listening()

            elif self.state == State.PROCESSING:
                print("[AirType] 処理中です。完了までお待ちください...")

    # ── VAD コールバック ──────────────────
    def _on_audio_ready(self, wav_path: Path):
        """
        VADRecorder から発話区間の WAV が届いたときに呼ばれる。
        PROCESSING 中は新しい音声をスキップする。
        """
        with self._state_lock:
            if self.state != State.LISTENING:
                # IDLE または既に PROCESSING 中 → スキップ
                if wav_path.exists():
                    wav_path.unlink()
                if self.state == State.PROCESSING:
                    print("[AirType] 処理中のため今回の発話はスキップします")
                return
            self.state = State.PROCESSING
            self._log_state("LISTENING → PROCESSING")

        self._run_pipeline(wav_path)

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

            with self._state_lock:
                if self.state == State.PROCESSING:
                    self.state = State.LISTENING
                    self._log_state("PROCESSING → LISTENING")

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
