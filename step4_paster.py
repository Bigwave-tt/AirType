"""
AirType - Step 4: クリップボードへのコピー + アクティブウィンドウへの自動ペースト

設計:
- pyperclip でテキストをクリップボードにコピー
- pynput で Ctrl+V (Windows/Linux) / Command+V (macOS) をシミュレート
- ペースト前に日本語IMEを無効化してテキストが変換候補に取り込まれるのを防ぐ
- ペースト後は IME の元の状態に復元する
- クリップボードコピー後に内容を検証し、不一致の場合はリトライする
- OS 自動判定でショートカットキーを切り替え
- 単体テスト: このファイルを直接実行するとテキスト入力をテストできる
"""

import sys
import time

import pyperclip
from pynput.keyboard import Controller, Key


# ─────────────────────────────────────
# 定数
# ─────────────────────────────────────
CLIPBOARD_SYNC_DELAY = 0.25   # クリップボードへのコピー後、ペーストまでの待機時間 (秒)
POST_PASTE_DELAY = 0.3        # ペースト後の安定待ち (ターゲットアプリが取り込む時間)
CLIPBOARD_RETRY_MAX = 2       # クリップボード書き込みの最大リトライ回数


# ─────────────────────────────────────
# IME制御 (Windows専用)
# ─────────────────────────────────────
def _get_ime_state() -> bool | None:
    """
    現在のフォアグラウンドウィンドウのIME状態を取得する。
    Windows以外はNoneを返す。
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        himc = ctypes.windll.imm32.ImmGetContext(hwnd)
        state = ctypes.windll.imm32.ImmGetOpenStatus(himc)
        ctypes.windll.imm32.ImmReleaseContext(hwnd, himc)
        return bool(state)
    except Exception:
        return None


def _set_ime_state(enabled: bool) -> None:
    """
    フォアグラウンドウィンドウのIME状態を設定する。
    Windows以外は何もしない。
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        himc = ctypes.windll.imm32.ImmGetContext(hwnd)
        ctypes.windll.imm32.ImmSetOpenStatus(himc, 1 if enabled else 0)
        ctypes.windll.imm32.ImmReleaseContext(hwnd, himc)
    except Exception:
        pass


# ─────────────────────────────────────
# Paster クラス
# ─────────────────────────────────────
class Paster:
    """
    テキストをクリップボードに書き込み、アクティブウィンドウにペーストする。

    Attributes
    ----------
    _keyboard : pynput.keyboard.Controller
        キーストロークシミュレーター
    _modifier_key : pynput.keyboard.Key
        ペーストに使うモディファイアキー (Key.ctrl or Key.cmd)
    """

    def __init__(self):
        self._keyboard = Controller()
        self._modifier_key = self._detect_modifier_key()
        print(f"[Paster] 初期化完了  ペーストキー: {self._modifier_key}+v")

    @staticmethod
    def _detect_modifier_key() -> Key:
        """OS に応じてペーストのモディファイアキーを返す"""
        if sys.platform == "darwin":
            return Key.cmd
        return Key.ctrl  # Windows / Linux

    def _copy_with_verify(self, text: str) -> bool:
        """
        クリップボードにコピーし、内容を検証する。
        最大CLIPBOARD_RETRY_MAX回リトライ。
        成功したらTrueを返す。
        """
        for attempt in range(CLIPBOARD_RETRY_MAX + 1):
            pyperclip.copy(text)
            time.sleep(CLIPBOARD_SYNC_DELAY)
            if pyperclip.paste() == text:
                return True
            if attempt < CLIPBOARD_RETRY_MAX:
                print(f"[Paster] クリップボード検証失敗、リトライ ({attempt + 1}/{CLIPBOARD_RETRY_MAX})")
        return False

    def paste(self, text: str):
        """
        テキストをクリップボードにコピーしてアクティブウィンドウにペーストする。

        Parameters
        ----------
        text : str
            ペーストするテキスト
        """
        if not text:
            print("[Paster] テキストが空のためスキップ")
            return

        # 1. クリップボードへコピー (検証付き)
        print(f"[Paster] クリップボードにコピー: {text[:50]!r}{'...' if len(text) > 50 else ''}")
        if not self._copy_with_verify(text):
            print("[Paster] クリップボードへの書き込みに失敗しました")
            return

        # 2. IMEを一時無効化
        ime_was_open = _get_ime_state()
        if ime_was_open:
            _set_ime_state(False)
            print("[Paster] IMEを無効化")

        try:
            # 3. Ctrl+V (または Command+V) でアトミックにペースト
            with self._keyboard.pressed(self._modifier_key):
                self._keyboard.press("v")
                self._keyboard.release("v")
            print(f"[Paster] ペースト実行 ({self._modifier_key}+v)")

            # 4. ターゲットアプリがクリップボード内容を取り込む時間を確保
            time.sleep(POST_PASTE_DELAY)

        finally:
            # 5. IMEを元の状態に復元
            if ime_was_open:
                _set_ime_state(True)
                print("[Paster] IMEを復元")

    def copy_only(self, text: str):
        """
        ペーストせずクリップボードへのコピーのみ行う。
        ユーザーが手動で貼り付けたい場合のフォールバック用。
        """
        pyperclip.copy(text)
        print(f"[Paster] クリップボードにコピーのみ: {text[:50]!r}")

    @staticmethod
    def get_clipboard() -> str:
        """現在のクリップボードの内容を取得する (デバッグ用)"""
        return pyperclip.paste()


# ─────────────────────────────────────
# 単体テスト用エントリポイント
# ─────────────────────────────────────
def _test_paste():
    """
    ペースト機能をテストする。
    実行後 3 秒以内にテキスト入力欄にフォーカスを当てると、
    テキストが自動入力される。
    """
    paster = Paster()

    test_text = "AirType の自動入力テストです。このテキストは音声入力から整形されました。"

    print(f"\n3 秒後にペーストを実行します...")
    print(f"テキスト入力欄（メモ帳など）にフォーカスを当ててください。")

    for i in range(3, 0, -1):
        print(f"  {i}...")
        time.sleep(1)

    paster.paste(test_text)

    # 確認
    time.sleep(0.5)
    clipboard_content = paster.get_clipboard()
    print(f"\n[確認] クリップボード内容: {clipboard_content!r}")
    assert clipboard_content == test_text, "クリップボードの内容が一致しません"
    print("[確認] OK")


if __name__ == "__main__":
    _test_paste()
