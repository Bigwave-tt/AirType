"""
AirType - Windows ネイティブ実装のシステムトレイアイコン

pystray を使わず ctypes で Shell_NotifyIcon を直接操作することで、
右クリックメニュー表示時の「グルグルカーソル (IDC_APPSTARTING)」問題を回避する。

修正点:
  SetForegroundWindow の直後に SetCursor(IDC_ARROW) を呼ぶ。
  すべての Windows API に正しい argtypes / restype を設定し、
  引数のマーシャリング崩れ（メニューが細い矩形になる問題）を防ぐ。
"""

import ctypes
import ctypes.wintypes as W
import io
import struct
import threading
from dataclasses import dataclass
from typing import Callable

# ─── Windows API ──────────────────────────────────────────────────────────────

_u32 = ctypes.windll.user32
_g32 = ctypes.windll.gdi32
_k32 = ctypes.windll.kernel32
_sh  = ctypes.windll.shell32

# ── 定数 ────────────────────────────────────────────────────────────────────

_NIM_ADD     = 0x00
_NIM_MODIFY  = 0x01
_NIM_DELETE  = 0x02
_NIM_SETVERSION = 0x04

_NIF_MESSAGE = 0x01
_NIF_ICON    = 0x02
_NIF_TIP     = 0x04

# NOTIFYICON_VERSION_4: Windows Vista 以降の推奨バージョン
# lParam low word = イベント, high word = アイコンID
# wParam = カーソル座標 (low=X, high=Y)
_NOTIFYICON_VERSION_4 = 4

_WM_NULL          = 0x0000
_WM_DESTROY       = 0x0002
_WM_APP           = 0x8000
_WM_TRAY          = _WM_APP + 1
_WM_UPD_ICON      = _WM_APP + 2
_WM_UPD_TIP       = _WM_APP + 3

# VERSION_4 でのイベント（lParam low word）
_WM_CONTEXTMENU   = 0x007B   # 右クリック (v4)
_WM_LBUTTONUP     = 0x0202   # 左ボタン離放 (v0 シングルクリック)
_WM_LBUTTONDBLCLK = 0x0203   # 左ダブルクリック
_WM_RBUTTONUP     = 0x0205   # 右クリック (v0)
_NIN_SELECT       = 0x0400   # 左シングルクリック (v4)
_NIN_KEYSELECT    = 0x0401   # キーボード選択 (v4)

_MF_STRING    = 0x0000
_MF_SEPARATOR = 0x0800
_MF_CHECKED   = 0x0008
_MF_GRAYED    = 0x0001

_TPM_LEFTALIGN   = 0x0000
_TPM_RIGHTBUTTON = 0x0002
_TPM_RETURNCMD   = 0x0100

_IDC_ARROW = 32512

# ── 構造体 ──────────────────────────────────────────────────────────────────

_WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_longlong,
    W.HWND, W.UINT, W.WPARAM, W.LPARAM,
)


class _WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize",        W.UINT),
        ("style",         W.UINT),
        ("lpfnWndProc",   _WNDPROC),
        ("cbClsExtra",    ctypes.c_int),
        ("cbWndExtra",    ctypes.c_int),
        ("hInstance",     W.HINSTANCE),
        ("hIcon",         W.HICON),
        ("hCursor",       W.HANDLE),
        ("hbrBackground", W.HANDLE),
        ("lpszMenuName",  W.LPCWSTR),
        ("lpszClassName", W.LPCWSTR),
        ("hIconSm",       W.HICON),
    ]


class _NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize",           W.DWORD),
        ("hWnd",             W.HWND),
        ("uID",              W.UINT),
        ("uFlags",           W.UINT),
        ("uCallbackMessage", W.UINT),
        ("hIcon",            W.HICON),
        ("szTip",            ctypes.c_wchar * 128),
        ("dwState",          W.DWORD),
        ("dwStateMask",      W.DWORD),
        ("szInfo",           ctypes.c_wchar * 256),
        ("uVersion",         W.UINT),
        ("szInfoTitle",      ctypes.c_wchar * 64),
        ("dwInfoFlags",      W.DWORD),
        ("guidItem",         ctypes.c_byte * 16),
        ("hBalloonIcon",     W.HICON),
    ]


# ── argtypes / restype の設定 ─────────────────────────────────────────────────
# これを設定しないと ctypes がデフォルトの c_int で返値を切り詰め、
# 引数のマーシャリングが崩れてメニューが細い矩形になる。

