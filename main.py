"""
AirType - メインスクリプト (全モジュール統合)

データフロー:
  [無変換 押下] → 録音開始
  [無変換 離放] → 録音停止 → WAV → Queue → Worker → STT → 整形 → ペースト

状態マシン:
  IDLE ↔ RECORDING  (STT/ペースト処理は Worker スレッドが非同期で担当)

スレッドモデル:
  メインスレッド  : pynput キーボード監視 (常時稼働)
  Worker スレッド : Queue から WAV を取り出して STT→整形→ペーストを繰り返す (常時稼働・不老不死)
  録音は sounddevice の非同期ストリームで行うため追加スレッド不要

Push-to-Talk (PTT):
  無変換キー (VK_NONCONVERT = 29) を押し続けている間だけ録音する。
  キーを離した瞬間に WAV を Queue に投入し、Worker が処理する。
  Worker は死なないため、キーを連続して押しても全ての音声が順番に処理される。
"""

import queue
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
# 定数
# ─────────────────────────────────────
# 無変換キーの Windows 仮想キーコード (VK_NONCONVERT = 0x1D = 29)
PTT_KEY_VK = 29

# Worker への終了シグナル (Poison Pill)
_POISON_PILL = None


# ─────────────────────────────────────
# 状態定義
# ─────────────────────────────────────
class State(Enum):
    IDLE = auto()
    RECORDING = auto()


# ─────────────────────────────────────
# AirType アプリ本体
# ─────────────────────────────────────
class AirType:
    """
    AirType の全処理を統合・管理するクラス。

    - PTT キー検知 → 状態遷移
    - 録音 → Queue → Worker (STT → 整形 → ペースト) のパイプライン
    - Worker は終了シグナル (Poison Pill) を受け取るまで動き続ける
    """

    def __init__(self):
        print("=" * 50)
        print("  AirType を起動しています...")
        print("=" * 50)

        # 各モジュール初期化
        self.recorder = Recorder()
        self.transcriber = WhisperTranscriber()
        self.refiner = RuleBasedRefiner()
        self.paster = Paster()

        # 状態管理
        self.state = State.IDLE
        self._state_lock = threading.Lock()
        self._ptt_key_down = False  # キーリピート対策フラグ

        # Queue + 不老不死 Worker スレッド
        self._wav_queue: queue.Queue = queue.Queue()
        self._worker = threading.Thread(
            target=self._worker_loop,
            daemon=True,
            name="AirType-Worker",
        )
        self._worker.start()

        print("\n" + "=" * 50)
        print("  AirType 起動完了")
        print(f"  PTT キー: 無変換 (VK={PTT_KEY_VK}) を押し続けて録音")
        print(f"  終了: Ctrl + C を2回")
        print("=" * 50 + "\n")

    # ── PTT キー判定 ──────────────────────
    @staticmethod
    def _is_ptt_key(key) -> bool:
        """無変換キー (VK_NONCONVERT=29) かどうかを仮想キーコードで判定する"""
        return hasattr(key, "vk") and key.vk == PTT_KEY_VK

    # ── キーイベント ──────────────────────
    def _on_press(self, key):
        if not self._is_ptt_key(key):
            return

        # キーリピート対策: すでに押下中なら 2回目以降のイベントは無視する
        if self._ptt_key_down:
            return
        self._ptt_key_down = True

        with self._state_lock:
            if self.state != State.IDLE:
                return
            self.state = State.RECORDING
        self._log_state("IDLE → RECORDING")
        self.recorder.start()

    def _on_release(self, key):
        if not self._is_ptt_key(key):
            return
        if not self._ptt_key_down:
            return
        self._ptt_key_down = False

        with self._state_lock:
            if self.state != State.RECORDING:
                return
            self.state = State.IDLE
        self._log_state("RECORDING → IDLE (WAV をキューに投入)")

        # 録音停止・WAV 保存・Queue 投入はキーリスナーをブロックしないよう別スレッドで行う
        threading.Thread(
            target=self._stop_and_enqueue,
            daemon=True,
            name="AirType-Enqueue",
        ).start()

    # ── 録音停止 & キュー投入 ──────────────
    def _stop_and_enqueue(self):
        """録音を停止して WAV ファイルを Queue に投入する"""
        try:
            wav_path = self.recorder.stop()
            print(f"[AirType] WAV をキューに投入: {wav_path.name}")
            self._wav_queue.put(wav_path)
        except Exception as e:
            print(f"[AirType] 録音停止エラー: {e}")

    # ── 不老不死 Worker ────────────────────
    def _worker_loop(self):
        """
        Queue から WAV を取り出して STT → 整形 → ペーストを繰り返す。
        Poison Pill (None) を受け取ったら終了する。
        """
        print("[Worker] 起動完了。キューを監視中...")
        while True:
            wav_path = self._wav_queue.get()
            if wav_path is _POISON_PILL:
                print("[Worker] Poison Pill 受信。終了します。")
                self._wav_queue.task_done()
                break
            self._run_pipeline(wav_path)
            self._wav_queue.task_done()

    # ── パイプライン実行 ────────────────────
    def _run_pipeline(self, wav_path: Path):
        """STT → 整形 → ペースト を順に実行する"""
        try:
            # 1. 音声認識 (STT)
            raw_text = self.transcriber.transcribe(wav_path)
            if not raw_text.strip():
                print("[AirType] 音声が認識されませんでした")
                return

            # 2. テキスト整形
            refined_text = self.refiner.refine(raw_text)
            if not refined_text.strip():
                print("[AirType] 整形後が空のため生テキストを使用します")
                refined_text = raw_text
            elif self._is_ascii_dominant(refined_text) and not self._is_ascii_dominant(raw_text):
                print("[AirType] LLM が英語に変換しました。生テキストを使用します")
                refined_text = raw_text

            # 3. アクティブウィンドウへペースト
            self.paster.paste(refined_text)
            print(f"\n[AirType] 完了: {refined_text!r}\n")

        except Exception as e:
            print(f"[AirType] パイプラインエラー: {e}")

        finally:
            if wav_path and wav_path.exists():
                wav_path.unlink()
                print(f"[AirType] 一時ファイルを削除: {wav_path.name}")

    # ── シャットダウン ─────────────────────
    def shutdown(self):
        """キューに残った処理を完了させてから Worker を安全に停止する"""
        print("[AirType] Worker に終了シグナルを送信...")
        self._wav_queue.put(_POISON_PILL)
        self._worker.join(timeout=10.0)

    # ── ユーティリティ ──────────────────────
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

    # ── 起動 ───────────────────────────────
    def run(self):
        """キーボード監視ループを起動する (ブロッキング)"""
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
            app.shutdown()
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
