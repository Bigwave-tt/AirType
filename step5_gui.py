"""
AirType - Step 5: GUI コンポーネント

- TrayIcon      : pystray によるシステムトレイアイコン (右クリックメニュー付き)
- SettingsWindow: モデル選択などの設定ダイアログ (tkinter Toplevel)
- HistoryWindow : 認識テキスト履歴の一覧 (tkinter Toplevel)
- DictWindow    : 個人辞書の管理ウィンドウ (tkinter Toplevel)

スレッド安全性:
  pystray のコールバックは pystray スレッドで実行される。
  tkinter 操作はすべて root.after(0, ...) 経由でメインスレッドに委譲する。
"""

import ctypes
import sys
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


def _load_custom_tray_icon(path_str: str):
    """カスタム画像を 64×64 RGBA PIL Image に変換して返す。失敗時は None。"""
    if not _HAS_TRAY or not path_str:
        return None
    try:
        import icon_manager
        return icon_manager.make_tray_image(path_str)
    except Exception as e:
        print(f"[TrayIcon] カスタムアイコン読み込み失敗: {e}")
        return None


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
        on_dict: Callable = None,
        on_video_transcribe: Callable = None,
        on_float_toggle: Callable = None,
        get_float_visible: "Callable[[], bool]" = None,
        icon_path: str = "",
    ):
        self._root = root
        self._icon = None

        if not _HAS_TRAY:
            return

        custom = _load_custom_tray_icon(icon_path)
        self._idle_icon = custom if custom is not None else _make_icon(False)
        self._rec_icon  = _make_icon(True)  # 録音中は常にデフォルト赤アイコン

        menu_items = [
            pystray.MenuItem("設定",       lambda icon, item: root.after(0, on_settings)),
            pystray.MenuItem("認識履歴",   lambda icon, item: root.after(0, on_history)),
        ]
        if on_dict:
            menu_items.append(
                pystray.MenuItem("個人辞書", lambda icon, item: root.after(0, on_dict))
            )
        if on_video_transcribe:
            menu_items.append(
                pystray.MenuItem("動画文字起こし", lambda icon, item: root.after(0, on_video_transcribe))
            )
        if on_float_toggle and get_float_visible:
            menu_items.append(
                pystray.MenuItem(
                    "フローティングボタン",
                    lambda icon, item: root.after(0, on_float_toggle),
                    checked=lambda item: get_float_visible(),
                )
            )
        menu_items += [
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("終了",       lambda icon, item: root.after(0, on_quit)),
        ]
        menu = pystray.Menu(*menu_items)

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

    def update_icon(self, path_str: str):
        """待機中アイコンをカスタム画像に差し替える（空文字でデフォルトに戻す）。"""
        if not self._icon:
            return
        custom = _load_custom_tray_icon(path_str) if path_str else None
        self._idle_icon = custom if custom is not None else _make_icon(False)
        self._icon.icon = self._idle_icon

    def stop(self):
        if self._icon:
            try:
                self._icon.stop()
            except Exception:
                pass


