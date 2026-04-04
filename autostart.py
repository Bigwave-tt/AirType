"""
AirType - ログイン時の自動起動管理

Windows レジストリ (HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run) に
AirType_launcher.vbs へのエントリを追加・削除することで自動起動を制御する。

- 登録コマンド: wscript.exe "<launcher_path>"
- 対象ユーザー: 現在のユーザーのみ (管理者権限不要)
"""

import winreg
from pathlib import Path

_REG_KEY  = r"Software\Microsoft\Windows\CurrentVersion\Run"
_REG_NAME = "AirType"
_LAUNCHER = Path(__file__).parent / "AirType_launcher.vbs"


def _reg_value() -> str:
    """レジストリに登録するコマンド文字列を返す。"""
    return f'wscript.exe "{_LAUNCHER}"'


def is_enabled() -> bool:
    """自動起動が有効かどうかを返す。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REG_KEY) as key:
            winreg.QueryValueEx(key, _REG_NAME)
            return True
    except FileNotFoundError:
        return False
    except Exception as e:
        print(f"[Autostart] 状態確認失敗: {e}")
        return False


def enable() -> bool:
    """自動起動を有効にする。成功したら True を返す。"""
    if not _LAUNCHER.exists():
        print(f"[Autostart] ランチャーが見つかりません: {_LAUNCHER}")
        return False
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _REG_KEY,
            access=winreg.KEY_SET_VALUE,
        ) as key:
            winreg.SetValueEx(key, _REG_NAME, 0, winreg.REG_SZ, _reg_value())
        print(f"[Autostart] 自動起動を有効にしました")
        return True
    except Exception as e:
        print(f"[Autostart] 有効化失敗: {e}")
        return False


def disable() -> bool:
    """自動起動を無効にする。成功したら True を返す。"""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _REG_KEY,
            access=winreg.KEY_SET_VALUE,
        ) as key:
            winreg.DeleteValue(key, _REG_NAME)
        print(f"[Autostart] 自動起動を無効にしました")
        return True
    except FileNotFoundError:
        return True  # すでに存在しない = 無効状態なので成功とみなす
    except Exception as e:
        print(f"[Autostart] 無効化失敗: {e}")
        return False
