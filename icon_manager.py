"""
AirType - カスタムアイコン管理

- 背景自動除去 (4隅フラッドフィル)
- トレイアイコン用に画像を 64×64 RGBA に変換
- デスクトップショートカット用に .ico を生成
- .lnk ショートカットのアイコンを更新 (pywin32 使用)
"""

from pathlib import Path

_HERE       = Path(__file__).parent
_CUSTOM_ICO = _HERE / "custom_icon.ico"
_ICO_SIZES  = [16, 32, 48, 64, 128, 256]
_TRAY_SIZE  = 64


# ─────────────────────────────────────
# 背景除去
# ─────────────────────────────────────

def _has_transparency(img) -> bool:
    """画像にすでに透明ピクセルがあるか確認。"""
    if img.mode == "RGBA":
        return img.getextrema()[3][0] < 255
    return False


def auto_remove_background(img, tolerance: int = 30):
    """
    4隅の色を背景色とみなし、フラッドフィルで透明化する。
    すでに透明部分がある画像はそのまま返す。
    白い背景の丸アイコンなどで背景が自動的に取り除かれる。
    """
    if _has_transparency(img):
        return img

    from collections import deque
    img = img.convert("RGBA")
    w, h = img.size

    # 4隅の平均色を背景色として推定
    corners = [img.getpixel((0, 0)), img.getpixel((w - 1, 0)),
               img.getpixel((0, h - 1)), img.getpixel((w - 1, h - 1))]
    bg = tuple(sum(c[i] for c in corners) // 4 for i in range(3))

    pixels = list(img.getdata())
    visited = bytearray(w * h)
    th = tolerance * 3

    queue = deque()
    for sx, sy in [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]:
        i = sy * w + sx
        if not visited[i]:
            visited[i] = 1
            queue.append((sx, sy))

    while queue:
        x, y = queue.popleft()
        px = pixels[y * w + x]
        if abs(px[0] - bg[0]) + abs(px[1] - bg[1]) + abs(px[2] - bg[2]) > th:
            continue
        pixels[y * w + x] = (px[0], px[1], px[2], 0)
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            nx, ny = x + dx, y + dy
            if 0 <= nx < w and 0 <= ny < h:
                ni = ny * w + nx
                if not visited[ni]:
                    visited[ni] = 1
                    queue.append((nx, ny))

    result = img.copy()
    result.putdata(pixels)
    return result


# ─────────────────────────────────────
# トレイアイコン変換
# ─────────────────────────────────────

def make_tray_image(path_str: str):
    """
    画像ファイルを 64×64 RGBA PIL Image に変換して返す。
    背景が単色（白など）の場合は自動で除去する。失敗時は None。
    """
    try:
        from PIL import Image
        img = Image.open(path_str).convert("RGBA")
        img = auto_remove_background(img)
        img.thumbnail((_TRAY_SIZE, _TRAY_SIZE), Image.LANCZOS)
        canvas = Image.new("RGBA", (_TRAY_SIZE, _TRAY_SIZE), (0, 0, 0, 0))
        offset = ((_TRAY_SIZE - img.width) // 2, (_TRAY_SIZE - img.height) // 2)
        canvas.paste(img, offset, img)
        return canvas
    except Exception as e:
        print(f"[IconManager] トレイアイコン変換失敗: {e}")
        return None


# ─────────────────────────────────────
# ICO 生成
# ─────────────────────────────────────

def generate_ico(source_path_str: str) -> Path:
    """
    source_path の画像を複数サイズの .ico に変換して保存し、そのパスを返す。
    背景が単色の場合は自動で除去する。
    """
    from PIL import Image
    img = Image.open(source_path_str).convert("RGBA")
    img = auto_remove_background(img)
    img.save(_CUSTOM_ICO, format="ICO", sizes=[(_s, _s) for _s in _ICO_SIZES])
    print(f"[IconManager] ICO 生成: {_CUSTOM_ICO.name}")
    return _CUSTOM_ICO


# ─────────────────────────────────────
# ショートカット更新
# ─────────────────────────────────────

def _get_desktop_path() -> Path:
    """レジストリから実際のデスクトップパスを取得する（OneDrive デスクトップに対応）。"""
    import winreg
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders",
        ) as key:
            return Path(winreg.QueryValueEx(key, "Desktop")[0])
    except Exception:
        import os
        return Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop"


def find_desktop_shortcuts(name_filter: str = "AirType") -> list:
    """
    デスクトップ・スタートメニューにある AirType の .lnk ファイルを探す。
    OneDrive デスクトップにも対応。name_filter に一致するショートカットのみ返す。
    """
    import os
    search_dirs = [
        _get_desktop_path(),
        Path(os.environ.get("PUBLIC", "C:/Users/Public")) / "Desktop",
        Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
    ]
    found = []
    for d in search_dirs:
        if not d.exists():
            continue
        for lnk in d.glob("*.lnk"):
            if name_filter.lower() in lnk.stem.lower():
                found.append(lnk)
    return found


def update_shortcut_icon(lnk_path: Path, ico_path: Path) -> bool:
    """ショートカット (.lnk) のアイコンを ico_path に変更する。pywin32 が必要。"""
    try:
        import pythoncom
        from win32com.shell import shell, shellcon
        pythoncom.CoInitialize()
        lnk = pythoncom.CoCreateInstance(
            shell.CLSID_ShellLink, None,
            pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IShellLink,
        )
        persist = lnk.QueryInterface(pythoncom.IID_IPersistFile)
        persist.Load(str(lnk_path))
        lnk.SetIconLocation(str(ico_path.resolve()), 0)
        persist.Save(str(lnk_path), True)

        # エクスプローラーのアイコンキャッシュを即時リフレッシュ
        try:
            shell.SHChangeNotify(shellcon.SHCNE_ASSOCCHANGED, shellcon.SHCNF_IDLIST, None, None)
        except Exception:
            pass
        return True
    except ImportError:
        print("[IconManager] pywin32 が未インストールです: pip install pywin32")
        return False
    except Exception as e:
        print(f"[IconManager] ショートカット更新失敗 ({lnk_path.name}): {e}")
        return False


def update_all_shortcuts(ico_path: Path) -> int:
    """見つかった全 AirType ショートカットのアイコンを更新し、更新数を返す。"""
    count = 0
    for lnk in find_desktop_shortcuts():
        if update_shortcut_icon(lnk, ico_path):
            count += 1
            print(f"[IconManager] ショートカット更新: {lnk.name}")
    return count