_u32.CreateWindowExW.restype  = W.HWND
_u32.RegisterClassExW.restype = W.ATOM

# CreateIconFromResourceEx: restype を設定しないと 64-bit 環境で HICON が切り詰められる
_u32.CreateIconFromResourceEx.restype  = W.HICON
_u32.CreateIconFromResourceEx.argtypes = [
    ctypes.c_void_p,   # presbits (PBYTE)
    W.DWORD,           # dwResSize
    W.BOOL,            # fIcon
    W.DWORD,           # dwVer
    ctypes.c_int,      # cxDesired
    ctypes.c_int,      # cyDesired
    W.UINT,            # uFlags
]

_u32.CreatePopupMenu.restype  = W.HMENU
_u32.CreatePopupMenu.argtypes = []

_u32.AppendMenuW.restype  = W.BOOL
_u32.AppendMenuW.argtypes = [
    W.HMENU,            # hMenu
    W.UINT,             # uFlags
    ctypes.c_size_t,    # uIDNewItem (UINT_PTR: pointer-sized)
    W.LPCWSTR,          # lpNewItem  (c_wchar_p; None = NULL)
]

_u32.TrackPopupMenu.restype  = W.BOOL
_u32.TrackPopupMenu.argtypes = [
    W.HMENU,
    W.UINT,
    ctypes.c_int, ctypes.c_int,  # x, y
    ctypes.c_int,                # nReserved (must be 0)
    W.HWND,
    ctypes.c_void_p,             # prcRect (RECT*, can be NULL)
]

_u32.DestroyMenu.restype  = W.BOOL
_u32.DestroyMenu.argtypes = [W.HMENU]

_u32.SetForegroundWindow.restype  = W.BOOL
_u32.SetForegroundWindow.argtypes = [W.HWND]

# W.HCURSOR は Python の ctypes.wintypes に存在しないバージョンがあるため
# ctypes.c_void_p (= HANDLE) で代用する。
# LoadCursorW の第 2 引数は LPCWSTR または MAKEINTRESOURCE(n) 整数ポインタなので
# c_void_p を使い整数を直接渡せるようにする。
_u32.LoadCursorW.restype  = ctypes.c_void_p          # HCURSOR
_u32.LoadCursorW.argtypes = [W.HINSTANCE,
                               ctypes.c_void_p]        # LPCWSTR or MAKEINTRESOURCE(n)

_u32.SetCursor.restype  = ctypes.c_void_p             # 直前の HCURSOR
_u32.SetCursor.argtypes = [ctypes.c_void_p]           # HCURSOR

_u32.GetCursorPos.restype  = W.BOOL
_u32.GetCursorPos.argtypes = [ctypes.POINTER(W.POINT)]

_u32.PostMessageW.restype  = W.BOOL
_u32.PostMessageW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]

_u32.DefWindowProcW.restype  = ctypes.c_longlong
_u32.DefWindowProcW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]

_sh.Shell_NotifyIconW.restype  = W.BOOL
_sh.Shell_NotifyIconW.argtypes = [W.DWORD, ctypes.POINTER(_NOTIFYICONDATAW)]

# ── データクラス ─────────────────────────────────────────────────────────────

@dataclass
class MenuItem:
    label:    str
    callback: Callable | None = None
    checked:  Callable | None = None
    default:  bool            = False


SEPARATOR = MenuItem(label=None)


# ─── PIL Image → HICON 変換 ───────────────────────────────────────────────────

def pil_to_hicon(pil_image, size: int = 32) -> int:
    img = pil_image.resize((size, size)).convert("RGBA")
    buf = io.BytesIO()
    img.save(buf, format="ICO", sizes=[(size, size)])
    ico = buf.getvalue()

    # ICO ファイルから最初の画像エントリを取り出す
    # ICONDIRENTRY: 4 byte header + 2+2 word + 4+4 dword = 16 bytes
    # dwBytesInRes は offset 8、dwImageOffset は offset 12
    img_size = struct.unpack_from("<I", ico, 6 + 8)[0]
    img_off  = struct.unpack_from("<I", ico, 6 + 12)[0]
    img_data = ico[img_off: img_off + img_size]

    img_buf = (ctypes.c_ubyte * len(img_data))(*img_data)
    hicon = _u32.CreateIconFromResourceEx(
        ctypes.cast(img_buf, ctypes.c_void_p),
        len(img_data), True, 0x00030000, size, size, 0,
    )
    return hicon


