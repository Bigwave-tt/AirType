"""
AirType - Step 5: GUI コンポーネント

- TrayIcon      : Windows ネイティブ実装のシステムトレイアイコン (右クリックメニュー付き)
- SettingsWindow: モデル選択などの設定ダイアログ (tkinter Toplevel)
- HistoryWindow : 認識テキスト履歴の一覧 (tkinter Toplevel)
- DictWindow    : 個人辞書の管理ウィンドウ (tkinter Toplevel)

スレッド安全性:
  TrayIcon のコールバックは Win32 スレッドで実行される。
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
    from PIL import Image, ImageDraw
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False
    print("[GUI] Pillow が未インストールです。pip install Pillow")

if sys.platform == "win32":
    try:
        from tray_win32 import Win32TrayIcon, MenuItem as _TrayItem, SEPARATOR as _SEP, pil_to_hicon
        _HAS_TRAY = True
    except Exception as e:
        _HAS_TRAY = False
        print(f"[GUI] tray_win32 の読み込み失敗: {e}")
else:
    _HAS_TRAY = False


# ─────────────────────────────────────
# アイコン画像生成
# ─────────────────────────────────────
def _make_icon(recording: bool) -> "Image.Image":
    """トレイアイコン用 PIL 画像を生成する (64×64 RGBA)"""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    bg = "#E53935" if recording else "#1565C0"
    draw.ellipse([2, 2, size - 2, size - 2], fill=bg)
    draw.rounded_rectangle([24, 12, 40, 34], radius=8, fill="white")
    draw.rectangle([30, 34, 34, 46], fill="white")
    draw.rectangle([22, 46, 42, 50], fill="white")
    draw.arc([18, 26, 46, 48], start=0, end=180, fill="white", width=3)
    return img


def _load_custom_tray_icon(path_str: str):
    """カスタム画像を 64×64 RGBA PIL Image に変換して返す。失敗時は None。"""
    if not _HAS_PIL or not path_str:
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
    Windows ネイティブ実装のシステムトレイアイコン (tray_win32.Win32TrayIcon を使用)。
    - 待機中: 青いマイクアイコン
    - 録音中: 赤いマイクアイコン
    - 右クリックメニュー: ホーム / 設定 / 認識履歴 / 終了
    - グルグルカーソル問題を解決済み
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
        on_home: Callable = None,
    ):
        self._root       = root
        self._tray       = None
        self._idle_pil   = None
        self._rec_pil    = None
        self._idle_hicon = 0
        self._rec_hicon  = 0

        if not _HAS_TRAY or not _HAS_PIL:
            return

        # PIL アイコン画像を生成（カスタムがあれば優先）
        custom = _load_custom_tray_icon(icon_path)
        self._idle_pil = custom if custom is not None else _make_icon(False)
        self._rec_pil  = _make_icon(True)
        self._idle_hicon = pil_to_hicon(self._idle_pil)
        self._rec_hicon  = pil_to_hicon(self._rec_pil)

        # メニュー項目を構築
        def _sched(fn):
            root.after(0, fn)

        items: list = []
        if on_home:
            items.append(_TrayItem("ホーム", lambda: _sched(on_home), default=True))
            items.append(_SEP)
        items += [
            _TrayItem("設定",     lambda: _sched(on_settings)),
            _TrayItem("認識履歴", lambda: _sched(on_history)),
        ]
        if on_dict:
            items.append(_TrayItem("個人辞書", lambda: _sched(on_dict)))
        if on_video_transcribe:
            items.append(_TrayItem("動画文字起こし", lambda: _sched(on_video_transcribe)))
        if on_float_toggle and get_float_visible:
            items.append(
                _TrayItem(
                    "フローティングボタン",
                    lambda: _sched(on_float_toggle),
                    checked=get_float_visible,
                )
            )
        items += [
            _SEP,
            _TrayItem("終了", lambda: _sched(on_quit)),
        ]

        self._tray = Win32TrayIcon(
            title="AirType - 無変換キーで録音",
            hicon=self._idle_hicon,
            menu_items=items,
            on_default=on_home,
            tk_schedule=lambda fn: root.after(0, fn),
        )
        self._tray.start()
        print("[TrayIcon] 起動完了 (Win32 ネイティブ)")

    def set_recording(self, recording: bool):
        """アイコンとツールチップを録音状態に合わせて切り替える"""
        if not self._tray:
            return
        hicon = self._rec_hicon if recording else self._idle_hicon
        tip   = "🎤 録音中..." if recording else "AirType - 無変換キーで録音"
        self._tray.update_icon(hicon)
        self._tray.update_tooltip(tip)

    def update_icon(self, path_str: str):
        """待機中アイコンをカスタム画像に差し替える（空文字でデフォルトに戻す）。"""
        if not self._tray:
            return
        custom = _load_custom_tray_icon(path_str) if path_str else None
        self._idle_pil   = custom if custom is not None else _make_icon(False)
        self._idle_hicon = pil_to_hicon(self._idle_pil)
        self._tray.update_icon(self._idle_hicon)

    def stop(self):
        if self._tray:
            try:
                self._tray.stop()
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
        get_backend: "Callable[[], str]" = lambda: "whisper",
        on_backend_change: "Callable[[str], None]" = lambda b: None,
        get_duck_mode: "Callable[[], str]" = lambda: "mute",
        on_duck_mode_change: "Callable[[str], None]" = lambda m: None,
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
        self._get_backend               = get_backend
        self._on_backend_change         = on_backend_change
        self._get_duck_mode             = get_duck_mode
        self._on_duck_mode_change       = on_duck_mode_change
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

        backend = self._get_backend()

        # ── 音声認識バックエンド選択 ──────────────────────────────────
        tk.Label(win, text="音声認識バックエンド:", font=("Yu Gothic UI", 10, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", **PAD
        )

        backend_var = tk.StringVar(value=backend)
        backend_frame = tk.Frame(win)
        backend_frame.grid(row=1, column=0, columnspan=2, sticky="w", padx=12, pady=(0, 4))

        _combo_ref = [None]  # combo が後から作られるため遅延参照

        def _on_backend_radio():
            c = _combo_ref[0]
            if c is None:
                return
            state = "disabled" if backend_var.get() == "sensevoice" else "readonly"
            c.configure(state=state)

        tk.Radiobutton(
            backend_frame, text="Whisper (whisper.cpp · Vulkan)",
            variable=backend_var, value="whisper",
            font=("Yu Gothic UI", 10), command=_on_backend_radio,
        ).pack(side=tk.LEFT, padx=(0, 16))
        tk.Radiobutton(
            backend_frame, text="SenseVoice Small (ONNX · DirectML)",
            variable=backend_var, value="sensevoice",
            font=("Yu Gothic UI", 10), command=_on_backend_radio,
        ).pack(side=tk.LEFT)

        ttk.Separator(win, orient="horizontal").grid(
            row=2, column=0, columnspan=2, sticky="ew", padx=12, pady=(4, 2)
        )

        # ── Whisper モデル選択 ────────────────────────────────────────
        tk.Label(win, text="Whisper モデル:", font=("Yu Gothic UI", 10, "bold")).grid(
            row=3, column=0, columnspan=2, sticky="w", **PAD
        )

        display_labels = []
        key_by_display: dict[str, str] = {}
        current_display = None

        for key, label in self._MODELS.items():
            path = _WHISPER_MODELS.get(key)
            display = label if (path and path.exists()) else label + "  [未インストール]"
            display_labels.append(display)
            key_by_display[display] = key
            if key == self._get_model():
                current_display = display

        if current_display is None:
            current_display = display_labels[0]

        model_var = tk.StringVar(value=current_display)
        combo_state = "disabled" if backend == "sensevoice" else "readonly"
        combo = ttk.Combobox(
            win, textvariable=model_var, values=display_labels,
            width=50, state=combo_state,
        )
        combo.grid(row=4, column=0, columnspan=2, sticky="ew", **PAD)
        _combo_ref[0] = combo

        tk.Label(
            win,
            text="[未インストール] のモデルはファイルをダウンロード後に使用できます",
            font=("Yu Gothic UI", 9),
            fg="#888888",
        ).grid(row=5, column=0, columnspan=2, sticky="w", padx=12, pady=(0, 6))

        # ── LLM テキスト整形 (Refiner) ON/OFF ────────────────────────
        ttk.Separator(win, orient="horizontal").grid(
            row=6, column=0, columnspan=2, sticky="ew", padx=12, pady=(6, 2)
        )

        refiner_var = tk.BooleanVar(value=self._get_use_refiner())
        tk.Checkbutton(
            win,
            text="LLM テキスト整形を使用する（句読点追加・フィラー除去）",
            variable=refiner_var,
            font=("Yu Gothic UI", 10),
        ).grid(row=7, column=0, columnspan=2, sticky="w", **PAD)

        tk.Label(
            win,
            text="OFF にすると Whisper の認識結果をそのまま出力します（より正確な場合があります）",
            font=("Yu Gothic UI", 9),
            fg="#888888",
        ).grid(row=8, column=0, columnspan=2, sticky="w", padx=12, pady=(0, 6))

        # ── ログイン時の自動起動 ──────────────────────────────────────
        ttk.Separator(win, orient="horizontal").grid(
            row=9, column=0, columnspan=2, sticky="ew", padx=12, pady=(6, 2)
        )

        import autostart as _autostart
        autostart_var = tk.BooleanVar(value=_autostart.is_enabled())
        tk.Checkbutton(
            win,
            text="ログイン時に AirType を自動起動する",
            variable=autostart_var,
            font=("Yu Gothic UI", 10),
        ).grid(row=10, column=0, columnspan=2, sticky="w", **PAD)

        tk.Label(
            win,
            text="有効にすると Windows ログイン時にバックグラウンドで自動起動します",
            font=("Yu Gothic UI", 9),
            fg="#888888",
        ).grid(row=11, column=0, columnspan=2, sticky="w", padx=12, pady=(0, 6))

        # ── 録音中のシステム音調整 ────────────────────────────────────
        ttk.Separator(win, orient="horizontal").grid(
            row=12, column=0, columnspan=2, sticky="ew", padx=12, pady=(6, 2)
        )

        tk.Label(win, text="録音中のシステム音調整:", font=("Yu Gothic UI", 10, "bold")).grid(
            row=13, column=0, columnspan=2, sticky="w", **PAD
        )

        duck_var = tk.StringVar(value=self._get_duck_mode())
        duck_frame = tk.Frame(win)
        duck_frame.grid(row=14, column=0, columnspan=2, sticky="w", padx=12, pady=(0, 4))
        for val, label in (
            ("duck", "音量を下げる（15%）"),
            ("mute", "ミュートにする"),
            ("off",  "無効"),
        ):
            tk.Radiobutton(
                duck_frame, text=label, variable=duck_var, value=val,
                font=("Yu Gothic UI", 10),
            ).pack(side=tk.LEFT, padx=(0, 14))

        tk.Label(
            win,
            text="録音中に他の音がマイクに混入するのを防ぎます",
            font=("Yu Gothic UI", 9),
            fg="#888888",
        ).grid(row=15, column=0, columnspan=2, sticky="w", padx=12, pady=(0, 6))

        # ── アイコン設定 ──────────────────────────────────────────────
        ttk.Separator(win, orient="horizontal").grid(
            row=16, column=0, columnspan=2, sticky="ew", padx=12, pady=(6, 2)
        )

        tk.Label(win, text="アイコン設定:", font=("Yu Gothic UI", 10, "bold")).grid(
            row=17, column=0, columnspan=2, sticky="w", **PAD
        )

        def _make_icon_row(parent, var, title, row_offset):
            tk.Label(parent, text=title, font=("Yu Gothic UI", 9, "bold")).grid(
                row=row_offset, column=0, columnspan=2, sticky="w", padx=12, pady=(4, 0)
            )
            frame = tk.Frame(parent)
            frame.grid(row=row_offset + 1, column=0, columnspan=2, sticky="ew", padx=12, pady=(2, 0))

            tk.Label(frame, textvariable=var, font=("Yu Gothic UI", 9),
                     fg="#444444", anchor="w", width=36).pack(side=tk.LEFT, fill=tk.X, expand=True)

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

            tk.Button(frame, text="参照...", width=8, command=_browse).pack(side=tk.LEFT, padx=(4, 2))
            tk.Button(frame, text="デフォルトに戻す", command=lambda v=var: v.set("")).pack(side=tk.LEFT, padx=(2, 0))

        tray_icon_var = tk.StringVar(value=self._get_icon_path())
        _make_icon_row(win, tray_icon_var, "トレイアイコン:", 18)

        shortcut_icon_var = tk.StringVar(value=self._get_shortcut_icon_path())
        _make_icon_row(win, shortcut_icon_var, "デスクトップショートカットアイコン:", 20)

        tk.Label(
            win,
            text="PNG / ICO 等に対応。白・単色の背景は自動で除去されます",
            font=("Yu Gothic UI", 9),
            fg="#888888",
        ).grid(row=22, column=0, columnspan=2, sticky="w", padx=12, pady=(2, 2))

        def _apply_ico_to_lnk(ico_path, lnk_paths):
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
        ).grid(row=23, column=0, columnspan=2, sticky="w", padx=12, pady=(2, 6))

        btn_frame = tk.Frame(win)
        btn_frame.grid(row=24, column=0, columnspan=2, pady=(4, 12))

        def apply():
            new_backend = backend_var.get()
            backend_changed = (new_backend != backend)

            selected_key = None
            if new_backend == "whisper":
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

            if autostart_var.get():
                _autostart.enable()
            else:
                _autostart.disable()
            self._on_refiner_change(refiner_var.get())
            self._on_duck_mode_change(duck_var.get())
            self._on_icon_change(tray_icon_var.get())
            self._on_shortcut_icon_change(shortcut_icon_var.get())
            if selected_key:
                self._on_apply(selected_key)
            if backend_changed:
                self._on_backend_change(new_backend)
                label = "SenseVoice" if new_backend == "sensevoice" else "Whisper"
                messagebox.showinfo(
                    "AirType",
                    f"音声認識バックエンドを {label} に変更しました。\n"
                    "設定を有効にするには AirType を再起動してください。",
                    parent=win,
                )
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
    動画ファイルを選択してチャンク分割しながら文字起こしするウィンドウ。
    ffmpeg で N 分ごとに音声抽出 → STT を繰り返す。キャンセル可能。
    show() はメインスレッドから呼ぶこと。
    """

    _VIDEO_INFER_TIMEOUT = 600

    def __init__(self, master: tk.Tk, transcriber, personal_dict=None, cfg: dict = None,
                 get_backend=None, whisper_cfg: dict = None):
        self._master        = master
        self._transcriber   = transcriber  # メイン STT（フォールバック用）
        self._personal_dict = personal_dict
        self._win           = None
        self._stop_event    = threading.Event()
        self._get_backend   = get_backend or (lambda: "whisper")
        self._whisper_cfg   = whisper_cfg or {}

        video_cfg = (cfg or {}).get("video", {})
        self._video_backend  = video_cfg.get("backend", "whisper")
        self._chunk_sec      = int(video_cfg.get(
            f"chunk_sec_{self._video_backend}",
            300 if self._video_backend == "whisper" else 30,
        ))
        self._max_duration   = int(video_cfg.get("max_duration_sec", 1800))

    def show(self):
        """ファイル選択ダイアログを開き、選択されたら文字起こしを開始する。"""
        if self._win and self._win.winfo_exists():
            self._win.lift()
            self._win.focus()
            return
        self._open_file_and_start()

    def _open_file_and_start(self):
        from pathlib import Path
        from tkinter import filedialog, messagebox
        from video_transcriber import find_ffmpeg, get_duration

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
            return

        # 動画文字起こし用トランスクライバーを決定
        transcriber, actual_backend = self._resolve_video_transcriber(messagebox)

        duration = get_duration(Path(video_path_str), ffmpeg)
        print(f"[Video] ファイル: {Path(video_path_str).name}  長さ: {duration:.1f}秒  "
              f"チャンク: {self._chunk_sec}秒  バックエンド: {actual_backend}")
        if duration == 0.0:
            print("[Video] 警告: 動画の長さを取得できませんでした。1チャンクで処理します。")
        if duration > self._max_duration:
            mins     = int(duration) // 60
            max_mins = self._max_duration // 60
            ok = messagebox.askyesno(
                "長い動画",
                f"この動画は約 {mins} 分あります（目安上限: {max_mins} 分）。\n"
                f"処理中はリソースを多く消費します。続行しますか？",
            )
            if not ok:
                return

        self._stop_event.clear()
        self._build_progress(video_path_str, ffmpeg, duration, transcriber)

    def _resolve_video_transcriber(self, messagebox):
        """動画文字起こし用トランスクライバーを返す。(transcriber, backend_name) のタプル。"""
        if self._video_backend != "whisper":
            return self._transcriber, self._video_backend

        # Whisper CLI モードで動画専用インスタンスを作成
        from pathlib import Path as _Path
        from step2_transcriber import WhisperTranscriber, DEFAULT_MODEL, _DEFAULT_WHISPER_DIR

        whisper_dir = _Path(self._whisper_cfg.get("dir", "")) or _DEFAULT_WHISPER_DIR
        model_key   = self._whisper_cfg.get("model_key", DEFAULT_MODEL)
        cli_exe     = whisper_dir / "whisper-cli.exe"
        server_exe  = whisper_dir / "whisper-server.exe"

        if not cli_exe.exists() and not server_exe.exists():
            messagebox.showwarning(
                "Whisper が見つかりません",
                f"whisper-cli.exe が {whisper_dir} に見つかりません。\n"
                f"SenseVoice で代替します（精度が低い場合があります）。",
            )
            return self._transcriber, self._get_backend()

        try:
            w = WhisperTranscriber(
                whisper_dir=whisper_dir,
                model=model_key,
                cli_only=True,
            )
            print(f"[Video] Whisper CLI モードで動画文字起こし: {w.model_path.name}")
            return w, "whisper"
        except Exception as e:
            messagebox.showwarning(
                "Whisper 初期化エラー",
                f"Whisper の初期化に失敗しました:\n{e}\n\nSenseVoice で代替します。",
            )
            return self._transcriber, self._get_backend()

    def _build_progress(self, video_path_str: str, ffmpeg: str, duration: float, transcriber=None):
        import math
        import tempfile
        from pathlib import Path
        from video_transcriber import extract_audio_chunk

        active_transcriber = transcriber or self._transcriber
        video_path  = Path(video_path_str)
        n_chunks    = max(1, math.ceil(duration / self._chunk_sec)) if duration > 0 else 1

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

        status_var = tk.StringVar(value="準備中...")
        tk.Label(
            win, textvariable=status_var,
            font=("Yu Gothic UI", 10), fg="#555555",
        ).pack(padx=24, pady=(0, 4))

        chunk_sec_label = (
            f"{self._chunk_sec}秒" if self._chunk_sec < 60
            else f"{self._chunk_sec // 60}分"
        )
        chunk_label = f"({chunk_sec_label}ごとに分割、全{n_chunks}チャンク)" if n_chunks > 1 else "(1チャンクで処理)"
        tk.Label(
            win,
            text=chunk_label,
            font=("Yu Gothic UI", 9), fg="#888888",
        ).pack(padx=24, pady=(0, 8))

        def _cancel():
            self._stop_event.set()
            cancel_btn.config(state=tk.DISABLED, text="キャンセル中...")

        cancel_btn = tk.Button(win, text="キャンセル", width=12, command=_cancel)
        cancel_btn.pack(pady=(0, 16))

        win.update_idletasks()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        w,  h  = win.winfo_reqwidth(),    win.winfo_reqheight()
        win.geometry(f"+{(sw - w) // 2}+{(sh - h) // 2}")

        def _work():
            parts    = []
            tmp_wav  = None
            print(f"[Video] 処理開始: {video_path.name}  n_chunks={n_chunks}")
            try:
                for i in range(n_chunks):
                    if self._stop_event.is_set():
                        win.after(0, lambda: self._show_cancelled(win))
                        return

                    start = i * self._chunk_sec
                    label = f"チャンク {i + 1}/{n_chunks}"
                    print(f"[Video] {label}: 開始={start}秒")

                    win.after(0, lambda s=label: status_var.set(f"音声抽出中... ({s})"))

                    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                        tmp_wav = Path(f.name)

                    extract_audio_chunk(
                        video_path, tmp_wav,
                        start_sec=start,
                        duration_sec=self._chunk_sec,
                        ffmpeg_exe=ffmpeg,
                    )

                    if self._stop_event.is_set():
                        win.after(0, lambda: self._show_cancelled(win))
                        return

                    win.after(0, lambda s=label: status_var.set(f"文字起こし中... ({s})"))
                    text = active_transcriber.transcribe(
                        tmp_wav,
                        infer_timeout=self._VIDEO_INFER_TIMEOUT,
                        force_cli=True,
                    )
                    if text.strip():
                        parts.append(text.strip())

                    tmp_wav.unlink(missing_ok=True)
                    tmp_wav = None

                full_text = "\n".join(parts)
                if self._personal_dict:
                    full_text = self._personal_dict.apply(full_text)

                win.after(0, lambda: self._show_result(win, video_path, full_text))

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

    def _show_cancelled(self, progress_win: tk.Toplevel):
        try:
            progress_win.grab_release()
            progress_win.destroy()
        except Exception:
            pass
        self._win = None

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


