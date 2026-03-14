"""
AirType - 軽量クライアント (クライアントPC用)

録音 (step1_recorder) と ペースト (step4_paster) のみを担当。
STT・LLM 処理はネットワーク上のサーバー (api_server.py) に委譲する。

動作フロー:
  [無変換 押下] → 録音開始
  [無変換 離放] → 録音停止 → WAV キューに投入
  [Worker スレッド] → WAV をサーバーへ送信 → テキスト受信 → ペースト

設定:
  airtype_config.json の network.server_url を自分の環境に合わせて変更してください。
  例: "server_url": "http://YOUR_SERVER_IP:8000/dictate"
  APIキーを使用する場合は network.api_key にサーバーと同じ値を設定してください。

起動方法:
  python client.py

必要なもの（クライアントPC側）:
  pip install pynput sounddevice numpy pyperclip requests
  ※ whisper.cpp / llama.cpp / GPU は不要
"""

import ctypes
import ctypes.wintypes
import queue
import threading
import time
from pathlib import Path

import requests

import config as _config
from step1_recorder import Recorder
from step4_paster import Paster


# ─────────────────────────────────────
# 設定読み込み
# ─────────────────────────────────────
_cfg         = _config.load()
_net         = _cfg["network"]

SERVER_URL       = _net["server_url"]
REQUEST_TIMEOUT  = int(_net["request_timeout"])
_API_KEY         = _net["api_key"]   # 空文字 = 認証なし


# ─────────────────────────────────────
# 定数
# ─────────────────────────────────────
PTT_KEY_VK   = 0x1D   # VK_NONCONVERT (無変換キー)
_POISON_PILL = None


# ─────────────────────────────────────
# VK_NONCONVERT 専用 Windows フック
# ─────────────────────────────────────
class _PttHook:
    """
    WH_KEYBOARD_LL を用いて VK_NONCONVERT (無変換) のみを握りつぶす。
    on_press / on_release コールバックで PTT イベントを通知する。
    """

    _WH_KEYBOARD_LL = 13
    _WM_KEYDOWN     = 0x0100
    _WM_KEYUP       = 0x0101
    _WM_SYSKEYDOWN  = 0x0104
    _WM_SYSKEYUP    = 0x0105

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
        self._hook_fn    = None
        self._thread_id  = None
        self._thread     = threading.Thread(
            target=self._run, daemon=True, name="PttHook"
        )

    def start(self):
        self._thread.start()

    def stop(self):
        if self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(
                self._thread_id, 0x0012, 0, 0  # WM_QUIT
            )

    def _run(self):
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()

        _HOOKPROC = ctypes.WINFUNCTYPE(
            ctypes.c_longlong,
            ctypes.c_int,
            ctypes.wintypes.WPARAM,
            ctypes.wintypes.LPARAM,
        )

        user32 = ctypes.windll.user32
        user32.SetWindowsHookExW.restype  = ctypes.wintypes.HHOOK
        user32.SetWindowsHookExW.argtypes = [
            ctypes.c_int, _HOOKPROC,
            ctypes.wintypes.HINSTANCE, ctypes.wintypes.DWORD,
        ]
        user32.CallNextHookEx.restype  = ctypes.c_longlong
        user32.CallNextHookEx.argtypes = [
            ctypes.wintypes.HHOOK, ctypes.c_int,
            ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM,
        ]

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
                    return 1  # 握りつぶす
            return user32.CallNextHookEx(None, nCode, wParam, lParam)

        self._hook_fn = _HOOKPROC(_hook_proc)
        self._hook = user32.SetWindowsHookExW(
            self._WH_KEYBOARD_LL, self._hook_fn, None, 0
        )

        if not self._hook:
            err = ctypes.windll.kernel32.GetLastError()
            print(f"[PttHook] ERROR: SetWindowsHookExW 失敗 (LastError={err})")
            return

        print(f"[PttHook] インストール完了 (VK=0x{PTT_KEY_VK:02X})")

        msg = ctypes.wintypes.MSG()
        while ctypes.windll.user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            ctypes.windll.user32.TranslateMessage(ctypes.byref(msg))
            ctypes.windll.user32.DispatchMessageW(ctypes.byref(msg))

        ctypes.windll.user32.UnhookWindowsHookEx(self._hook)
        print("[PttHook] 解除完了")