# ─── Win32TrayIcon ────────────────────────────────────────────────────────────

class Win32TrayIcon:
    _CLASS_NAME        = "AirType_TrayWnd_v2"
    _TRAY_ID           = 1
    _WM_TASKBARCREATED = None

    def __init__(
        self,
        title:       str,
        hicon:       int,
        menu_items:  list,
        on_default:  Callable | None = None,
        tk_schedule: Callable | None = None,
    ):
        self._title       = title
        self._hicon       = hicon
        self._menu_items  = menu_items
        self._on_default  = on_default
        self._tk_schedule = tk_schedule or (lambda fn: fn())

        self._hwnd       = None
        self._nid        = None
        self._stop_event = threading.Event()
        self._thread     = threading.Thread(
            target=self._run, daemon=True, name="Win32Tray"
        )

    def start(self):
        self._thread.start()

    def stop(self):
        if self._hwnd:
            try:
                _u32.PostMessageW(self._hwnd, _WM_DESTROY, 0, 0)
            except Exception:
                pass
        self._stop_event.wait(timeout=3.0)

    def update_icon(self, hicon: int):
        self._hicon = hicon
        if self._hwnd:
            _u32.PostMessageW(self._hwnd, _WM_UPD_ICON, hicon, 0)

    def update_tooltip(self, text: str):
        self._title = text
        if self._hwnd:
            _u32.PostMessageW(self._hwnd, _WM_UPD_TIP, 0, 0)

    # ── 内部実装 ─────────────────────────────────────────────────────────────

    def _run(self):
        hinstance = _k32.GetModuleHandleW(None)
        self._wndproc_ref = _WNDPROC(self._wndproc)

        wc = _WNDCLASSEXW()
        wc.cbSize        = ctypes.sizeof(_WNDCLASSEXW)
        wc.lpfnWndProc   = self._wndproc_ref
        wc.hInstance     = hinstance
        wc.lpszClassName = self._CLASS_NAME
        _u32.RegisterClassExW(ctypes.byref(wc))

        self._hwnd = _u32.CreateWindowExW(
            0, self._CLASS_NAME, "AirType Tray",
            0, 0, 0, 0, 0, None, None, hinstance, None,
        )

        if Win32TrayIcon._WM_TASKBARCREATED is None:
            Win32TrayIcon._WM_TASKBARCREATED = _u32.RegisterWindowMessageW(
                "TaskbarCreated"
            )

        nid = _NOTIFYICONDATAW()
        nid.cbSize           = ctypes.sizeof(_NOTIFYICONDATAW)
        nid.hWnd             = self._hwnd
        nid.uID              = self._TRAY_ID
        nid.uFlags           = _NIF_MESSAGE | _NIF_ICON | _NIF_TIP
        nid.uCallbackMessage = _WM_TRAY
        nid.hIcon            = self._hicon
        nid.szTip            = self._title[:127]
        _sh.Shell_NotifyIconW(_NIM_ADD, ctypes.byref(nid))
        self._nid = nid

        # Windows Vista 以降推奨の VERSION_4 を設定
        nid_v = _NOTIFYICONDATAW()
        nid_v.cbSize    = ctypes.sizeof(_NOTIFYICONDATAW)
        nid_v.hWnd      = self._hwnd
        nid_v.uID       = self._TRAY_ID
        nid_v.uVersion  = _NOTIFYICON_VERSION_4
        _sh.Shell_NotifyIconW(_NIM_SETVERSION, ctypes.byref(nid_v))

        msg = W.MSG()
        while _u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            _u32.TranslateMessage(ctypes.byref(msg))
            _u32.DispatchMessageW(ctypes.byref(msg))

        if self._nid:
            self._nid.uFlags = 0
            _sh.Shell_NotifyIconW(_NIM_DELETE, ctypes.byref(self._nid))
        _u32.DestroyWindow(self._hwnd)
        _u32.UnregisterClassW(self._CLASS_NAME, hinstance)
        self._stop_event.set()

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            return self._wndproc_impl(hwnd, msg, wparam, lparam)
        except Exception as e:
            import traceback
            print(f"[TrayIcon] _wndproc 例外 msg=0x{msg:04X}: {e}")
            traceback.print_exc()
            return _u32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _wndproc_impl(self, hwnd, msg, wparam, lparam):
        if msg == _WM_TRAY:
            # VERSION_4: lParam low word = イベント
            event = lparam & 0xFFFF
            if event in (_WM_CONTEXTMENU, _WM_RBUTTONUP):
                self._show_menu()
                return 0
            elif event in (_NIN_SELECT, _NIN_KEYSELECT,
                           _WM_LBUTTONUP, _WM_LBUTTONDBLCLK):
                if self._on_default:
                    self._tk_schedule(self._on_default)
                return 0

        elif msg == _WM_UPD_ICON:
            if self._nid:
                self._nid.hIcon  = wparam
                self._nid.uFlags = _NIF_ICON
                _sh.Shell_NotifyIconW(_NIM_MODIFY, ctypes.byref(self._nid))
            return 0

        elif msg == _WM_UPD_TIP:
            if self._nid:
                self._nid.szTip  = self._title[:127]
                self._nid.uFlags = _NIF_TIP
                _sh.Shell_NotifyIconW(_NIM_MODIFY, ctypes.byref(self._nid))
            return 0

        elif msg == _WM_DESTROY:
            _u32.PostQuitMessage(0)
            return 0

        elif Win32TrayIcon._WM_TASKBARCREATED and msg == Win32TrayIcon._WM_TASKBARCREATED:
            if self._nid:
                self._nid.uFlags = _NIF_MESSAGE | _NIF_ICON | _NIF_TIP
                _sh.Shell_NotifyIconW(_NIM_ADD, ctypes.byref(self._nid))
            return 0

        return _u32.DefWindowProcW(hwnd, msg, wparam, lparam)

    # ── ここまで _wndproc_impl ────────────────────────────────────────────

    def _show_menu(self):
        """
        右クリックメニューを表示する。
        SetForegroundWindow の直後に SetCursor(IDC_ARROW) を呼び
        IDC_APPSTARTING（グルグルカーソル）を即座に解除する。
        """
        try:
            self._show_menu_impl()
        except Exception as e:
            import traceback
            print(f"[TrayIcon] _show_menu エラー: {e}")
            traceback.print_exc()

    def _show_menu_impl(self):
        hmenu = _u32.CreatePopupMenu()
        if not hmenu:
            print("[TrayIcon] CreatePopupMenu 失敗")
            return

        cmd_map: dict[int, Callable] = {}
        cmd_id = 100

        for item in self._menu_items:
            if item is SEPARATOR or item.label is None:
                _u32.AppendMenuW(hmenu, _MF_SEPARATOR, 0, None)
            else:
                flags = _MF_STRING
                if item.checked and item.checked():
                    flags |= _MF_CHECKED
                # Python str を直接渡す。argtypes の LPCWSTR (c_wchar_p) は
                # Python str を自動マーシャリングできる。
                # create_unicode_buffer (c_wchar Array) は c_wchar_p.from_param が
                # 型不一致で例外を出す場合があるため使わない。
                ok = _u32.AppendMenuW(hmenu, flags, cmd_id, item.label)
                if not ok:
                    print(f"[TrayIcon] AppendMenuW 失敗: {item.label!r}")
                if item.callback:
                    cmd_map[cmd_id] = item.callback
                cmd_id += 1

        pt = W.POINT()
        _u32.GetCursorPos(ctypes.byref(pt))

        # ─ グルグルカーソル防止 ────────────────────────────────────────────
        _u32.SetForegroundWindow(self._hwnd)
        # _IDC_ARROW は Python int。argtypes[1] = c_void_p は
        # Python int をポインタ値として受け付ける（MAKEINTRESOURCE 相当）。
        _u32.SetCursor(_u32.LoadCursorW(None, _IDC_ARROW))
        # ───────────────────────────────────────────────────────────────────

        chosen = _u32.TrackPopupMenu(
            hmenu,
            _TPM_LEFTALIGN | _TPM_RIGHTBUTTON | _TPM_RETURNCMD,
            pt.x, pt.y,
            0,           # nReserved (must be 0)
            self._hwnd,
            None,
        )
        _u32.PostMessageW(self._hwnd, _WM_NULL, 0, 0)
        _u32.DestroyMenu(hmenu)

        if chosen and chosen in cmd_map:
            cmd_map[chosen]()