# ─────────────────────────────────────
# ホームウィンドウ
# ─────────────────────────────────────
class HomeWindow:
    """
    AirType メインウィンドウ。
    使用統計の表示と、トレイアイコンの全機能をここから操作できる。
    show() はメインスレッドから呼ぶこと。
    """

    _BG        = "#F7F7F7"
    _CARD_BG   = "white"
    _ACCENT    = "#1565C0"
    _FONT_H    = ("Yu Gothic UI", 15, "bold")
    _FONT_NUM  = ("Yu Gothic UI", 22, "bold")
    _FONT_UNIT = ("Yu Gothic UI", 9)
    _FONT_LBL  = ("Yu Gothic UI", 9)
    _FONT_BTN  = ("Yu Gothic UI", 10)

    def __init__(
        self,
        master: tk.Tk,
        stats=None,
        on_settings: Callable = None,
        on_history: Callable = None,
        on_dict: Callable = None,
        on_video: Callable = None,
        on_advisor: Callable = None,
        on_float_toggle: Callable = None,
        get_float_visible: "Callable[[], bool]" = None,
        on_quit: Callable = None,
    ):
        self._master           = master
        self._stats            = stats
        self._on_settings      = on_settings
        self._on_history       = on_history
        self._on_dict          = on_dict
        self._on_video         = on_video
        self._on_advisor       = on_advisor
        self._on_float_toggle  = on_float_toggle
        self._get_float_visible = get_float_visible or (lambda: False)
        self._on_quit          = on_quit
        self._win              = None
        self._stat_vars: dict[str, tk.StringVar] = {}
        self._float_var: tk.StringVar | None = None
        self._update_job       = None

    def show(self):
        if self._win and self._win.winfo_exists():
            self._win.lift()
            self._win.focus()
            return
        self._build()

    def _build(self):
        win = tk.Toplevel(self._master)
        win.title("AirType")
        win.configure(bg=self._BG)
        win.resizable(False, False)
        self._win = win

        # ── ヘッダー ──────────────────────────────────────────────
        hdr = tk.Frame(win, bg=self._ACCENT)
        hdr.pack(fill=tk.X)
        tk.Label(
            hdr, text="  🎤  AirType",
            font=self._FONT_H, fg="white", bg=self._ACCENT,
        ).pack(side=tk.LEFT, padx=12, pady=14)

        # ── 統計カードエリア ───────────────────────────────────────
        cards_outer = tk.Frame(win, bg=self._BG)
        cards_outer.pack(fill=tk.X, padx=14, pady=(14, 0))

        cards_info = [
            ("合計使用時間", "total_time", None),
            ("節約時間",     "saved_time", None),
            ("入力速度",     "wpm",        "WPM"),
            ("入力単語数",   "words",      "単語"),
        ]
        for i, (label, key, unit) in enumerate(cards_info):
            row, col = divmod(i, 2)
            card = tk.Frame(
                cards_outer, bg=self._CARD_BG,
                highlightthickness=1, highlightbackground="#DDDDDD",
            )
            card.grid(row=row, column=col, padx=5, pady=5, sticky="nsew",
                      ipadx=12, ipady=10)
            cards_outer.grid_columnconfigure(col, weight=1, uniform="col")

            num_var = tk.StringVar(value="--")
            self._stat_vars[key] = num_var

            num_row = tk.Frame(card, bg=self._CARD_BG)
            num_row.pack(anchor="w")
            tk.Label(
                num_row, textvariable=num_var,
                font=self._FONT_NUM, fg="#111111", bg=self._CARD_BG,
            ).pack(side=tk.LEFT)
            if unit:
                tk.Label(
                    num_row, text=f" {unit}",
                    font=self._FONT_UNIT, fg="#888888", bg=self._CARD_BG,
                ).pack(side=tk.LEFT, anchor="s", pady=(0, 4))
            tk.Label(
                card, text=label,
                font=self._FONT_LBL, fg="#888888", bg=self._CARD_BG,
            ).pack(anchor="w")

        # ── セパレーター ───────────────────────────────────────────
        ttk.Separator(win, orient="horizontal").pack(fill=tk.X, padx=14, pady=(14, 8))

        # ── アクションボタン（2列グリッド）────────────────────────
        act = tk.Frame(win, bg=self._BG)
        act.pack(fill=tk.X, padx=14)

        btn_defs = [
            ("⚙  設定",           self._on_settings),
            ("📋  認識履歴",        self._on_history),
        ]
        if self._on_dict:
            btn_defs.append(("📖  個人辞書", self._on_dict))
        if self._on_video:
            btn_defs.append(("🎬  動画文字起こし", self._on_video))
        if self._on_advisor:
            btn_defs.append(("🔍  最適設定診断", self._on_advisor))

        for i, (text, cmd) in enumerate(btn_defs):
            r, c = divmod(i, 2)
            tk.Button(
                act, text=text, font=self._FONT_BTN,
                command=cmd,
                relief=tk.FLAT, bg="#EBEBEB", activebackground="#D8D8D8",
                cursor="hand2", anchor="w", padx=10, pady=7,
            ).grid(row=r, column=c, padx=4, pady=3, sticky="ew")
            act.grid_columnconfigure(c, weight=1, uniform="col")

        # フローティングボタン（全幅トグル）
        if self._on_float_toggle:
            next_row = (len(btn_defs) + 1) // 2
            self._float_var = tk.StringVar(value=self._float_label())
            tk.Button(
                act,
                textvariable=self._float_var,
                font=self._FONT_BTN,
                command=self._do_float_toggle,
                relief=tk.FLAT, bg="#EBEBEB", activebackground="#D8D8D8",
                cursor="hand2", anchor="w", padx=10, pady=7,
            ).grid(row=next_row, column=0, columnspan=2,
                   padx=4, pady=3, sticky="ew")

        # ── フッター ──────────────────────────────────────────────
        ttk.Separator(win, orient="horizontal").pack(fill=tk.X, padx=14, pady=(12, 8))

        footer = tk.Frame(win, bg=self._BG)
        footer.pack(fill=tk.X, padx=14, pady=(0, 14))

        if self._on_quit:
            tk.Button(
                footer, text="終了", font=self._FONT_BTN,
                command=self._on_quit,
                relief=tk.FLAT, bg="#FFEBEE", fg="#B71C1C",
                activebackground="#FFCDD2", activeforeground="#B71C1C",
                cursor="hand2", padx=14, pady=6,
            ).pack(side=tk.RIGHT, padx=(6, 0))

        tk.Button(
            footer, text="閉じる", font=self._FONT_BTN,
            command=win.destroy,
            relief=tk.FLAT, bg="#EBEBEB", activebackground="#D8D8D8",
            cursor="hand2", padx=14, pady=6,
        ).pack(side=tk.RIGHT)

        # ── ウィンドウ中央配置 ────────────────────────────────────
        win.update_idletasks()
        sw = win.winfo_screenwidth()
        sh = win.winfo_screenheight()
        w  = win.winfo_reqwidth()
        h  = win.winfo_reqheight()
        win.geometry(f"+{(sw - w) // 2}+{(sh - h) // 2}")

        def _on_close():
            if self._update_job:
                try:
                    win.after_cancel(self._update_job)
                except Exception:
                    pass
                self._update_job = None
            self._win = None
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", _on_close)
        self._refresh_stats()

    # ── ヘルパー ──────────────────────────────────────────────────────────

    def _float_label(self) -> str:
        on = self._get_float_visible()
        return f"{'✓' if on else '○'}  フローティングボタン  {'ON' if on else 'OFF'}"

    def _do_float_toggle(self):
        if self._on_float_toggle:
            self._on_float_toggle()
        if self._float_var:
            self._float_var.set(self._float_label())

    def _refresh_stats(self):
        """2秒ごとに統計カードを更新する"""
        if not self._win or not self._win.winfo_exists():
            return
        if self._stats:
            s = self._stats
            self._stat_vars["total_time"].set(_fmt_duration(s.total_recording_sec))
            self._stat_vars["saved_time"].set(_fmt_duration(s.saved_sec))
            self._stat_vars["wpm"].set(f"{s.wpm:.0f}")
            self._stat_vars["words"].set(_fmt_count(s.total_words))
        if self._float_var:
            self._float_var.set(self._float_label())
        self._update_job = self._win.after(2000, self._refresh_stats)


