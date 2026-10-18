"""时间解析、时间窗口过滤与主题词匹配。

CC98 的时间字段在 JSON 里可能是：
  * ISO8601 字符串（带 Z 或带偏移，少数情况无时区 → 按 UTC 解释）
  * 数字（秒或毫秒时间戳）
  * 嵌套对象 {"year":..,"month":..,"day":..,"hour":..,"minute":..,"second":..}
全部统一转成 Asia/Shanghai。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Iterator, Sequence

try:  # Windows 上 zoneinfo 需要 tzdata；缺失时回退固定 UTC+8
    from zoneinfo import ZoneInfo

    SHANGHAI: timezone | ZoneInfo = ZoneInfo("Asia/Shanghai")
except Exception:  # pragma: no cover
    SHANGHAI = timezone(timedelta(hours=8), name="UTC+8")

UTC = timezone.utc
_MS_THRESHOLD = 1e11  # 大于此值视为毫秒时间戳
_TIME_KEYS = ("time", "createTime", "create_time", "postTime", "postTimeString", "lastPostTime")


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _parse_nested(obj: dict[str, Any]) -> datetime | None:
    year = _as_int(obj.get("year") or obj.get("Year"))
    month = _as_int(obj.get("month") or obj.get("Month"))
    day = _as_int(obj.get("day") or obj.get("Day"))
    if year is None or month is None or day is None:
        return None
    hour = _as_int(obj.get("hour") or obj.get("Hour")) or 0
    minute = _as_int(obj.get("minute") or obj.get("Minute")) or 0
    second = _as_int(obj.get("second") or obj.get("Second")) or 0
    try:
        return datetime(year, month, day, hour, minute, second, tzinfo=UTC)
    except ValueError:
        return None


def _parse_string(text: str) -> datetime | None:
    s = text.strip()
    if not s:
        return None
    if s.isdigit():
        return _parse_epoch(float(s))
    normalized = s.replace("Z", "+00:00") if s.endswith("Z") else s
    normalized = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", normalized)
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                dt = None
        if dt is None:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _parse_epoch(value: float) -> datetime | None:
    if value <= 0:
        return None
    if value > _MS_THRESHOLD:
        value = value / 1000.0
    try:
        # 不用 utcfromtimestamp（3.12 起已弃用）
        return datetime.fromtimestamp(value, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def parse_cc98_time(value: Any) -> datetime | None:
    """把任意形态的 CC98 时间值解析成带时区的 datetime；失败返回 None。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, bool):
        return None
    elif isinstance(value, (int, float)):
        dt = _parse_epoch(float(value))
    elif isinstance(value, str):
        dt = _parse_string(value)
    elif isinstance(value, dict):
        dt = _parse_nested(value)
    else:
        return None
    if dt is None:
        return None
    # 统一到上海时间，便于报告与"当地一天"语义
    return dt.astimezone(SHANGHAI)


