"""
AirType - Step 4: クリップボードへのコピー + アクティブウィンドウへの自動ペースト

設計:
- pyperclip でテキストをクリップボードにコピー
- pyautogui で Ctrl+V (Windows/Linux) / Command+V (macOS) をシミュレート
- ペースト前に短いウェイトを入れてクリップボード反映を確実にする
- OS 自動判定でショートカットキーを切り替え
- 単体テスト: このファイルを直接実行するとテキスト入力をテストできる
"""

import sys
import time

import pyautogui
import pyperclip


# ─────────────────────────────────────
# 定数
# ─────────────────────────────────────
PASTE_DELAY = 0.15   # クリップボードへのコピー後、ペーストまでの待機時間 (秒)
HOTKEY_DELAY = 0.05  # ホットキー操作間のインターバル (秒)

# pyautogui フェイルセーフ: マウスを画面の角に移動すると緊急停止
# 本番では False にすることもできるが、開発中は True 推奨
pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.05  # 各操作間のデフォルト待機


# ─────────────────────────────────────
# Paster クラス
# ─────────────────────────────────────
class Paster:
    """
    テキストをクリップボードに書き込み、アクティブウィンドウにペーストする。

    Attributes
    ----------
    paste_hotkey : str
        ペーストに使うホットキー ('ctrl' or 'command')
    """

    def __init__(self):
        self.paste_hotkey = self._detect_paste_hotkey()
        print(f"[Paster] 初期化完了  ペーストキー: {self.paste_hotkey}+v")

    @staticmethod
    def _detect_paste_hotkey() -> str:
        """OS に応じてペーストのモディファイアキーを返す"""
        if sys.platform == "darwin":
            return "command"
        return "ctrl"  # Windows / Linux

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

        # 1. クリップボードへコピー
        pyperclip.copy(text)
        print(f"[Paster] クリップボードにコピー: {text[:50]!r}{'...' if len(text) > 50 else ''}")

        # 2. クリップボードへの反映を待つ
        time.sleep(PASTE_DELAY)

        # 3. Ctrl+V (または Command+V) でペースト
        pyautogui.hotkey(self.paste_hotkey, "v", interval=HOTKEY_DELAY)
        print(f"[Paster] ペースト実行 ({self.paste_hotkey}+v)")

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
