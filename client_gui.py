"""
AirType - クライアント GUI コンポーネント

- ClientTrayIcon  : システムトレイアイコン (待機/録音中/処理中 の3状態)
- ClientHistoryWindow: 受信テキスト履歴ウィンドウ

スレッド安全性:
  pystray コールバックは pystray スレッドで実行される。
  tkinter 操作はすべて root.after(0, ...) 経由でメインスレッドに委譲する。
"""

import threading
import tkinter as tk
from datetime import datetime
from tkinter import ttk

try:
    import pystray
    from PIL import Image, ImageDraw
    _HAS_TRAY = True
except ImportError:
    _HAS_TRAY = False
    print("[ClientGUI] pystray / Pillow が未インストールです。トレイアイコンは無効。")
    print("            pip install pystray Pillow")


# ─────────────────────────────────────
# 状態定義
# ─────────────────────────────────────
_STATES = {
    #  state_key : (bg_color, tooltip_text)
    "idle":       ("#1565C0", "AirType クライアント - 無変換で録音"),
    "recording":  ("#E53935", "🎤 録音中..."),
    "processing": ("#F57C00", "⏳ 処理中..."),
    "error":      ("#757575", "⚠ エラーが発生しました"),
}


# ─────────────────────────────────────
# アイコン画像生成
# ─────────────────────────────────────
def _make_icon(state: str) -> "Image.Image":
    """トレイアイコン用 PIL 画像を生成する (64×64 RGBA)"""
    bg, _ = _STATES.get(state, _STATES["idle"])
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # 外円
    draw.ellipse([2, 2, size - 2, size - 2], fill=bg)

    if state == "processing":
        # 砂時計風: 白い ⏳ 代わりに白い三角2つ
        draw.polygon([(20, 14), (44, 14), (32, 30)], fill="white")
        draw.polygon([(20, 50), (44, 50), (32, 34)], fill="white")
        draw.rectangle([20, 14, 44, 15], fill="white")
        draw.rectangle([20, 49, 44, 50], fill="white")
    else:
        # マイク本体 (白)
        draw.rounded_rectangle([24, 12, 40, 34], radius=8, fill="white")
        # マイクスタンド
        draw.rectangle([30, 34, 34, 46], fill="white")
        draw.rectangle([22, 46, 42, 50], fill="white")
        # アーク (口元)
        draw.arc([18, 26, 46, 48], start=0, end=180, fill="white", width=3)

    return img


# ─────────────────────────────────────
# システムトレイアイコン
# ─────────────────────────────────────
class ClientTrayIcon:
    """
    pystray を使ったシステムトレイアイコン。
    - 待機中  : 青いマイクアイコン
    - 録音中  : 赤いマイクアイコン
    - 処理中  : 橙の砂時計アイコン
    - 右クリックメニュー: 認識履歴 / 終了
    """

    def __init__(self, root: tk.Tk, on_history, on_quit):
        self._root  = root
        self._icon  = None
        self._icons = {}

        if not _HAS_TRAY:
            return

        for state in _STATES:
            self._icons[state] = _make_icon(state)

        menu = pystray.Menu(
            pystray.MenuItem("認識履歴", lambda icon, item: root.after(0, on_history)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("終了",     lambda icon, item: root.after(0, on_quit)),
        )

        self._icon = pystray.Icon(
            "AirType Client",
            self._icons["idle"],
            _STATES["idle"][1],
            menu,
        )

        threading.Thread(
            target=self._icon.run, daemon=True, name="ClientTrayIcon"
        ).start()
        print("[ClientTrayIcon] 起動完了")

    def set_state(self, state: str):
        """トレイアイコンの状態を切り替える (idle / recording / processing / error)"""
        if not self._icon:
            return
        icon_img = self._icons.get(state, self._icons["idle"])
        tooltip   = _STATES.get(state, _STATES["idle"])[1]
        self._icon.icon  = icon_img
        self._icon.title = tooltip

    def stop(self):
        if self._icon:
            try:
                self._icon.stop()
            except Exception:
                pass


# ─────────────────────────────────────
# 認識履歴ウィンドウ
# ─────────────────────────────────────
class ClientHistoryWindow:
    """
    サーバーから受信したテキストの履歴を一覧表示するウィンドウ。
    add() はどのスレッドからでも呼べる。
    show() はメインスレッドから呼ぶこと。
    """

    MAX_ENTRIES = 200

    def __init__(self, master: tk.Tk):
        self._master  = master
        self._win     = None
        self._listbox = None
        self._entries: list[tuple[str, str]] = []  # [(time_str, text), ...]
        self._lock    = threading.Lock()

    def add(self, text: str):
        """履歴にエントリを追加する（スレッドセーフ）"""
        time_str = datetime.now().strftime("%H:%M:%S")
        with self._lock:
            self._entries.append((time_str, text))
            if len(self._entries) > self.MAX_ENTRIES:
                self._entries.pop(0)
        if self._listbox and self._win and self._win.winfo_exists():
            self._win.after(0, self._refresh_listbox)

    def show(self):
        if self._win and self._win.winfo_exists():
            self._win.lift()
            self._win.focus()
            return
        self._build()

    def _build(self):
        win = tk.Toplevel(self._master)
        win.title("認識履歴 (クライアント)")
        win.geometry("540x380")
        self._win = win

        frame = tk.Frame(win)
        frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=(8, 4))

        scrollbar = tk.Scrollbar(frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self._listbox = tk.Listbox(
            frame,
            yscrollcommand=scrollbar.set,
            font=("Yu Gothic UI", 10),
            selectmode=tk.SINGLE,
            activestyle="none",
        )
        self._listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.config(command=self._listbox.yview)

        self._refresh_listbox()

        btn_frame = tk.Frame(win)
        btn_frame.pack(fill=tk.X, padx=8, pady=(0, 8))

        def copy_selected():
            sel = self._listbox.curselection()
            if not sel:
                return
            with self._lock:
                entries = list(self._entries)
            idx = len(entries) - 1 - sel[0]
            if 0 <= idx < len(entries):
                text = entries[idx][1]
                win.clipboard_clear()
                win.clipboard_append(text)

        def clear_all():
            with self._lock:
                self._entries.clear()
            self._refresh_listbox()

        tk.Button(btn_frame, text="コピー",   width=10, command=copy_selected).pack(side=tk.LEFT,  padx=4)
        tk.Button(btn_frame, text="全クリア", width=10, command=clear_all).pack(    side=tk.LEFT,  padx=4)
        tk.Button(btn_frame, text="閉じる",   width=10, command=win.destroy).pack(  side=tk.RIGHT, padx=4)

        def on_close():
            self._listbox = None
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", on_close)

    def _refresh_listbox(self):
        if not self._listbox or not self._win or not self._win.winfo_exists():
            return
        with self._lock:
            entries = list(self._entries)
        self._listbox.delete(0, tk.END)
        for time_str, text in reversed(entries):
            self._listbox.insert(tk.END, f"[{time_str}]  {text}")
