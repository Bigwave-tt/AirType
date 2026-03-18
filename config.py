"""
AirType - 設定ローダー

airtype_config.json から設定を読み込み、デフォルト値とマージして提供する。
設定ファイルが存在しない場合はデフォルト値のみで動作する。

設定ファイルのパス: AirType フォルダ内の airtype_config.json
"""

import copy
import json
import socket
from pathlib import Path

_HERE       = Path(__file__).parent
CONFIG_PATH = _HERE / "airtype_config.json"

# ─────────────────────────────────────
# デフォルト設定
# ─────────────────────────────────────
_DEFAULTS: dict = {
    "network": {
        "server_url":      "http://YOUR_SERVER_IP:8000/dictate",  # client.py 用
        "host":            "0.0.0.0",   # main.py / api_server.py のリッスンアドレス
        "port":            8000,         # main.py / api_server.py のポート
        "serve":           False,        # True = main.py で API サーバーも起動する
        "api_key":         "",           # 空文字 = 認証なし
        "request_timeout": 60,           # client.py のリクエストタイムアウト (秒)
    },
    "whisper": {
        "dir":         "",      # 空文字 = デフォルト相対パスを使用
        "server_port": 18766,   # 0 = 空きポートを自動割り当て
    },
    "llama": {
        "dir":         "",      # 空文字 = デフォルト相対パスを使用
        "server_port": 18765,   # 0 = 空きポートを自動割り当て
    },
}


# ─────────────────────────────────────
# 内部ユーティリティ
# ─────────────────────────────────────
def _deep_merge(base: dict, override: dict) -> dict:
    """override の値を base に再帰的にマージする（base を変更して返す）。"""
    for k, v in override.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


# ─────────────────────────────────────
# 公開 API
# ─────────────────────────────────────
def load() -> dict:
    """
    airtype_config.json を読み込み、デフォルト値とマージして返す。
    ファイルが存在しない・読み込めない場合はデフォルト値を返す。
    """
    cfg = copy.deepcopy(_DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                user = json.load(f)
            _deep_merge(cfg, user)
            print(f"[Config] 設定を読み込みました: {CONFIG_PATH.name}")
        except Exception as e:
            print(f"[Config] 設定ファイル読み込みエラー: {e}。デフォルト値を使用します。")
    return cfg


def find_free_port() -> int:
    """OS に空きポートを割り当ててもらう。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def resolve_port(configured: int) -> int:
    """0 の場合は空きポートを自動割り当て、それ以外はそのまま返す。"""
    if configured == 0:
        port = find_free_port()
        print(f"[Config] ポート自動割り当て: {port}")
        return port
    return configured


def resolve_dir(configured: str, default_subdir: str) -> Path:
    """
    設定された dir 文字列を Path に変換する。
    空文字の場合は config.py の親フォルダから default_subdir をたどるデフォルトを返す。
    """
    if configured:
        return Path(configured)
    return _HERE.parent / default_subdir