# ─────────────────────────────────────
# フローティングボタン
# ─────────────────────────────────────
class FloatingButton:
    """
    常に最前面に表示されるドラッグ可能なフローティングボタン。

    状態:
      idle      - 待機中（青）  : クリックで録音開始
      recording - 録音中（赤）  : クリックで録音停止
      done      - 認識完了（緑）: クリックで最後のテキストを再ペースト

    WS_EX_NOACTIVATE を設定するため、クリックしてもフォーカスを奪わない。
    done 状態でクリックしたとき、直前までアクティブだったウィンドウへのペーストが正しく届く。
    """

    _IDLE_BG  = "#1565C0"
    _REC_BG   = "#C62828"
    _DONE_BG  = "#2E7D32"
    _FG       = "white"
    _FONT     = ("Yu Gothic UI", 10, "bold")
    _W, _H    = 130, 36
    _DRAG_THR = 6          # px: これを超えたらドラッグとみなす
    _DONE_TTL = 30_000     # ms: done 状態の自動リセット時間

    def __init__(
        self,
        master: tk.Tk,
        on_press_record: Callable,
        on_release_record: Callable,
        on_paste: Callable,
    ):
        self._master            = master
        self._on_press_record   = on_press_record
        self._on_release_record = on_release_record
        self._on_paste          = on_paste
        self._visible           = False
        self._state             = "idle"
        self._drag_x            = 0
        self._drag_y            = 0
        self._dragged           = False
        self._no_activate_set   = False

        win = tk.Toplevel(master)
        win.overrideredirect(True)
        win.wm_attributes("-topmost", True)
        win.wm_attributes("-alpha", 0.92)
        win.configure(bg=self._IDLE_BG)
        win.withdraw()
        self._win = win

        self._lbl = tk.Label(
            win,
            text="🎤 AirType",
            font=self._FONT,
            fg=self._FG,
            bg=self._IDLE_BG,
            padx=14,
            pady=6,
            cursor="hand2",
        )
        self._lbl.pack(fill=tk.BOTH, expand=True)

        # 初期位置: 画面右下
        win.update_idletasks()
        sw = win.winfo_screenwidth()
        sh = win.winfo_screenheight()
        win.geometry(f"{self._W}x{self._H}+{sw - self._W - 20}+{sh - self._H - 80}")

        for w in (win, self._lbl):
            w.bind("<ButtonPress-1>",  self._on_press)
            w.bind("<B1-Motion>",       self._on_drag)
            w.bind("<ButtonRelease-1>", self._on_release)

    # ── WS_EX_NOACTIVATE: クリックしてもキーボードフォーカスを奪わない ──
    def _apply_no_activate(self):
        if self._no_activate_set or sys.platform != "win32":
            return
        try:
            hwnd             = self._win.winfo_id()
            GWL_EXSTYLE      = -20
            WS_EX_NOACTIVATE = 0x08000000
            style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            ctypes.windll.user32.SetWindowLongW(
                hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE
            )
            self._no_activate_set = True
        except Exception as e:
            print(f"[FloatingButton] WS_EX_NOACTIVATE 設定失敗: {e}")

    # ── マウスイベント ────────────────────────────────────────────────
    def _on_press(self, event):
        self._drag_x  = event.x_root
        self._drag_y  = event.y_root
        self._dragged = False
        # WS_EX_NOACTIVATE 設定時でも B1-Motion を受け取れるよう明示的にキャプチャ
        if sys.platform == "win32":
            try:
                ctypes.windll.user32.SetCapture(self._win.winfo_id())
            except Exception:
                pass

    def _on_drag(self, event):
        if (abs(event.x_root - self._drag_x) > self._DRAG_THR
                or abs(event.y_root - self._drag_y) > self._DRAG_THR):
            self._dragged = True
        if self._dragged:
            new_x = self._win.winfo_x() + (event.x_root - self._drag_x)
            new_y = self._win.winfo_y() + (event.y_root - self._drag_y)
            self._win.geometry(f"+{new_x}+{new_y}")
            self._drag_x = event.x_root
            self._drag_y = event.y_root

    def _on_release(self, event):
        if sys.platform == "win32":
            try:
                ctypes.windll.user32.ReleaseCapture()
            except Exception:
                pass
        if self._dragged:
            return
        if self._state == "idle":
            self._on_press_record()
        elif self._state == "recording":
            self._on_release_record()
        elif self._state == "done":
            self._on_paste()

    # ── 状態変更（メインスレッドから呼ぶ）────────────────────────────
    def set_idle(self):
        self._state = "idle"
        self._lbl.configure(text="🎤 AirType", bg=self._IDLE_BG)
        self._win.configure(bg=self._IDLE_BG)

    def set_recording(self):
        self._state = "recording"
        self._lbl.configure(text="⏹ 停止", bg=self._REC_BG)
        self._win.configure(bg=self._REC_BG)

    def set_done(self, text: str):
        self._state = "done"
        preview = (text[:10] + "…") if len(text) > 10 else text
        self._lbl.configure(text=f"📋 {preview}", bg=self._DONE_BG)
        self._win.configure(bg=self._DONE_BG)
        # 一定時間後に自動リセット
        self._master.after(self._DONE_TTL, self._auto_reset)

    def _auto_reset(self):
        if self._state == "done":
            self.set_idle()

    # ── 表示制御 ─────────────────────────────────────────────────────
    def show(self):
        self._visible = True
        self._win.deiconify()
        self._win.lift()
        self._win.wm_attributes("-topmost", True)
        self._apply_no_activate()

    def hide(self):
        self._visible = False
        self._win.withdraw()

    def toggle(self):
        if self._visible:
            self.hide()
        else:
            self.show()

    @property
    def visible(self) -> bool:
        return self._visible


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
        get_icon_path: "Callable[[], str]" = lambda: "",
        on_icon_change: "Callable[[str], None]" = lambda p: None,
        get_shortcut_icon_path: "Callable[[], str]" = lambda: "",
        on_shortcut_icon_change: "Callable[[str], None]" = lambda p: None,
    ):
        self._master    = master
        self._get_model = get_model
        self._on_apply  = on_apply
        self._get_use_refiner           = get_use_refiner
        self._on_refiner_change         = on_refiner_change
        self._get_icon_path             = get_icon_path
        self._on_icon_change            = on_icon_change
        self._get_shortcut_icon_path    = get_shortcut_icon_path
        self._on_shortcut_icon_change   = on_shortcut_icon_change
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

        # ── ログイン時の自動起動 ────────────────────────────
        sep2 = ttk.Separator(win, orient="horizontal")
        sep2.grid(row=6, column=0, columnspan=2, sticky="ew", padx=12, pady=(6, 2))

        import autostart as _autostart
        autostart_var = tk.BooleanVar(value=_autostart.is_enabled())
        autostart_chk = tk.Checkbutton(
            win,
            text="ログイン時に AirType を自動起動する",
            variable=autostart_var,
            font=("Yu Gothic UI", 10),
        )
        autostart_chk.grid(row=7, column=0, columnspan=2, sticky="w", **PAD)

        tk.Label(
            win,
            text="有効にすると Windows ログイン時にバックグラウンドで自動起動します",
            font=("Yu Gothic UI", 9),
            fg="#888888",
        ).grid(row=8, column=0, columnspan=2, sticky="w", padx=12, pady=(0, 6))

        # ── アイコン設定 ────────────────────────────────────────────────
        sep3 = ttk.Separator(win, orient="horizontal")
        sep3.grid(row=9, column=0, columnspan=2, sticky="ew", padx=12, pady=(6, 2))

        tk.Label(win, text="アイコン設定:", font=("Yu Gothic UI", 10, "bold")).grid(
            row=10, column=0, columnspan=2, sticky="w", **PAD
        )

        def _make_icon_row(parent, var, title, row_offset):
            """アイコン行（ラベル + 参照ボタン + デフォルトに戻すボタン）を生成する。"""
            tk.Label(parent, text=title, font=("Yu Gothic UI", 9, "bold")).grid(
                row=row_offset, column=0, columnspan=2, sticky="w", padx=12, pady=(4, 0)
            )
            frame = tk.Frame(parent)
            frame.grid(row=row_offset + 1, column=0, columnspan=2, sticky="ew", padx=12, pady=(2, 0))

            lbl = tk.Label(frame, textvariable=var, font=("Yu Gothic UI", 9),
                           fg="#444444", anchor="w", width=36)
            lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)

            def _browse(v=var):
                from tkinter import filedialog
                p = filedialog.askopenfilename(
                    title="アイコン画像を選択",
                    filetypes=[
                        ("画像ファイル", "*.png *.jpg *.jpeg *.bmp *.gif *.ico *.webp"),
                        ("すべてのファイル", "*.*"),
                    ],
                    parent=win,
                )
                if p:
                    v.set(p)

            def _reset(v=var):
                v.set("")

            tk.Button(frame, text="参照...", width=8, command=_browse).pack(side=tk.LEFT, padx=(4, 2))
            tk.Button(frame, text="デフォルトに戻す", command=_reset).pack(side=tk.LEFT, padx=(2, 0))

        # トレイアイコン行
        tray_icon_var = tk.StringVar(value=self._get_icon_path())
        _make_icon_row(win, tray_icon_var, "トレイアイコン:", 11)

        # デスクトップショートカットアイコン行
        shortcut_icon_var = tk.StringVar(value=self._get_shortcut_icon_path())
        _make_icon_row(win, shortcut_icon_var, "デスクトップショートカットアイコン:", 13)

        tk.Label(
            win,
            text="PNG / ICO 等に対応。白・単色の背景は自動で除去されます",
            font=("Yu Gothic UI", 9),
            fg="#888888",
        ).grid(row=15, column=0, columnspan=2, sticky="w", padx=12, pady=(2, 2))

        def _apply_ico_to_lnk(ico_path, lnk_paths):
            """ico_path を指定した .lnk リストに適用し、結果を表示する。"""
            import icon_manager
            from pathlib import Path as _Path
            count = sum(
                1 for lnk in lnk_paths
                if icon_manager.update_shortcut_icon(_Path(lnk), ico_path)
            )
            if count > 0:
                messagebox.showinfo(
                    "更新完了",
                    f"{count} 個のショートカットのアイコンを更新しました。\n"
                    "すぐに反映されない場合はエクスプローラーを再起動してください。",
                    parent=win,
                )
            else:
                messagebox.showerror(
                    "更新失敗",
                    "ショートカットの更新に失敗しました。\n"
                    "pywin32 がインストールされているか確認してください:\n"
                    "  pip install pywin32",
                    parent=win,
                )

        def _update_shortcuts():
            """ICO を生成してデスクトップショートカットのアイコンを更新する。"""
            sc_path = shortcut_icon_var.get()
            if not sc_path:
                messagebox.showwarning(
                    "アイコン未選択",
                    "デスクトップショートカットアイコンを先に選択してください。",
                    parent=win,
                )
                return
            try:
                import icon_manager
                ico_path = icon_manager.generate_ico(sc_path)
                shortcuts = icon_manager.find_desktop_shortcuts()
                if shortcuts:
                    _apply_ico_to_lnk(ico_path, shortcuts)
                else:
                    # 自動検出できなかった場合は手動選択
                    if messagebox.askyesno(
                        "ショートカットが見つかりません",
                        "AirType のショートカット (.lnk) を自動検出できませんでした。\n\n"
                        "手動でショートカットファイルを選択しますか？",
                        parent=win,
                    ):
                        from tkinter import filedialog
                        lnk_path = filedialog.askopenfilename(
                            title="AirType のショートカットを選択",
                            filetypes=[("ショートカット", "*.lnk"), ("すべてのファイル", "*.*")],
                            parent=win,
                        )
                        if lnk_path:
                            _apply_ico_to_lnk(ico_path, [lnk_path])
            except Exception as e:
                messagebox.showerror("エラー", str(e), parent=win)

        tk.Button(
            win, text="ショートカットのアイコンを今すぐ更新",
            command=_update_shortcuts,
        ).grid(row=16, column=0, columnspan=2, sticky="w", padx=12, pady=(2, 6))

        btn_frame = tk.Frame(win)
        btn_frame.grid(row=17, column=0, columnspan=2, pady=(4, 12))

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
            # 自動起動の設定
            if autostart_var.get():
                _autostart.enable()
            else:
                _autostart.disable()
            self._on_refiner_change(refiner_var.get())
            self._on_icon_change(tray_icon_var.get())
            self._on_shortcut_icon_change(shortcut_icon_var.get())
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

    def __init__(self, master: tk.Tk, personal_dict=None):
        self._master       = master
        self._win          = None
        self._listbox      = None
        self._entries: list[tuple[str, str]] = []  # [(time_str, text), ...]
        self._lock         = threading.Lock()
        self._personal_dict = personal_dict  # PersonalDict | None

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
        if self._personal_dict is not None:
            tk.Button(
                btn_frame, text="辞書登録", width=10,
                command=lambda: self._open_register_dialog(win),
            ).pack(side=tk.LEFT, padx=4)
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

    def _open_register_dialog(self, parent: tk.Toplevel):
        """選択中の履歴テキストを個人辞書に登録するダイアログを開く。"""
        sel = self._listbox.curselection() if self._listbox else None
        initial_from = ""
        if sel:
            with self._lock:
                entries = list(self._entries)
            idx = len(entries) - 1 - sel[0]
            if 0 <= idx < len(entries):
                initial_from = entries[idx][1]

        dlg = tk.Toplevel(parent)
        dlg.title("辞書に登録")
        dlg.resizable(False, False)
        dlg.grab_set()

        PAD = {"padx": 12, "pady": 5}

        tk.Label(dlg, text="認識テキスト（変換前）:", font=("Yu Gothic UI", 10)).grid(
            row=0, column=0, sticky="w", **PAD
        )
        from_var = tk.StringVar(value=initial_from)
        from_entry = tk.Entry(dlg, textvariable=from_var, width=44, font=("Yu Gothic UI", 10))
        from_entry.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 8))

        tk.Label(dlg, text="正しいテキスト（変換後）:", font=("Yu Gothic UI", 10)).grid(
            row=2, column=0, sticky="w", **PAD
        )
        to_var = tk.StringVar()
        to_entry = tk.Entry(dlg, textvariable=to_var, width=44, font=("Yu Gothic UI", 10))
        to_entry.grid(row=3, column=0, sticky="ew", padx=12, pady=(0, 8))

        def register():
            from tkinter import messagebox as _mb
            ok = self._personal_dict.add(from_var.get(), to_var.get())
            if ok:
                _mb.showinfo("辞書登録完了",
                             f"{from_var.get()!r} → {to_var.get()!r} を登録しました。",
                             parent=dlg)
                dlg.destroy()
            else:
                _mb.showwarning("入力エラー", "変換前のテキストを入力してください。", parent=dlg)

        btn_frame = tk.Frame(dlg)
        btn_frame.grid(row=4, column=0, pady=(4, 12))
        tk.Button(btn_frame, text="登録",       width=10, command=register).pack(side=tk.LEFT, padx=6)
        tk.Button(btn_frame, text="キャンセル", width=10, command=dlg.destroy).pack(side=tk.LEFT, padx=6)

        dlg.update_idletasks()
        sw, sh = dlg.winfo_screenwidth(), dlg.winfo_screenheight()
        w,  h  = dlg.winfo_reqwidth(),    dlg.winfo_reqheight()
        dlg.geometry(f"+{(sw - w) // 2}+{(sh - h) // 2}")
        from_entry.focus_set()
        from_entry.selection_range(0, tk.END)


