"""
AirType - 軽量クライアント (クライアントPC用)

録音 (step1_recorder) と ペースト (step4_paster) のみを担当。
STT・LLM 処理はネットワーク上のサーバー (api_server.py) に委譲する。

動作フロー:
  [無変換 押下] → 録音開始
  [無変換 離放] → 録音停止 → WAV をサーバーへ送信 → テキスト受信 → ペースト

設定:
  SERVER_URL を自分の環境に合わせて変更してください。
  例: SERVER_URL = "http://192.168.1.100:8000/dictate"

起動方法:
  python client.py

必要なもの（クライアントPC側）:
  pip install pynput sounddevice numpy pyperclip requests
  ※ whisper.cpp / llama.cpp / GPU は不要
"""

import ctypes
import ctypes.wintypes
import sys
import threading
import time
from pathlib import Path

import requests

from step1_recorder import Recorder
from step4_paster import Paster


# ─────────────────────────────────────
# ★ 設定: サーバーのIPアドレスをここで変更 ★
# ─────────────────────────────────────
SERVER_URL = "http://192.168.1.100:8000/dictate"

# リクエストタイムアウト (秒) - STT + LLM の処理時間を考慮
REQUEST_TIMEOUT = 60


# ─────────────────────────────────────
# 定数
# ─────────────────────────────────────
PTT_KEY_VK   = 0x1D   # VK_NONCONVERT (無変換キー)
_POISON_PILL = None


# ─────────────────────────────────────
# VK_NONCONVERT 専用 Windows フック (main.py の _PttHook と同等)
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
    STT・LLM 処理はサーバーに委譲するため、GPU・重いモデル不要。
    """

    def __init__(self):
        print("=" * 50)
        print("  AirType クライアントを起動しています...")
        print(f"  サーバー: {SERVER_URL}")
        print("=" * 50)

        self.recorder = Recorder()
        self.paster   = Paster()

        self._ptt_key_down = False
        self._recording    = False
        self._lock         = threading.Lock()

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

        # キーリスナーをブロックしないよう別スレッドで処理
        threading.Thread(
            target=self._stop_and_send,
            daemon=True,
            name="Client-Send",
        ).start()

    def _stop_and_send(self):
        """録音停止 → サーバー送信 → ペースト"""
        try:
            wav_path = self.recorder.stop()
        except Exception as e:
            print(f"[Client] 録音停止エラー: {e}")
            return

        try:
            self._send_and_paste(wav_path)
        finally:
            if wav_path.exists():
                wav_path.unlink()
                print(f"[Client] 一時ファイルを削除: {wav_path.name}")

    def _send_and_paste(self, wav_path: Path):
        """WAV をサーバーへ送信し、返ってきたテキストをペーストする"""
        print(f"[Client] サーバーへ送信中: {wav_path.name}")
        try:
            with open(wav_path, "rb") as f:
                response = requests.post(
                    SERVER_URL,
                    files={"file": (wav_path.name, f, "audio/wav")},
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
            print(f"[Client] ERROR: サーバーエラー: {e}")
            return

        data = response.json()
        text = data.get("text", "").strip()

        if not text:
            print("[Client] 認識結果が空でした")
            return

        print(f"[Client] 受信テキスト: {text!r}")
        self.paster.paste(text)
        print(f"[Client] ペースト完了")

    def shutdown(self):
        self._ptt_hook.stop()
        print("[Client] 終了しました")


# ─────────────────────────────────────
# エントリポイント
# ─────────────────────────────────────
def main():
    client = AirTypeClient()

    print("Ctrl+C で終了")
    try:
        # メインスレッドを生かし続ける（PttHook はデーモンスレッド）
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[Client] 終了します")
        client.shutdown()


if __name__ == "__main__":
    main()