# ─────────────────────────────────────
# AirType クライアント本体
# ─────────────────────────────────────
class AirTypeClient:
    """
    録音 → サーバー送信 → ペースト を担当する軽量クライアント。

    HTTP リクエストはキューに積んだ後、専用の Worker スレッドで非同期実行する。
    これにより PTT フックや録音スレッドがネットワーク遅延でブロックされない。
    """

    def __init__(self):
        print("=" * 50)
        print("  AirType クライアントを起動しています...")
        print(f"  サーバー: {SERVER_URL}")
        print(f"  タイムアウト: {REQUEST_TIMEOUT}秒")
        print(f"  APIキー: {'設定あり' if _API_KEY else '設定なし'}")
        print("=" * 50)

        self.recorder = Recorder()
        self.paster   = Paster()

        self._ptt_key_down = False
        self._recording    = False
        self._lock         = threading.Lock()

        # WAV キュー + 不老不死 Worker スレッド
        # PTT 離放 → WAV パスをキューに投入するだけで即時復帰。
        # HTTP 送信の遅延・タイムアウトは Worker スレッドが吸収する。
        self._wav_queue: queue.Queue = queue.Queue()
        self._worker = threading.Thread(
            target=self._worker_loop,
            daemon=True,
            name="Client-Worker",
        )
        self._worker.start()

        # PTT フック
        self._ptt_hook = _PttHook(
            on_press=self._handle_ptt_press,
            on_release=self._handle_ptt_release,
        )
        self._ptt_hook.start()

        print("\n" + "=" * 50)
        print("  AirType クライアント起動完了")
        print(f"  PTT キー: 無変換 (VK=0x{PTT_KEY_VK:02X}) を押し続けて録音")
        print("  終了: Ctrl+C")
        print("=" * 50 + "\n")

    # ── PTT イベント ──────────────────────────────────────────────────
    def _handle_ptt_press(self):
        if self._ptt_key_down:
            return
        self._ptt_key_down = True

        with self._lock:
            if self._recording:
                return
            self._recording = True

        print("[Client] 録音開始")
        self.recorder.start()

    def _handle_ptt_release(self):
        if not self._ptt_key_down:
            return
        self._ptt_key_down = False

        with self._lock:
            if not self._recording:
                return
            self._recording = False

        # 録音停止・WAV 保存・キュー投入を別スレッドで実行（フックをブロックしない）
        threading.Thread(
            target=self._stop_and_enqueue,
            daemon=True,
            name="Client-Enqueue",
        ).start()

    # ── 録音停止 & キュー投入 ─────────────────────────────────────────
    def _stop_and_enqueue(self):
        """録音を停止して WAV パスをキューに積む。"""
        try:
            wav_path = self.recorder.stop()
            print(f"[Client] WAV をキューに投入: {wav_path.name}")
            self._wav_queue.put(wav_path)
        except Exception as e:
            print(f"[Client] 録音停止エラー: {e}")

    # ── Worker ループ ─────────────────────────────────────────────────
    def _worker_loop(self):
        """
        Queue から WAV パスを取り出してサーバーへ送信・ペーストを繰り返す。
        Poison Pill (None) を受け取ったら終了する。
        """
        print("[Client-Worker] 起動完了。キューを監視中...")
        while True:
            wav_path = self._wav_queue.get()
            if wav_path is _POISON_PILL:
                print("[Client-Worker] Poison Pill 受信。終了します。")
                self._wav_queue.task_done()
                break
            try:
                self._send_and_paste(wav_path)
            finally:
                if wav_path.exists():
                    wav_path.unlink()
                    print(f"[Client] 一時ファイルを削除: {wav_path.name}")
                self._wav_queue.task_done()

    # ── サーバー送信 & ペースト ───────────────────────────────────────
    def _send_and_paste(self, wav_path: Path):
        """WAV をサーバーへ送信し、返ってきたテキストをペーストする。"""
        print(f"[Client] サーバーへ送信中: {wav_path.name}")

        headers = {}
        if _API_KEY:
            headers["X-API-Key"] = _API_KEY

        try:
            with open(wav_path, "rb") as f:
                response = requests.post(
                    SERVER_URL,
                    files={"file": (wav_path.name, f, "audio/wav")},
                    headers=headers,
                    timeout=REQUEST_TIMEOUT,
                )
            response.raise_for_status()
        except requests.exceptions.ConnectionError:
            print(f"[Client] ERROR: サーバーに接続できません。{SERVER_URL} を確認してください。")
            return
        except requests.exceptions.Timeout:
            print(f"[Client] ERROR: サーバー応答タイムアウト ({REQUEST_TIMEOUT}秒)")
            return
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else "?"
            if status == 403:
                print("[Client] ERROR: 認証失敗。airtype_config.json の api_key を確認してください。")
            else:
                print(f"[Client] ERROR: サーバーエラー: {e}")
            return

        data = response.json()
        text = data.get("text", "").strip()

        if not text:
            print("[Client] 認識結果が空でした")
            return

        print(f"[Client] 受信テキスト: {text!r}")
        self.paster.paste(text)
        print("[Client] ペースト完了")

    # ── シャットダウン ─────────────────────────────────────────────────
    def shutdown(self):
        self._wav_queue.put(_POISON_PILL)
        self._worker.join(timeout=5.0)
        self._ptt_hook.stop()
        self.recorder.close()
        print("[Client] 終了しました")


# ─────────────────────────────────────
# エントリポイント
# ─────────────────────────────────────
def main():
    client = AirTypeClient()

    print("Ctrl+C で終了")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[Client] 終了します")
        client.shutdown()


if __name__ == "__main__":
    main()
