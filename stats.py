"""AirType - 使用統計の追跡・永続化"""

import json
import threading
from datetime import datetime
from pathlib import Path


class Stats:
    """音声入力の使用統計を追跡・保存するクラス。スレッドセーフ。"""

    _FILE = Path(__file__).parent / "airtype_stats.json"
    _TYPING_WPM = 40.0  # 想定タイピング速度（単語/分）

    def __init__(self):
        self._lock = threading.Lock()
        self._data = self._load()
        self._rec_start: datetime | None = None

    # ── 永続化 ───────────────────────────────────────────────────────────

    def _load(self) -> dict:
        try:
            if self._FILE.exists():
                data = json.loads(self._FILE.read_text(encoding="utf-8"))
                data.setdefault("total_recording_sec", 0.0)
                data.setdefault("total_words", 0)
                return data
        except Exception:
            pass
        return {"total_recording_sec": 0.0, "total_words": 0}

    def _save(self):
        try:
            self._FILE.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            print(f"[Stats] 保存失敗: {e}")

    # ── イベント通知 ──────────────────────────────────────────────────────

    def recording_started(self):
        """録音開始時に呼ぶ"""
        self._rec_start = datetime.now()

    def recording_stopped(self):
        """録音停止時に呼ぶ（使用時間を加算して保存）"""
        if self._rec_start is None:
            return
        elapsed = (datetime.now() - self._rec_start).total_seconds()
        self._rec_start = None
        with self._lock:
            self._data["total_recording_sec"] = (
                self._data.get("total_recording_sec", 0.0) + elapsed
            )
            self._save()

    def text_added(self, text: str):
        """認識テキストが確定したときに単語数を加算して保存する"""
        words = _count_words(text)
        if words <= 0:
            return
        with self._lock:
            self._data["total_words"] = self._data.get("total_words", 0) + words
            self._save()

    # ── 集計プロパティ ────────────────────────────────────────────────────

    @property
    def total_recording_sec(self) -> float:
        return self._data.get("total_recording_sec", 0.0)

    @property
    def total_words(self) -> int:
        return self._data.get("total_words", 0)

    @property
    def wpm(self) -> float:
        """平均入力速度（単語/分）"""
        rec_min = self.total_recording_sec / 60.0
        if rec_min <= 0:
            return 0.0
        return self.total_words / rec_min

    @property
    def saved_sec(self) -> float:
        """節約時間（秒）= タイピングに要する推定時間 - 実際の録音時間"""
        if self.total_words <= 0:
            return 0.0
        typing_sec = (self.total_words / self._TYPING_WPM) * 60.0
        return max(0.0, typing_sec - self.total_recording_sec)


# ── ユーティリティ ─────────────────────────────────────────────────────────


def _count_words(text: str) -> int:
    """テキストの単語数を推定する。日本語は 2 文字 = 1 単語として計算。"""
    if not text:
        return 0
    stripped = text.strip()
    if not stripped:
        return 0

    non_ascii_alpha = sum(1 for c in stripped if ord(c) >= 128 and not c.isspace())
    total_alnum = sum(1 for c in stripped if not c.isspace())

    if total_alnum == 0:
        return 0

    if total_alnum > 0 and non_ascii_alpha / total_alnum > 0.3:
        # 日本語/CJK 主体: 2 文字 = 1 単語
        return max(1, non_ascii_alpha // 2)
    else:
        # 英語主体: スペース区切りで単語を数える
        return max(1, len(stripped.split()))
