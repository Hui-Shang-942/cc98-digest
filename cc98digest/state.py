"""运行状态：记录"同一个主题帖是否已经报告过"，用于重跑时的幂等标注。

state.json 体积必须可控：条目超过 STATE_KEEP_DAYS 天后自动清理。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .logging_setup import get_logger
from .window import SHANGHAI

log = get_logger("state")

# state.json 里只留最近几天的"已报告"记录：
# 24 小时窗口决定了更早的记录永远不会再被比对到，留着只会白占空间。
# 与 output.keep_days（报告保留 3 天）保持一致。
STATE_KEEP_DAYS = 3


class StateStore:
    def __init__(self, path: Path):
        self.path = path
        self.last_run_at: str | None = None
        self.reported: dict[str, dict[str, Any]] = {}

    @classmethod
    def load(cls, path: Path) -> "StateStore":
        store = cls(path)
        if not path.exists():
            return store
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("状态文件 %s 无法读取（%s），按全新状态处理。", path, exc)
            return store
        if not isinstance(data, dict):
            return store
        store.last_run_at = data.get("last_run_at")
        reported = data.get("reported_topics")
        if isinstance(reported, dict):
            store.reported = {str(k): v for k, v in reported.items() if isinstance(v, dict)}
        return store

    def was_reported(self, topic_id: int, keywords: list[str] | None = None) -> bool:
        """该帖之前报告过；给了 keywords 时还要求关键词有交集（避免不同关键词互相污染）。"""
        entry = self.reported.get(str(topic_id))
        if not entry:
            return False
        if not keywords:
            return True
        old = set(entry.get("keywords") or [])
        return bool(old.intersection(keywords))

    def record(self, topic_id: int, keywords: list[str], run_at: datetime) -> None:
        key = str(topic_id)
        entry = self.reported.get(key) or {}
        merged = sorted(set(entry.get("keywords") or []).union(keywords))
        self.reported[key] = {
            "keywords": merged,
            "first_seen": entry.get("first_seen") or run_at.isoformat(),
            "reported_at": run_at.isoformat(),
        }

    def prune(self, now: datetime, keep_days: int = STATE_KEEP_DAYS) -> int:
        cutoff = now - timedelta(days=keep_days)
        removed = 0
        for key, entry in list(self.reported.items()):
            stamp = entry.get("reported_at")
            try:
                when = datetime.fromisoformat(str(stamp))
            except (TypeError, ValueError):
                when = None
            if when is None or when < cutoff:
                del self.reported[key]
                removed += 1
        return removed

    def save(self, run_at: datetime) -> None:
        self.last_run_at = run_at.isoformat()
        payload = {
            "last_run_at": self.last_run_at,
            "reported_topics": self.reported,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)  # 原子替换，避免半截文件
        except OSError as exc:
            log.warning("写入状态文件失败：%s", exc)

    @property
    def topic_count(self) -> int:
        return len(self.reported)


def local_date_key(dt: datetime) -> str:
    """上海时区的日期键（YYYY-MM-DD），用于"今天是否已经跑过"的判断。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=SHANGHAI)
    return dt.astimezone(SHANGHAI).strftime("%Y-%m-%d")
