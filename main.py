"""
AirType - メインスクリプト (全モジュール統合)

データフロー:
  [無変換 押下] → OSD表示 + 録音開始
  [無変換 離放] → OSD非表示 + 録音停止 → WAV → Queue → Worker → STT → 整形 → ペースト

スレッドモデル:
  メインスレッド  : tkinter OSD イベントループ (tkinter は必ずメインスレッド)
  PttHook スレッド: WH_KEYBOARD_LL フック + Windows メッセージポンプ
  Worker スレッド : Queue から WAV を取り出して STT→整形→ペースト (不老不死)
  録音            : sounddevice 非同期ストリーム (追加スレッド不要)

Phase 1 - キー横取り:
  WH_KEYBOARD_LL フックで VK_NONCONVERT (無変換) を IME に渡す前に握りつぶす。
  それ以外のキーは CallNextHookEx で通常通りに通過させる。管理者権限不要。

Phase 2 - 録音中 OSD:
  枠なし・常に最前面・半透明・クリック透過の Toplevel ウィンドウ。
  画面下部中央に「🎤 録音中...」を表示。スレッドセーフな Queue 経由で制御。

Phase 3 - ランチャー:
  AirType_launcher.vbs を使ってコンソールなしでワンクリック起動可能。
"""

import ctypes
import ctypes.wintypes
import queue
import signal
import sys
import threading
import time
import tkinter as tk
from enum import Enum, auto
from pathlib import Path

from step1_recorder import Recorder
from step2_transcriber import WhisperTranscriber
from step3_refiner import RuleBasedRefiner
from step4_paster import Paster


# ─────────────────────────────────────
# pythonw.exe 実行時はログをファイルへリダイレクト
# ─────────────────────────────────────
if sys.stdout is None or not hasattr(sys.stdout, "write"):
    _log_path = Path(__file__).parent / "airtype.log"
    _log = open(_log_path, "w", encoding="utf-8", buffering=1)
    sys.stdout = _log
    sys.stderr = _log


# ─────────────────────────────────────
# 定数
# ─────────────────────────────────────
PTT_KEY_VK   = 0x1D   # VK_NONCONVERT (無変換キー)
_POISON_PILL = None   # Worker 終了シグナル


# ─────────────────────────────────────
# 状態定義
# ─────────────────────────────────────
class State(Enum):
    IDLE      = auto()
    RECORDING = auto()


# ─────────────────────────────────────
# Phase 1: VK_NONCONVERT 専用 Windows フック
# ─────────────────────────────────────
class _PttHook:
    """
    WH_KEYBOARD_LL を用いて VK_NONCONVERT (無変換) のみを握りつぶす。

    - 対象キーは PTT コールバックを呼び出してから return 1 で消費 (IME に届かない)
    - 他のキーは CallNextHookEx で通常通りに流す
    - 管理者権限不要。専用スレッドで Windows メッセージポンプを回す。
    """

    _WH_KEYBOARD_LL = 13
    _WM_KEYDOWN     = 0x0100
    _WM_KEYUP       = 0x0101
    _WM_SYSKEYDOWN  = 0x0104
    _WM_SYSKEYUP    = 0x0105

    _HOOKPROC = ctypes.WINFUNCTYPE(
        ctypes.c_long,
        ctypes.c_int,
        ctypes.wintypes.WPARAM,
        ctypes.wintypes.LPARAM,
    )

    class _KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ("vkCode",      ctypes.wintypes.DWORD),
            ("scanCode",    ctypes.wintypes.DWORD),
            ("flags",       ctypes.wintypes.DWORD),
            ("time",        ctypes.wintypes.DWORD),
            ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
        ]

    def __init__(self, on_press: callable, on_release: callable):
        self._on_press   = on_press
        self._on_release = on_release
        self._hook       = None
        self._hook_fn    = None   # GC 防止用参照
        self._thread_id  = None
        self._thread     = threading.Thread(
            target=self._run, daemon=True, name="PttHook"
        )

    def start(self):
        self._thread.start()

    def stop(self):
        """WM_QUIT を送ってメッセージループを終了させる"""
        if self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(
                self._thread_id, 0x0012, 0, 0  # WM_QUIT
            )

    def _run(self):
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()

        def _hook_proc(nCode, wParam, lParam):
            if nCode >= 0:
                kb = ctypes.cast(
                    lParam, ctypes.POINTER(self._KBDLLHOOKSTRUCT)
                ).contents
                if kb.vkCode == PTT_KEY_VK:
                    if wParam in (self._WM_KEYDOWN, self._WM_SYSKEYDOWN):
                        try:
                            self._on_press()
                        except Exception:
                            pass
                    elif wParam in (self._WM_KEYUP, self._WM_SYSKEYUP):
                        try:
                            self._on_release()
                        except Exception:
                            pass
                    return 1  # 握りつぶす (CallNextHookEx を呼ばない)
            return ctypes.windll.user32.CallNextHookEx(None, nCode, wParam, lParam)

        self._hook_fn = self._HOOKPROC(_hook_proc)
        self._hook = ctypes.windll.user32.SetWindowsHookExW(
            self._WH_KEYBOARD_LL, self._hook_fn, None, 0
        )

        if not self._hook:
            err = ctypes.windll.kernel32.GetLastError()
            print(f"[PttHook] ERROR: SetWindowsHookExW 失敗 (LastError={err})")
            return

        print(f"[PttHook] インストール完了 (VK=0x{PTT_KEY_VK:02X})")

        # WH_KEYBOARD_LL のコールバックを受けるにはメッセージポンプが必要
        msg = ctypes.wintypes.MSG()
        while ctypes.windll.user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            ctypes.windll.user32.TranslateMessage(ctypes.byref(msg))
            ctypes.windll.user32.DispatchMessageW(ctypes.byref(msg))

        ctypes.windll.user32.UnhookWindowsHookEx(self._hook)
        print("[PttHook] 解除完了")


