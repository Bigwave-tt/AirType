"""
AirType - Step 5: GUI コンポーネント

- TrayIcon      : pystray によるシステムトレイアイコン (右クリックメニュー付き)
- SettingsWindow: モデル選択などの設定ダイアログ (tkinter Toplevel)
- HistoryWindow : 認識テキスト履歴の一覧 (tkinter Toplevel)

スレッド安全性:
  pystray のコールバックは pystray スレッドで実行される。
  tkinter 操作はすべて root.after(0, ...) 経由でメインスレッドに委譲する。
"""

import threading
import tkinter as tk
from datetime import datetime
from tkinter import ttk
from typing import Callable

try:
    import pystray
    from PIL import Image, ImageDraw
    _HAS_TRAY = True
except ImportError:
    _HAS_TRAY = False
    print("[GUI] pystray / Pillow が未インストールです。トレイアイコンは無効。")
    print("      pip install pystray Pillow")


# ─────────────────────────────────────
# アイコン画像生成
# ─────────────────────────────────────
def _make_icon(recording: bool) -> "Image.Image":
    """トレイアイコン用 PIL 画像を生成する (64×64 RGBA)"""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # 外円
    bg = "#E53935" if recording else "#1565C0"
    draw.ellipse([2, 2, size - 2, size - 2], fill=bg)

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
class TrayIcon:
    """
    pystray を使ったシステムトレイアイコン。
    - 待機中: 青いマイクアイコン
    - 録音中: 赤いマイクアイコン
    - 右クリックメニュー: 設定 / 認識履歴 / 終了
    """

    def __init__(
        self,
        root: tk.Tk,
        on_settings: Callable,
        on_history: Callable,
        on_quit: Callable,
    ):
        self._root = root
        self._icon = None

        if not _HAS_TRAY:
            return

        self._idle_icon = _make_icon(False)
        self._rec_icon  = _make_icon(True)

        menu = pystray.Menu(
            pystray.MenuItem("設定",     lambda icon, item: root.after(0, on_settings)),
            pystray.MenuItem("認識履歴", lambda icon, item: root.after(0, on_history)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("終了",     lambda icon, item: root.after(0, on_quit)),
        )

        self._icon = pystray.Icon(
            "AirType",
            self._idle_icon,
            "AirType - 無変換キーで録音",
            menu,
        )

        threading.Thread(
            target=self._icon.run, daemon=True, name="TrayIcon"
        ).start()
        print("[TrayIcon] 起動完了")

    def set_recording(self, recording: bool):
        """アイコンと tooltip を録音状態に合わせて切り替える"""
        if not self._icon:
            return
        self._icon.icon  = self._rec_icon  if recording else self._idle_icon
        self._icon.title = "🎤 録音中..."  if recording else "AirType - 無変換キーで録音"

    def stop(self):
        if self._icon:
            try:
                self._icon.stop()
            except Exception:
                pass


# ─────────────────────────────────────
# 設定ウィンドウ
# ─────────────────────────────────────
class SettingsWindow:
    """
    モデル選択・Refiner ON/OFF などの設定ダイアログ。
    show() はメインスレッドから呼ぶこと。
    """

    # モデルキー → 表示ラベル (順番が Combobox の表示順)
    _MODELS = {
        "kotoba-q5":   "kotoba-q5   (推奨・538 MB・日本語特化)",
        "kotoba-full": "kotoba-full (最高精度・1.52 GB・日本語特化)",
    }

    def __init__(
        self,
        master: tk.Tk,
        get_model: Callable[[], str],
        on_apply: Callable[[str], None],
        get_use_refiner: Callable[[], bool] = lambda: True,
        on_refiner_change: Callable[[bool], None] = lambda v: None,
    ):
        self._master    = master
        self._get_model = get_model
        self._on_apply  = on_apply
        self._get_use_refiner  = get_use_refiner
        self._on_refiner_change = on_refiner_change
        self._win = None

    def show(self):
        if self._win and self._win.winfo_exists():
            self._win.lift()
            self._win.focus()
            return
        self._build()

    def _build(self):
        from step2_transcriber import MODELS as _WHISPER_MODELS
        from tkinter import messagebox

        win = tk.Toplevel(self._master)
        win.title("AirType 設定")
        win.resizable(False, False)
        win.grab_set()
        self._win = win

        PAD = {"padx": 12, "pady": 6}

        tk.Label(win, text="音声認識モデル:", font=("Yu Gothic UI", 10, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", **PAD
        )

        # ファイル存在チェックしながら表示ラベルを構築
        display_labels = []
        key_by_display: dict[str, str] = {}
        current_display = None

        for key, label in self._MODELS.items():
            path = _WHISPER_MODELS.get(key)
            if path and path.exists():
                display = label
            else:
                display = label + "  [未インストール]"
            display_labels.append(display)
            key_by_display[display] = key
            if key == self._get_model():
                current_display = display

        if current_display is None:
            current_display = display_labels[0]

        model_var = tk.StringVar(value=current_display)
        combo = ttk.Combobox(
            win, textvariable=model_var, values=display_labels,
            width=50, state="readonly",
        )
        combo.grid(row=1, column=0, columnspan=2, sticky="ew", **PAD)

        tk.Label(
            win,
            text="[未インストール] のモデルはファイルをダウンロード後に使用できます",
            font=("Yu Gothic UI", 9),
            fg="#888888",
        ).grid(row=2, column=0, columnspan=2, sticky="w", padx=12, pady=(0, 6))

        # ── LLM テキスト整形 (Refiner) ON/OFF ─────────────
        sep = ttk.Separator(win, orient="horizontal")
        sep.grid(row=3, column=0, columnspan=2, sticky="ew", padx=12, pady=(6, 2))

        refiner_var = tk.BooleanVar(value=self._get_use_refiner())
        refiner_chk = tk.Checkbutton(
            win,
            text="LLM テキスト整形を使用する（句読点追加・フィラー除去）",
            variable=refiner_var,
            font=("Yu Gothic UI", 10),
        )
        refiner_chk.grid(row=4, column=0, columnspan=2, sticky="w", **PAD)

        tk.Label(
            win,
            text="OFF にすると Whisper の認識結果をそのまま出力します（より正確な場合があります）",
            font=("Yu Gothic UI", 9),
            fg="#888888",
        ).grid(row=5, column=0, columnspan=2, sticky="w", padx=12, pady=(0, 6))

        btn_frame = tk.Frame(win)
        btn_frame.grid(row=6, column=0, columnspan=2, pady=(4, 12))

        def apply():
            selected_display = model_var.get()
            selected_key = key_by_display.get(selected_display)
            if not selected_key:
                return
            path = _WHISPER_MODELS.get(selected_key)
            if path and not path.exists():
                messagebox.showerror(
                    "モデルファイルなし",
                    f"モデルファイルが見つかりません:\n{path.name}\n\n"
                    f"whisper.cpp-windows-vulkan フォルダにダウンロードしてください。",
                    parent=win,
                )
                return
            self._on_refiner_change(refiner_var.get())
            self._on_apply(selected_key)
            win.destroy()

        tk.Button(btn_frame, text="適用", width=10, command=apply).pack(side=tk.LEFT, padx=6)
        tk.Button(btn_frame, text="キャンセル", width=10, command=win.destroy).pack(side=tk.LEFT, padx=6)

        win.update_idletasks()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        w,  h  = win.winfo_reqwidth(),    win.winfo_reqheight()
        win.geometry(f"+{(sw - w) // 2}+{(sh - h) // 2}")


# ─────────────────────────────────────
# 認識履歴ウィンドウ
# ─────────────────────────────────────
class HistoryWindow:
    """
    音声認識テキストの履歴を一覧表示するウィンドウ。
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
        # ウィンドウが開いていれば更新
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
        win.title("認識履歴")
        win.geometry("540x380")
        self._win = win

        # リストボックス + スクロールバー
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

        # ボタンバー
        btn_frame = tk.Frame(win)
        btn_frame.pack(fill=tk.X, padx=8, pady=(0, 8))

        def copy_selected():
            sel = self._listbox.curselection()
            if not sel:
                return
            with self._lock:
                entries = list(self._entries)
            # 新しい順に表示しているため逆引き
            idx = len(entries) - 1 - sel[0]
            if 0 <= idx < len(entries):
                text = entries[idx][1]
                win.clipboard_clear()
                win.clipboard_append(text)

        def clear_all():
            with self._lock:
                self._entries.clear()
            self._refresh_listbox()

        tk.Button(btn_frame, text="コピー",     width=10, command=copy_selected).pack(side=tk.LEFT,  padx=4)
        tk.Button(btn_frame, text="全クリア",   width=10, command=clear_all).pack(    side=tk.LEFT,  padx=4)
        tk.Button(btn_frame, text="閉じる",     width=10, command=win.destroy).pack(  side=tk.RIGHT, padx=4)

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