# ─────────────────────────────────────
# 個人辞書管理ウィンドウ
# ─────────────────────────────────────
class DictWindow:
    """
    個人辞書の一覧表示・追加・削除を行うウィンドウ。
    show() はメインスレッドから呼ぶこと。
    """

    def __init__(self, master: tk.Tk, personal_dict):
        self._master        = master
        self._personal_dict = personal_dict
        self._win           = None

    def show(self):
        if self._win and self._win.winfo_exists():
            self._win.lift()
            self._win.focus()
            return
        self._build()

    def _build(self):
        win = tk.Toplevel(self._master)
        win.title("個人辞書")
        win.geometry("520x420")
        self._win = win

        PAD = {"padx": 10, "pady": 4}

        # ── エントリ一覧 ──────────────────────────────────────────────
        tk.Label(win, text="登録済みの変換ルール:", font=("Yu Gothic UI", 10, "bold")).pack(
            anchor="w", **PAD
        )

        list_frame = tk.Frame(win)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 4))

        scrollbar = tk.Scrollbar(list_frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        listbox = tk.Listbox(
            list_frame,
            yscrollcommand=scrollbar.set,
            font=("Yu Gothic UI", 10),
            selectmode=tk.SINGLE,
            activestyle="none",
        )
        listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.config(command=listbox.yview)

        def refresh():
            listbox.delete(0, tk.END)
            for from_text, to_text in self._personal_dict.entries().items():
                listbox.insert(tk.END, f"{from_text}  →  {to_text}")

        refresh()

        def delete_selected():
            from tkinter import messagebox as _mb
            sel = listbox.curselection()
            if not sel:
                return
            item = listbox.get(sel[0])
            from_text = item.split("  →  ")[0]
            if _mb.askyesno("削除確認", f"{from_text!r} を削除しますか？", parent=win):
                self._personal_dict.remove(from_text)
                refresh()

        tk.Button(win, text="選択を削除", width=12, command=delete_selected).pack(
            anchor="w", **PAD
        )

        ttk.Separator(win, orient="horizontal").pack(fill=tk.X, padx=10, pady=6)

        # ── 新規登録フォーム ──────────────────────────────────────────
        tk.Label(win, text="新規登録:", font=("Yu Gothic UI", 10, "bold")).pack(
            anchor="w", **PAD
        )

        form_frame = tk.Frame(win)
        form_frame.pack(fill=tk.X, padx=10, pady=(0, 4))

        tk.Label(form_frame, text="変換前:", font=("Yu Gothic UI", 10), width=7, anchor="e").grid(
            row=0, column=0, padx=(0, 4), pady=3
        )
        from_var = tk.StringVar()
        tk.Entry(form_frame, textvariable=from_var, width=28, font=("Yu Gothic UI", 10)).grid(
            row=0, column=1, sticky="ew", pady=3
        )

        tk.Label(form_frame, text="変換後:", font=("Yu Gothic UI", 10), width=7, anchor="e").grid(
            row=1, column=0, padx=(0, 4), pady=3
        )
        to_var = tk.StringVar()
        tk.Entry(form_frame, textvariable=to_var, width=28, font=("Yu Gothic UI", 10)).grid(
            row=1, column=1, sticky="ew", pady=3
        )

        def add_entry():
            from tkinter import messagebox as _mb
            ok = self._personal_dict.add(from_var.get(), to_var.get())
            if ok:
                from_var.set("")
                to_var.set("")
                refresh()
            else:
                _mb.showwarning("入力エラー", "変換前のテキストを入力してください。", parent=win)

        tk.Button(form_frame, text="追加", width=8, command=add_entry).grid(
            row=0, column=2, rowspan=2, padx=(8, 0)
        )

        ttk.Separator(win, orient="horizontal").pack(fill=tk.X, padx=10, pady=6)

        tk.Button(win, text="閉じる", width=10, command=win.destroy).pack(pady=(0, 8))

        win.protocol("WM_DELETE_WINDOW", win.destroy)


# ─────────────────────────────────────
# 動画文字起こしウィンドウ
# ─────────────────────────────────────
class VideoTranscribeWindow:
    """
    動画ファイルを選択して文字起こしするウィンドウ。
    ffmpeg で音声抽出 → 既存の WhisperTranscriber で文字起こし。
    show() はメインスレッドから呼ぶこと。
    """

    # 動画推論タイムアウト (秒) - 通常の 30 秒より大幅に長く設定
    _VIDEO_INFER_TIMEOUT = 600

    def __init__(self, master: tk.Tk, transcriber, personal_dict=None):
        self._master        = master
        self._transcriber   = transcriber
        self._personal_dict = personal_dict
        self._win           = None

    def show(self):
        """ファイル選択ダイアログを開き、選択されたら文字起こしを開始する。"""
        if self._win and self._win.winfo_exists():
            self._win.lift()
            self._win.focus()
            return
        self._open_file_and_start()

    def _open_file_and_start(self):
        from tkinter import filedialog, messagebox
        from video_transcriber import find_ffmpeg

        ffmpeg = find_ffmpeg()
        if ffmpeg is None:
            messagebox.showerror(
                "ffmpeg が見つかりません",
                "ffmpeg がインストールされていないか、PATH に含まれていません。\n\n"
                "インストール後に再試行してください。\n"
                "または アプリフォルダ/ffmpeg/ffmpeg.exe に配置しても使用できます。",
            )
            return

        video_path_str = filedialog.askopenfilename(
            title="動画ファイルを選択",
            filetypes=[
                ("動画ファイル", "*.mp4 *.mov *.avi *.mkv *.wmv *.flv *.webm *.m4v *.ts"),
                ("すべてのファイル", "*.*"),
            ],
        )
        if not video_path_str:
            return  # キャンセル

        self._build_progress(video_path_str, ffmpeg)

    def _build_progress(self, video_path_str: str, ffmpeg: str):
        import tempfile
        import threading
        from pathlib import Path
        from video_transcriber import extract_audio

        video_path = Path(video_path_str)

        win = tk.Toplevel(self._master)
        win.title(f"文字起こし中 — {video_path.name}")
        win.resizable(False, False)
        win.grab_set()
        self._win = win

        tk.Label(
            win,
            text=f"ファイル: {video_path.name}",
            font=("Yu Gothic UI", 10),
            wraplength=420,
        ).pack(padx=24, pady=(16, 4))

        status_var = tk.StringVar(value="音声を抽出中...")
        tk.Label(
            win, textvariable=status_var,
            font=("Yu Gothic UI", 10), fg="#555555",
        ).pack(padx=24, pady=(0, 4))

        tk.Label(
            win,
            text="長い動画は数分かかる場合があります",
            font=("Yu Gothic UI", 9), fg="#888888",
        ).pack(padx=24, pady=(0, 16))

        win.update_idletasks()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        w,  h  = win.winfo_reqwidth(),    win.winfo_reqheight()
        win.geometry(f"+{(sw - w) // 2}+{(sh - h) // 2}")

        def _work():
            tmp_wav = None
            try:
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                    tmp_wav = Path(f.name)

                win.after(0, lambda: status_var.set("音声を抽出中..."))
                extract_audio(video_path, tmp_wav, ffmpeg_exe=ffmpeg)

                win.after(0, lambda: status_var.set("文字起こし中... (しばらくお待ちください)"))
                text = self._transcriber.transcribe(
                    tmp_wav,
                    infer_timeout=self._VIDEO_INFER_TIMEOUT,
                    force_cli=True,
                )

                if self._personal_dict:
                    text = self._personal_dict.apply(text)

                win.after(0, lambda: self._show_result(win, video_path, text))

            except Exception as e:
                err = str(e)
                win.after(0, lambda: self._show_error(win, err))
            finally:
                if tmp_wav and tmp_wav.exists():
                    try:
                        tmp_wav.unlink()
                    except Exception:
                        pass

        threading.Thread(target=_work, daemon=True, name="VideoTranscribe").start()

    def _show_result(self, progress_win: tk.Toplevel, video_path, text: str):
        progress_win.grab_release()
        progress_win.destroy()
        self._win = None

        win = tk.Toplevel(self._master)
        win.title(f"文字起こし結果 — {video_path.name}")
        win.geometry("620x460")
        self._win = win

        tk.Label(
            win,
            text=f"ファイル: {video_path.name}",
            font=("Yu Gothic UI", 10), fg="#555555",
        ).pack(anchor="w", padx=12, pady=(10, 2))

        text_frame = tk.Frame(win)
        text_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=4)

        scrollbar = tk.Scrollbar(text_frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        textbox = tk.Text(
            text_frame,
            yscrollcommand=scrollbar.set,
            font=("Yu Gothic UI", 11),
            wrap=tk.WORD,
        )
        textbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.config(command=textbox.yview)

        textbox.insert("1.0", text if text.strip() else "（テキストを検出できませんでした）")
        textbox.focus_set()  # ウィンドウを開いたらテキストにフォーカス

        btn_frame = tk.Frame(win)
        btn_frame.pack(fill=tk.X, padx=10, pady=(0, 10))

        def copy_all():
            result_text = textbox.get("1.0", tk.END).rstrip()
            win.clipboard_clear()
            win.clipboard_append(result_text)

        def save_as():
            from tkinter import filedialog
            from pathlib import Path as _Path
            save_path_str = filedialog.asksaveasfilename(
                title="テキストファイルとして保存",
                initialfile=video_path.stem + ".txt",
                defaultextension=".txt",
                filetypes=[("テキストファイル", "*.txt"), ("すべてのファイル", "*.*")],
            )
            if save_path_str:
                _Path(save_path_str).write_text(
                    textbox.get("1.0", tk.END).rstrip(), encoding="utf-8"
                )

        tk.Button(btn_frame, text="クリップボードにコピー", width=20, command=copy_all).pack(
            side=tk.LEFT, padx=4
        )
        tk.Button(btn_frame, text="テキストファイルで保存...", width=20, command=save_as).pack(
            side=tk.LEFT, padx=4
        )
        tk.Button(btn_frame, text="閉じる", width=10, command=win.destroy).pack(
            side=tk.RIGHT, padx=4
        )

        win.protocol("WM_DELETE_WINDOW", win.destroy)

        win.update_idletasks()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        w,  h  = 620, 460  # ウィンドウの実際のサイズで中央配置
        win.geometry(f"+{(sw - w) // 2}+{(sh - h) // 2}")

    def _show_error(self, progress_win: tk.Toplevel, error_msg: str):
        from tkinter import messagebox
        try:
            progress_win.grab_release()
            progress_win.destroy()
        except Exception:
            pass
        self._win = None
        messagebox.showerror(
            "文字起こしエラー",
            f"エラーが発生しました:\n\n{error_msg[:800]}",
        )