# ─────────────────────────────────────
# Phase 2: 録音中インジケーター (OSD)
# ─────────────────────────────────────
class _OSD:
    """
    枠なし・常に最前面・半透明・クリック透過の小型オーバーレイウィンドウ。
    画面下部中央に録音中インジケーターを表示する。
    show() / hide() は必ずメインスレッド (tkinter ループ) から呼ぶこと。
    """

    _BG   = "#1C1C1C"
    _FG   = "#FF6B6B"
    _FONT = ("Yu Gothic UI", 13, "bold")

    def __init__(self, master: tk.Tk):
        self._win = tk.Toplevel(master)
        self._win.overrideredirect(True)           # タイトルバー・枠を消す
        self._win.wm_attributes("-topmost", True)  # 常に最前面
        self._win.wm_attributes("-alpha", 0.88)    # 半透明
        self._win.configure(bg=self._BG)

        self._label = tk.Label(
            self._win,
            text="  🎤 録音中...  ",
            font=self._FONT,
            fg=self._FG,
            bg=self._BG,
            padx=14,
            pady=8,
        )
        self._label.pack()

        # 画面下部中央に配置
        self._win.update_idletasks()
        sw = self._win.winfo_screenwidth()
        sh = self._win.winfo_screenheight()
        w  = self._win.winfo_reqwidth()
        h  = self._win.winfo_reqheight()
        self._win.geometry(f"+{(sw - w) // 2}+{sh - h - 60}")

        self._click_through_applied = False
        self._win.withdraw()  # 非表示で待機

    def _apply_click_through(self):
        """マウスイベントを背面に透過させる (Windows 専用・初回 show 時に適用)"""
        if sys.platform != "win32":
            return
        try:
            hwnd = self._win.winfo_id()
            GWL_EXSTYLE       = -20
            WS_EX_LAYERED     = 0x00080000
            WS_EX_TRANSPARENT = 0x00000020
            style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            ctypes.windll.user32.SetWindowLongW(
                hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED | WS_EX_TRANSPARENT
            )
            self._click_through_applied = True
        except Exception as e:
            print(f"[OSD] クリック透過設定失敗: {e}")

    def show(self):
        self._win.deiconify()
        if not self._click_through_applied:
            self._apply_click_through()
        self._win.lift()
        self._win.wm_attributes("-topmost", True)

    def hide(self):
        self._win.withdraw()


