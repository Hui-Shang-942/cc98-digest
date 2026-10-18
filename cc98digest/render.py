"""报告渲染：Markdown（人看）与 CSV（Excel 看）。

渲染函数保持纯函数（输入 dataclass，输出字符串）。
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from .window import Topic

TITLE_MAX = 200
CELL_MAX = 120


@dataclass
class ScanStat:
    keyword: str
    scanned: int
    pages: int
    capped: bool
    matched: int
    parse_failures: int = 0
    interrupted: bool = False
    budget_exceeded: bool = False
    failed: bool = False

    @property
    def partial(self) -> bool:
        return self.capped or self.interrupted or self.budget_exceeded

    @property
    def abnormal(self) -> bool:
        """是否出现"异常"的不完整（不算每轮都会发生的翻页触顶）。

        翻页上限现在锁死在 2 页，触顶是既定规则下的常态，不再当作异常来报警；
        真正需要提示的是：抓取失败、超出时间预算、被手动中断。
        """
        return self.failed or self.budget_exceeded or self.interrupted

    def abnormal_label(self) -> str:
        reasons = []
        if self.failed:
            reasons.append("抓取失败")
        if self.budget_exceeded:
            reasons.append("超出时间预算")
        if self.interrupted:
            reasons.append("手动中断")
        return "/".join(reasons) if reasons else "正常"


@dataclass
class DigestRun:
    run_at: datetime
    window_start: datetime
    window_end: datetime
    keywords: Sequence[str]
    topics: list[Topic] = field(default_factory=list)
    stats: list[ScanStat] = field(default_factory=list)
    repeated_ids: set[int] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)
    host: str = "api.cc98.org"
    feed_pages: int = 0
    feed_scanned: int = 0
    feed_oldest: datetime | None = None
    feed_note: str | None = None

    @property
    def total_scanned(self) -> int:
        return sum(s.scanned for s in self.stats)

    @property
    def total_parse_failures(self) -> int:
        return sum(s.parse_failures for s in self.stats)

    @property
    def capped_keywords(self) -> list[str]:
        return [s.keyword for s in self.stats if s.capped]

    @property
    def partial_keywords(self) -> list[str]:
        """结果可能不完整、且**需要提示**的关键词（失败 / 超时 / 被中断）。

        不含"翻页触顶"：上限锁死 2 页后它每轮都会发生，属于既定规则，不再报警。
        """
        return [s.keyword for s in self.stats if s.abnormal]

    @property
    def interrupted_keywords(self) -> list[str]:
        return [s.keyword for s in self.stats if s.interrupted]

    @property
    def budget_keywords(self) -> list[str]:
        return [s.keyword for s in self.stats if s.budget_exceeded]

    @property
    def feed_only_topics(self) -> list[Topic]:
        """只靠"最新主题"扫到、关键词搜索没返回的主题（搜索索引延迟的实证）。"""
        return [
            t
            for t in self.topics
            if "recent-feed" in t.sources and "keyword-search" not in t.sources
        ]


def escape_cell(text: str, limit: int = CELL_MAX) -> str:
    """Markdown 表格单元转义：换行、竖线、反斜杠；并截断超长内容。"""
    s = (text or "").replace("\\", "\\\\").replace("|", "\\|")
    s = " ".join(s.split())  # 折叠换行与连续空白
    if len(s) > limit:
        s = s[: limit - 1] + "…"
    return s


def format_dt(dt: datetime | None, pattern: str = "%m-%d %H:%M") -> str:
    if dt is None:
        return "-"
    return dt.strftime(pattern)


def format_full(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S %z")


def render_markdown(run: DigestRun) -> str:
    lines: list[str] = []
    lines.append("# CC98 主题词日报")
    lines.append("")
    lines.append(f"- 运行时间：{format_full(run.run_at)}（UTC+8）")
    lines.append(
        f"- 时间窗口：{format_full(run.window_start)} → {format_full(run.window_end)}"
    )
    lines.append(f"- 主题词：{'、'.join(run.keywords) if run.keywords else '（未配置）'}")
    lines.append(f"- 命中主题帖：**{len(run.topics)}** 条（扫描 {run.total_scanned} 条原始结果）")
    lines.append(f"- 数据源：`https://{run.host}` 只读搜索接口")
    if run.partial_keywords:
        lines.append(
            "- ⚠️ **本次结果为部分结果**（以下关键词异常："
            + "、".join(run.partial_keywords)
            + "），详见文末「数据完备性」。"
        )
    lines.append("")

    if run.topics:
        # 报告只保留"按主题词分组"这一种结果视图（标题 + 链接 + 发布时间）。
        lines.append("## 按主题词分组（同一帖可能出现在多个词下）")
        lines.append("")
        for kw in run.keywords:
            group = [t for t in run.topics if kw in t.keywords]
            stat = next((s for s in run.stats if s.keyword == kw), None)
            scanned_note = f"，扫描 {stat.scanned} 条" if stat else ""
            lines.append(f"### {kw}（命中 {len(group)} 条{scanned_note}）")
            lines.append("")
            if not group:
                lines.append("无")
                lines.append("")
                continue
            lines.append("| # | 标题 | 发布时间 |")
            lines.append("|---|------|----------|")
            for idx, topic in enumerate(group, 1):
                title = escape_cell(topic.title, TITLE_MAX) or "（无标题）"
                link = f"[{title}]({topic.url})" if topic.title else "（无标题）"
                repeat = " 🔄" if topic.id in run.repeated_ids else ""
                lines.append(f"| {idx} | {link}{repeat} | {format_dt(topic.created_at)} |")
            lines.append("")
        if run.repeated_ids:
            lines.append("> 🔄 = 上一次运行时已报告过（同一天重跑的重复项）。")
            lines.append("")
    else:
        lines.append("## 结果")
        lines.append("")
        lines.append("时间窗口内没有匹配主题词的主题帖。")
        lines.append("")

    # ---- 数据完备性 ----
    lines.append("## 数据完备性")
    lines.append("")
    lines.append(
        "检索方式：CC98 的 `/topic/search` 不支持按时间过滤或排序，因此按关键词抓取搜索结果，"
        "再在本地按创建时间筛出时间窗口内的帖子。按当前规则，每个关键词最多看 2 页。"
    )
    lines.append("")
    scan_summary = "；".join(
        f"{escape_cell(stat.keyword, 40)}：{stat.pages} 页 / 扫描 {stat.scanned} 条 / 窗口内命中 {stat.matched} 条"
        for stat in run.stats
    )
    if scan_summary:
        lines.append(f"- 扫描统计：{scan_summary}")
    if run.feed_note:
        lines.append(f"- {run.feed_note}")
    feed_only = run.feed_only_topics
    if feed_only:
        lines.append(
            f"- 其中 **{len(feed_only)} 条只被「最新主题」扫到**（关键词搜索当时还没索引到，多为刚发布的帖）："
            + "、".join(f"[{escape_cell(t.title, 24)}]({t.url})" for t in feed_only)
        )
    lines.append("")
    exceptions = [
        f"{escape_cell(stat.keyword, 40)}（{stat.abnormal_label()}）"
        for stat in run.stats
        if stat.abnormal
    ]
    if exceptions:
        lines.append("> ⚠️ 以下关键词本次异常，结果可能不完整：" + "、".join(exceptions))
        lines.append("")
    if run.budget_keywords:
        lines.append(
            "> ⏱ 达到单轮时间预算的关键词已按当前进度出报告，"
            "可调大 `search.run_time_budget_seconds`，或分批运行（如 `once --keyword AI`）。"
        )
        lines.append("")
    if run.total_parse_failures:
        lines.append(f"> 有 {run.total_parse_failures} 条结果缺少 id 或时间字段，已跳过。")
        lines.append("")

    if run.warnings:
        lines.append("## 运行告警")
        lines.append("")
        for warning in run.warnings:
            lines.append(f"- {warning}")
        lines.append("")

    lines.append("---")
    lines.append("由 cc98-digest 自动生成（只读接口，不会发帖/回帖/点赞）。")
    lines.append("")
    return "\n".join(lines)


CSV_HEADER = [
    "id",
    "title",
    "board",
    "author",
    "created_at",
    "last_post_at",
    "reply_count",
    "hit_count",
    "keywords",
    "url",
    "repeated",
]


def render_csv(run: DigestRun) -> str:
    """UTF-8 with BOM，Excel 直接双击不乱码。"""
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    writer.writerow(CSV_HEADER)
    for topic in run.topics:
        writer.writerow(
            [
                topic.id,
                topic.title,
                topic.board_name,
                topic.author,
                format_full(topic.created_at),
                format_full(topic.last_post_at) if topic.last_post_at else "",
                topic.reply_count if topic.reply_count is not None else "",
                topic.hit_count if topic.hit_count is not None else "",
                "、".join(topic.keywords),
                topic.url,
                "1" if topic.id in run.repeated_ids else "",
            ]
        )
    return "\ufeff" + buf.getvalue()


# --------------------------------------------------------------------------
# 机器可读输出：前端界面（cc98-gui）直接消费这个结构，避免去解析 Markdown
# --------------------------------------------------------------------------


def _topic_to_dict(topic: Topic, run: DigestRun) -> dict[str, Any]:
    return {
        "id": topic.id,
        "title": topic.title,
        "url": topic.url,
        "board": topic.board_name,
        "author": topic.author,
        "isAnonymous": topic.is_anonymous,
        "createdAt": topic.created_at.isoformat(),
        "createdLabel": format_dt(topic.created_at),
        "lastPostLabel": format_dt(topic.last_post_at),
        "replyCount": topic.reply_count,
        "hitCount": topic.hit_count,
        "keywords": list(topic.keywords),
        "repeated": topic.id in run.repeated_ids,
    }


def build_report_payload(run: DigestRun) -> dict[str, Any]:
    """把一次运行结果整理成前端要的 JSON。

    只在这里组装一次，前端不再关心接口细节；字段名用 camelCase，方便 JS 直接取。
    """
    groups = []
    for keyword in run.keywords:
        stat = next((s for s in run.stats if s.keyword == keyword), None)
        topics = [t for t in run.topics if keyword in t.keywords]
        groups.append(
            {
                "keyword": keyword,
                "count": len(topics),
                "pages": stat.pages if stat else 0,
                "scanned": stat.scanned if stat else 0,
                "status": stat.abnormal_label() if stat else "未扫描",
                "abnormal": bool(stat.abnormal) if stat else False,
                "topics": [_topic_to_dict(t, run) for t in topics],
            }
        )

    return {
        "schema": 1,
        "runAt": run.run_at.isoformat(),
        "runLabel": format_full(run.run_at),
        "windowStart": run.window_start.isoformat(),
        "windowEnd": run.window_end.isoformat(),
        "windowLabel": f"{format_full(run.window_start)} → {format_full(run.window_end)}",
        "keywords": list(run.keywords),
        "totalTopics": len(run.topics),
        "totalScanned": run.total_scanned,
        "host": run.host,
        "parseFailures": run.total_parse_failures,
        "partialKeywords": list(run.partial_keywords),
        "cappedKeywords": list(run.capped_keywords),
        "warnings": list(run.warnings),
        "groups": groups,
        "topics": [_topic_to_dict(t, run) for t in run.topics],
    }


def render_json(run: DigestRun) -> str:
    import json

    return json.dumps(build_report_payload(run), ensure_ascii=False, indent=2)
