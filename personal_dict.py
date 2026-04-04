"""
AirType - 個人辞書

音声認識後のテキストに対して、ユーザー定義の変換ルールを適用する。

- 辞書ファイル: airtype_dict.json (アプリと同じフォルダ)
- 形式: {"変換前": "変換後", ...}
- 適用: LLM 整形後に単純な文字列置換で適用
- スレッドセーフ: lock で保護
"""

import json
import threading
from pathlib import Path

_DICT_FILE = Path(__file__).parent / "airtype_dict.json"


class PersonalDict:
    """
    個人辞書の管理クラス。

    Parameters
    ----------
    path : Path
        辞書 JSON ファイルのパス。省略時はアプリと同フォルダの airtype_dict.json。
    """

    def __init__(self, path: Path = _DICT_FILE):
        self._path  = path
        self._lock  = threading.Lock()
        self._entries: dict[str, str] = {}
        self._load()

    # ── 辞書 I/O ─────────────────────────────────────────────────────
    def _load(self):
        if not self._path.exists():
            return
        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._entries = {str(k): str(v) for k, v in data.items()}
            print(f"[Dict] 辞書を読み込みました ({len(self._entries)} 件): {self._path.name}")
        except Exception as e:
            print(f"[Dict] 辞書読み込み失敗: {e}")

    def _save(self):
        try:
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(self._entries, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[Dict] 辞書保存失敗: {e}")

    # ── 公開 API ─────────────────────────────────────────────────────
    def apply(self, text: str) -> str:
        """辞書の変換ルールをテキストに適用する。"""
        with self._lock:
            entries = dict(self._entries)
        for from_text, to_text in entries.items():
            text = text.replace(from_text, to_text)
        return text

    def add(self, from_text: str, to_text: str) -> bool:
        """
        変換ルールを追加（または上書き）して保存する。

        Returns
        -------
        bool
            from_text が空でない場合に True。
        """
        from_text = from_text.strip()
        to_text   = to_text.strip()
        if not from_text:
            return False
        with self._lock:
            self._entries[from_text] = to_text
            self._save()
        print(f"[Dict] 登録: {from_text!r} → {to_text!r}")
        return True

    def remove(self, from_text: str):
        """変換ルールを削除して保存する。"""
        with self._lock:
            if from_text in self._entries:
                del self._entries[from_text]
                self._save()
                print(f"[Dict] 削除: {from_text!r}")

    def entries(self) -> dict[str, str]:
        """現在の辞書エントリをコピーして返す。"""
        with self._lock:
            return dict(self._entries)