# ─────────────────────────────────────
# 最適設定ウィザード
# ─────────────────────────────────────
class AdvisorWindow:
    """
    ハードウェア自動診断 + ユーザー問診から最適な設定を推奨するウィザード。
    show() はメインスレッドから呼ぶこと。
    """

    _FONT_H  = ("Yu Gothic UI", 11, "bold")
    _FONT_N  = ("Yu Gothic UI", 10)
    _FONT_S  = ("Yu Gothic UI",  9)
    _FONT_HW = ("Consolas",      9)

    def __init__(self, master: tk.Tk, cfg: dict):
        self._master = master
        self._cfg    = cfg
        self._win    = None

    def show(self):
        if self._win and self._win.winfo_exists():
            self._win.lift()
            self._win.focus()
            return
        self._build()

    def _build(self):
        win = tk.Toplevel(self._master)
        win.title("最適設定ウィザード")
        win.resizable(False, False)
        win.grab_set()
        self._win = win

        # ── ハード診断エリア ──────────────────────────────────────────
        hw_lf = ttk.LabelFrame(win, text="  ハードウェア診断  ", padding=8)
        hw_lf.pack(fill=tk.X, padx=14, pady=(14, 4))

        self._hw_text = tk.StringVar(value="診断中...")
        tk.Label(
            hw_lf, textvariable=self._hw_text,
            font=self._FONT_HW, justify=tk.LEFT, fg="#333333",
        ).pack(anchor="w")

        # ── 問診エリア・推奨エリア (検出後に表示) ─────────────────────
        self._qa_lf     = ttk.LabelFrame(win, text="  使用状況  ", padding=8)
        self._result_lf = ttk.LabelFrame(win, text="  推奨設定  ", padding=8)
        self._env_var   = tk.StringVar(value="normal")
        self._prio_var  = tk.StringVar(value="balance")
        self._lang_var  = tk.StringVar(value="ja")
        self._hw        = None
        self._inst      = None

        # 初期ウィンドウ中央配置
        win.update_idletasks()
        self._recentre()

        threading.Thread(target=self._detect_worker, daemon=True, name="HWDetect").start()

    def _detect_worker(self):
        try:
            import advisor as _adv
            hw   = _adv.detect_hardware()
            inst = _adv.check_installed(self._cfg)
        except Exception as e:
            print(f"[Advisor] 診断エラー: {e}")
            hw, inst = None, None
        if self._win and self._win.winfo_exists():
            self._win.after(0, lambda: self._on_detected(hw, inst))

    def _on_detected(self, hw, inst):
        if hw is None:
            self._hw_text.set("ハードウェア診断に失敗しました。")
            return

        self._hw   = hw
        self._inst = inst

        uv        = hw["unified_memory"]
        vram_note = f"(統合・実効 {hw['effective_vram_gb']:.1f} GB)" if uv else "(専用)"
        npu_text  = hw["npu_name"] if hw["npu_name"] else "未検出（将来対応予定）"
        cpu_text  = hw["cpu_name"] or "不明"
        if hw["cpu_cores"]:
            cpu_text += f"  {hw['cpu_cores']}コア / {hw['cpu_threads']}スレッド"
        lines = [
            f"GPU      : {hw['gpu_name']}",
            f"VRAM     : {hw['vram_gb']:.1f} GB {vram_note}",
            f"NPU      : {npu_text}",
            f"DirectML : {'対応' if hw['directml'] else '非対応'}"
            f"  /  Vulkan : {'対応' if hw['vulkan'] else '非対応'}",
            f"RAM      : {hw['ram_gb']:.1f} GB",
            f"CPU      : {cpu_text}",
        ]
        self._hw_text.set("\n".join(lines))
        self._build_qa()

    def _build_qa(self):
        qa = self._qa_lf
        qa.pack(fill=tk.X, padx=14, pady=4)

        def _radio_group(label, var, choices):
            tk.Label(qa, text=label, font=self._FONT_N).pack(anchor="w", pady=(6, 2))
            for text, val in choices:
                tk.Radiobutton(qa, text=text, variable=var, value=val, font=self._FONT_N).pack(
                    anchor="w", padx=16
                )

        _radio_group(
            "Q1. 主な使用場所は？", self._env_var,
            [("静かな場所", "quiet"), ("普通（多少の雑音）", "normal"), ("騒がしい場所（カフェ等）", "noisy")],
        )
        ttk.Separator(qa, orient="horizontal").pack(fill=tk.X, pady=4)
        _radio_group(
            "Q2. 何を優先しますか？", self._prio_var,
            [("速度重視（レスポンスを速く）", "speed"), ("バランス", "balance"), ("精度重視（認識精度を最大化）", "accuracy")],
        )
        ttk.Separator(qa, orient="horizontal").pack(fill=tk.X, pady=4)
        _radio_group(
            "Q3. 主な使用言語は？", self._lang_var,
            [("日本語中心", "ja"), ("日英混在", "mixed"), ("英語中心", "en")],
        )

        btn_row = tk.Frame(self._win)
        btn_row.pack(pady=(8, 4))
        tk.Button(
            btn_row, text="推奨設定を確認する", width=20,
            command=self._show_recommendation,
        ).pack()

        self._recentre()

    def _show_recommendation(self):
        import advisor as _adv
        answers = {
            "env":      self._env_var.get(),
            "priority": self._prio_var.get(),
            "language": self._lang_var.get(),
        }
        recipe = _adv.recommend(self._hw, answers, self._inst)

        result = self._result_lf
        for w in result.winfo_children():
            w.destroy()
        result.pack(fill=tk.X, padx=14, pady=(4, 14))

        if recipe is None:
            tk.Label(
                result,
                text=(
                    "推奨できるレシピが見つかりませんでした。\n"
                    "SenseVoice か Whisper のどちらかをインストールしてください。"
                ),
                font=self._FONT_N, fg="#C62828", justify=tk.LEFT,
            ).pack(anchor="w", padx=4, pady=8)
            tk.Button(result, text="閉じる", width=10, command=self._win.destroy).pack(pady=(4, 8))
            self._recentre()
            return

        tk.Label(
            result, text=f"推奨モード：{recipe['label']}",
            font=self._FONT_H, fg="#1565C0",
        ).pack(anchor="w", padx=4, pady=(4, 2))
        tk.Label(
            result, text=recipe["description"],
            font=self._FONT_S, fg="#555555", wraplength=400, justify=tk.LEFT,
        ).pack(anchor="w", padx=4, pady=(0, 6))

        cfg    = recipe["config"]
        back   = cfg.get("transcriber.backend",   "whisper")
        mk     = cfg.get("transcriber.model_key",  "")
        refine = cfg.get("refiner.enabled",         True)
        _MODEL_LABELS = {
            "kotoba-q5":   "Kotoba-Whisper v2.0 Q5（標準・約538MB）",
            "kotoba-full": "Kotoba-Whisper v2.0 完全版（高精度・約1.52GB）",
        }
        stt_text = (
            f"Whisper — {_MODEL_LABELS.get(mk, mk)}" if back == "whisper"
            else "SenseVoice Small（ONNX + DirectML）"
        )
        ref_text = "LLM 整形（llama.cpp）" if refine else "ルールベースのみ（高速）"

        tk.Label(
            result,
            text=f"STT : {stt_text}\n整形 : {ref_text}",
            font=self._FONT_HW, fg="#222222", justify=tk.LEFT,
            bg="#F0F4FF", relief=tk.SOLID, borderwidth=1,
            padx=10, pady=6,
        ).pack(fill=tk.X, padx=4, pady=(0, 4))

        tk.Label(
            result, text="※ 設定はアプリ再起動後に有効になります",
            font=self._FONT_S, fg="#888888",
        ).pack(anchor="w", padx=4, pady=(0, 6))

        btn_row = tk.Frame(result)
        btn_row.pack(pady=(2, 6))
        tk.Button(
            btn_row, text="この設定を適用する", width=18,
            bg="#1565C0", fg="white",
            activebackground="#0D47A1", activeforeground="white",
            command=lambda r=recipe: self._apply(r),
        ).pack(side=tk.LEFT, padx=6)
        tk.Button(
            btn_row, text="閉じる", width=10, command=self._win.destroy,
        ).pack(side=tk.LEFT, padx=6)

        self._recentre()

    def _apply(self, recipe: dict):
        from tkinter import messagebox
        import advisor as _adv
        if _adv.apply_recipe(recipe):
            messagebox.showinfo(
                "設定を適用しました",
                "設定を保存しました。\n\nAirType を再起動すると新しい設定が有効になります。",
                parent=self._win,
            )
        else:
            messagebox.showerror("保存失敗", "設定の保存に失敗しました。", parent=self._win)
        self._win.destroy()

    def _recentre(self):
        if not self._win or not self._win.winfo_exists():
            return
        self._win.update_idletasks()
        sw, sh = self._win.winfo_screenwidth(), self._win.winfo_screenheight()
        w, h   = self._win.winfo_reqwidth(),    self._win.winfo_reqheight()
        self._win.geometry(f"+{(sw - w) // 2}+{(sh - h) // 2}")


# ── フォーマットユーティリティ ─────────────────────────────────────────────

def _fmt_duration(sec: float) -> str:
    """秒数を 'Xh Ymin' / 'Ymin' / 'Zs' 形式に変換する"""
    sec = max(0, int(sec))
    total_min, s = divmod(sec, 60)
    h, m = divmod(total_min, 60)
    if h > 0:
        return f"{h}h {m}min"
    if m > 0:
        return f"{m}min"
    return f"{s}s"


def _fmt_count(n: int) -> str:
    """1000 以上の数を '4.2k' 形式に変換する"""
    if n >= 10000:
        return f"{n / 1000:.0f}k"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)
