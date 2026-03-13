"""
AirType - IME 制御の抽象化レイヤー

OS 判定とプラットフォーム固有の IME 制御ロジックを分離する。
Windows 以外は no-op の基底クラスを返すことで、
将来的なクロスプラットフォーム対応の拡張点として機能する。

使用例:
    from ime import get_controller
    ime = get_controller()
    prev = ime.get_state()   # True/False/None
    ime.set_state(False)     # IME を無効化
    ime.set_state(prev)      # 元の状態に戻す
"""

import sys


class ImeController:
    """IME 制御のベースクラス（no-op 実装）。Windows 以外のプラットフォームで使用。"""

    def get_state(self) -> bool | None:
        """現在の IME 状態を返す。取得できない場合は None。"""
        return None

    def set_state(self, enabled: bool) -> None:
        """IME の状態を設定する。"""
        pass


class WindowsImeController(ImeController):
    """Windows IMM32 API を使用した IME 制御実装。"""

    def get_state(self) -> bool | None:
        try:
            import ctypes
            hwnd  = ctypes.windll.user32.GetForegroundWindow()
            himc  = ctypes.windll.imm32.ImmGetContext(hwnd)
            state = ctypes.windll.imm32.ImmGetOpenStatus(himc)
            ctypes.windll.imm32.ImmReleaseContext(hwnd, himc)
            return bool(state)
        except Exception:
            return None

    def set_state(self, enabled: bool) -> None:
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            himc = ctypes.windll.imm32.ImmGetContext(hwnd)
            ctypes.windll.imm32.ImmSetOpenStatus(himc, 1 if enabled else 0)
            ctypes.windll.imm32.ImmReleaseContext(hwnd, himc)
        except Exception:
            pass


def get_controller() -> ImeController:
    """
    現在の OS に対応する ImeController インスタンスを返す。

    Windows : WindowsImeController (IMM32 API による実装)
    その他  : ImeController (no-op)
    """
    if sys.platform == "win32":
        return WindowsImeController()
    return ImeController()
