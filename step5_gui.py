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
        on_dict: Callable = None,
        on_video_transcribe: Callable = None,
    ):
        self._root = root
        self._icon = None

        if not _HAS_TRAY:
            return

        self._idle_icon = _make_icon(False)
        self._rec_icon  = _make_icon(True)

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

        btn_frame = tk.Frame(win)
        btn_frame.grid(row=9, column=0, columnspan=2, pady=(4, 12))

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
                    tmp_wav, infer_timeout=self._VIDEO_INFER_TIMEOUT
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
        w,  h  = win.winfo_reqwidth(),    win.winfo_reqheight()
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