# ─────────────────────────────────────
# AirType アプリ本体
# ─────────────────────────────────────
class AirType:
    """
    AirType の全処理を統合・管理するクラス。

    - PTT フック (無変換キー) による録音トリガー + IME 干渉ブロック
    - OSD による録音状態の視覚的フィードバック
    - Queue + 不老不死 Worker による非同期 STT→整形→ペーストパイプライン
    """

    def __init__(self, root: tk.Tk):
        print("=" * 50)
        print("  AirType を起動しています...")
        print("=" * 50)

        self._root = root

        # 各モジュール初期化
        self.recorder    = Recorder()
        self.transcriber = WhisperTranscriber()
        self.refiner     = RuleBasedRefiner()
        self.paster      = Paster()

        # 状態管理
        self.state         = State.IDLE
        self._state_lock   = threading.Lock()
        self._ptt_key_down = False  # キーリピート対策フラグ

        # UI コマンドキュー (PttHook スレッド → メインスレッド)
        self._ui_queue: queue.Queue = queue.Queue()

        # OSD (メインスレッドで初期化)
        self._osd = _OSD(root)

        # WAV 処理キュー + 不老不死 Worker スレッド
        self._wav_queue: queue.Queue = queue.Queue()
        self._worker = threading.Thread(
            target=self._worker_loop, daemon=True, name="AirType-Worker"
        )
        self._worker.start()

        # PTT フック (VK_NONCONVERT 握りつぶし付き)
        self._ptt_hook = _PttHook(
            on_press=self._handle_ptt_press,
            on_release=self._handle_ptt_release,
        )
        self._ptt_hook.start()

        # UI ポーリング開始 (50ms ごと)
        self._root.after(50, self._poll_ui_queue)

        print("\n" + "=" * 50)
        print("  AirType 起動完了")
        print(f"  PTT キー: 無変換 (VK=0x{PTT_KEY_VK:02X}) を押し続けて録音")
        print("  終了: タスクマネージャーで終了 (コンソール起動時は Ctrl+C を2回)")
        print("=" * 50 + "\n")

    # ── PTT イベント (PttHook スレッドから呼ばれる) ─────────────────────
    def _handle_ptt_press(self):
        # キーリピート対策: すでに押下中なら 2回目以降は無視
        if self._ptt_key_down:
            return
        self._ptt_key_down = True

        with self._state_lock:
            if self.state != State.IDLE:
                return
            self.state = State.RECORDING
        self._log_state("IDLE → RECORDING")

        self.recorder.start()
        self._ui_queue.put("show")

    def _handle_ptt_release(self):
        if not self._ptt_key_down:
            return
        self._ptt_key_down = False

        with self._state_lock:
            if self.state != State.RECORDING:
                return
            self.state = State.IDLE
        self._log_state("RECORDING → IDLE (WAV をキューに投入)")

        self._ui_queue.put("hide")

        # キーリスナーをブロックしないよう別スレッドで停止・保存・投入
        threading.Thread(
            target=self._stop_and_enqueue,
            daemon=True,
            name="AirType-Enqueue",
        ).start()

    # ── UI ポーリング (メインスレッド) ─────────────────────────────────
    def _poll_ui_queue(self):
        """50ms ごとに UI キューを消化して OSD を更新する"""
        try:
            while True:
                cmd = self._ui_queue.get_nowait()
                if cmd == "show":
                    self._osd.show()
                elif cmd == "hide":
                    self._osd.hide()
        except queue.Empty:
            pass
        self._root.after(50, self._poll_ui_queue)

    # ── 録音停止 & WAV キュー投入 ──────────────────────────────────────
    def _stop_and_enqueue(self):
        try:
            wav_path = self.recorder.stop()
            print(f"[AirType] WAV をキューに投入: {wav_path.name}")
            self._wav_queue.put(wav_path)
        except Exception as e:
            print(f"[AirType] 録音停止エラー: {e}")

    # ── 不老不死 Worker ────────────────────────────────────────────────
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

    # ── パイプライン実行 ────────────────────────────────────────────────
    def _run_pipeline(self, wav_path: Path):
        """STT → テキスト整形 → ペースト を順に実行する"""
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

    # ── シャットダウン ─────────────────────────────────────────────────
    def shutdown(self):
        """フックを解除し、Queue に残った処理が終わるのを待ってから終了する"""
        print("[AirType] 終了処理中...")
        self._ptt_hook.stop()
        self._wav_queue.put(_POISON_PILL)
        self._worker.join(timeout=10.0)
        try:
            self._root.quit()
        except Exception:
            pass

    # ── ユーティリティ ─────────────────────────────────────────────────
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


# ─────────────────────────────────────
# エントリポイント
# ─────────────────────────────────────
def main():
    # tkinter はメインスレッドで起動する
    root = tk.Tk()
    root.withdraw()  # メイン Tk ウィンドウ自体は非表示 (OSD は Toplevel で独立表示)

    app = AirType(root)
    _last_ctrl_c = [0.0]

    def _on_sigint(signum, frame):
        now = time.time()
        if now - _last_ctrl_c[0] < 2.0:
            print("\n[AirType] 終了します")
            app.shutdown()
            return
        _last_ctrl_c[0] = now
        print("\n[AirType] もう一度 Ctrl+C を押すと終了します (2秒以内)")

    signal.signal(signal.SIGINT, _on_sigint)

    # tkinter の mainloop 中も Python シグナルハンドラを動作させるために定期覚醒
    def _wakeup():
        root.after(200, _wakeup)

    root.after(200, _wakeup)

    try:
        root.mainloop()
    except KeyboardInterrupt:
        app.shutdown()


if __name__ == "__main__":
    main()