def _first_time(topic: dict[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if key in topic and topic[key] not in (None, ""):
            return topic[key]
    return None


@dataclass
class Topic:
    """规范化后的主题帖。"""

    id: int
    title: str
    board_id: int | None
    board_name: str
    author: str
    created_at: datetime
    last_post_at: datetime | None
    reply_count: int | None
    hit_count: int | None
    is_anonymous: bool
    keywords: list[str] = field(default_factory=list)
    # 命中的来源：keyword-search / recent-feed（用于说明某条是搜索漏掉、靠最新主题补上的）
    sources: list[str] = field(default_factory=list)

    @property
    def url(self) -> str:
        return f"https://www.cc98.org/topic/{self.id}"


def topic_from_raw(raw: dict[str, Any]) -> Topic | None:
    """把 API 原始对象转成 Topic；缺 id 或缺创建时间则返回 None（调用方计数）。"""
    topic_id = _as_int(raw.get("id") or raw.get("topicId"))
    created = parse_cc98_time(_first_time(raw, _TIME_KEYS))
    if topic_id is None or created is None:
        return None
    last = parse_cc98_time(_first_time(raw, ("lastPostTime", "lastPostTimeString")))
    author = str(raw.get("userName") or raw.get("author") or "")
    is_anonymous = bool(raw.get("isAnonymous"))
    if is_anonymous and not author:
        author = "匿名"
    return Topic(
        id=topic_id,
        title=(str(raw.get("title") or "")).strip(),
        board_id=_as_int(raw.get("boardId")),
        board_name=str(raw.get("boardName") or "").strip(),
        author=author,
        created_at=created,
        last_post_at=last,
        reply_count=_as_int(raw.get("replyCount")),
        hit_count=_as_int(raw.get("hitCount")),
        is_anonymous=is_anonymous,
    )


def iter_topics(raws: Iterable[dict[str, Any]]) -> Iterator[Topic]:
    for raw in raws:
        topic = topic_from_raw(raw)
        if topic is not None:
            yield topic


def _is_ascii_alnum(ch: str) -> bool:
    """是否 ASCII 字母或数字（用 ord 判断，避免正则对 Unicode 的歧义）。"""
    return ch.isascii() and ch.isalnum()


def _find_keyword(
    haystack: str,
    needle: str,
    case_sensitive: bool,
    match_style: str,
) -> bool:
    """match_style:

    * substring —— 纯子串匹配（老行为）
    * word      —— 一律要求词边界
    * auto（默认）—— 只对**关键词两端的 ASCII 字符**要求词边界。

    auto 的效果："AI" 能命中 "AI 应用" / "用ai做科研" / "AI应用"，
    但不再误命中 "MacBook Air"、"AirPods"、"OpenAI"、"email"。
    纯中文关键词（如 "人工智能"）不受影响，仍是子串匹配。
    """
    if not needle:
        return False
    probe = needle if case_sensitive else needle.lower()
    if match_style == "substring":
        return probe in haystack

    start = 0
    while True:
        idx = haystack.find(probe, start)
        if idx < 0:
            return False
        end = idx + len(probe)
        left_ok = True
        right_ok = True
        if match_style == "word" or _is_ascii_alnum(probe[0]):
            left_ok = idx == 0 or not _is_ascii_alnum(haystack[idx - 1])
        if match_style == "word" or _is_ascii_alnum(probe[-1]):
            right_ok = end >= len(haystack) or not _is_ascii_alnum(haystack[end])
        if left_ok and right_ok:
            return True
        start = idx + 1


def match_keywords(
    text: str,
    keywords: Sequence[str],
    case_sensitive: bool = False,
    match_mode: str = "any",
    match_style: str = "auto",
) -> list[str]:
    """返回命中的关键词列表。match_mode=any 时命中任意一个即可返回非空。"""
    if not text:
        text = ""
    haystack = text if case_sensitive else text.lower()
    hits: list[str] = []
    for kw in keywords:
        if _find_keyword(haystack, kw, case_sensitive, match_style):
            hits.append(kw)
    if match_mode == "all":
        return hits if len(hits) == len(keywords) else []
    return hits


@dataclass
class WindowResult:
    topics: list[Topic] = field(default_factory=list)
    scanned: int = 0
    parse_failures: int = 0
    duplicates: int = 0


def select_topics(
    raw_topics: Iterable[dict[str, Any]],
    keywords: Sequence[str],
    start: datetime,
    end: datetime,
    case_sensitive: bool = False,
    match_mode: str = "any",
    match_style: str = "auto",
) -> WindowResult:
    """按"命中主题词 + 创建时间落在 [start, end]"筛选，并按 id 去重。"""
    result = WindowResult()
    by_id: dict[int, Topic] = {}

    for raw in raw_topics:
        result.scanned += 1
        topic = topic_from_raw(raw)
        if topic is None:
            result.parse_failures += 1
            continue
        hits = match_keywords(topic.title, keywords, case_sensitive, match_mode, match_style)
        if not hits:
            # 标题未命中时用作者名兜底（CC98 搜索也会匹配作者），不影响时间过滤准确性
            hits = match_keywords(topic.author, keywords, case_sensitive, match_mode, match_style)
        if not hits:
            continue
        # 时间窗口必须是闭区间 [start, end]，且上界要真正拦住"晚于窗口结束"的帖子：
        # 抓取本身要花时间，接口可能返回比运行时刻更新的帖子，那些不属于本窗口。
        if not (start <= topic.created_at <= end):
            continue

        if topic.id in by_id:
            result.duplicates += 1
            continue
        topic.keywords = hits
        by_id[topic.id] = topic

    result.topics = sorted(
        by_id.values(),
        key=lambda t: (t.created_at, t.id),
        reverse=True,
    )
    return result


def merge_topics(*groups: Iterable[Topic]) -> list[Topic]:
    """跨关键词合并并按 id 去重，命中的关键词取并集；按创建时间倒序。"""
    by_id: dict[int, Topic] = {}
    for group in groups:
        for topic in group:
            existing = by_id.get(topic.id)
            if existing is None:
                by_id[topic.id] = topic
                continue
            for kw in topic.keywords:
                if kw not in existing.keywords:
                    existing.keywords.append(kw)
            for src in topic.sources:
                if src not in existing.sources:
                    existing.sources.append(src)
    return sorted(by_id.values(), key=lambda t: (t.created_at, t.id), reverse=True)
