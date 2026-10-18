"""命令行入口：init / auth-test / run / once / install-task / uninstall-task。

退出码约定：
    0 = 成功
    1 = 运行时错误（网络、解析、写文件失败）
    2 = 配置或凭据问题（需要用户动手修）
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path

from . import __version__
from .api import ApiClient, ApiError, PaginationResult
from .auth import AuthError, Token, get_token
from .config import (
    AppConfig,
    ConfigError,
    DEFAULT_TASK_NAME,
    EXAMPLE_CONFIG_PATH,
    PAGE_LIMIT_HARD,
    PAGE_SIZE_MAX,
    PROJECT_ROOT,
    load_config,
    normalize_keywords,
)
from .logging_setup import get_logger, setup_logging
from .render import DigestRun, ScanStat, render_csv, render_json, render_markdown
from .state import StateStore, local_date_key
from .window import SHANGHAI, Topic, merge_topics, select_topics

EXIT_OK = 0
EXIT_RUNTIME = 1
EXIT_CONFIG = 2


class RunError(Exception):
    """运行期错误，映射到 EXIT_RUNTIME。"""


@dataclass
class RunOutcome:
    run: DigestRun
    markdown_path: Path | None = None
    csv_path: Path | None = None
    latest_path: Path | None = None
    previous_run_at: str | None = None
    state_file: Path | None = None
    extra: dict = field(default_factory=dict)


# ---------------------------------------------------------------- 入口


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cc98-digest",
        description="CC98（浙江大学校网论坛）主题词每日摘要：抓取最近 N 小时内命中主题词的主题帖。",
    )
    parser.add_argument("--version", action="version", version=f"cc98-digest {__version__}")
    parser.add_argument("--config", help="配置文件路径（默认项目根目录 config.json）")
    parser.add_argument(
        "--verbose", action="store_true", help="输出 DEBUG 级日志"
    )
    parser.add_argument(
        "--no-console-log", action="store_true", help="不把日志打到控制台（计划任务用）"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="生成 config.json（不会覆盖已有文件，除非 --force）")
    p_init.add_argument("--force", action="store_true", help="覆盖已存在的 config.json")

    p_auth = sub.add_parser("auth-test", help="验证配置与登录是否可用")
    p_auth.add_argument("--force-login", action="store_true", help="忽略令牌缓存，强制重新登录")

    p_run = sub.add_parser("run", help="标准入口：按配置跑一次（计划任务调用此命令）")
    p_run.add_argument("--print", action="store_true", help="同时把报告打印到控制台")
    p_run.add_argument("--no-state", action="store_true", help="不读写 state.json")

    p_once = sub.add_parser("once", help="一次性运行，可临时改窗口/主题词")
    p_once.add_argument("--hours", type=float, default=None, help="覆盖时间窗口小时数（默认取配置）")
    p_once.add_argument(
        "--all-time", action="store_true", help="不做时间过滤，输出全部匹配结果（受翻页上限约束）"
    )
    p_once.add_argument(
        "--keyword", action="append", default=None, help="临时替换主题词，可重复指定"
    )
    p_once.add_argument("--print", action="store_true", help="把报告打印到控制台")
    p_once.add_argument("--no-state", action="store_true", help="不读写 state.json")
    p_once.add_argument("--max-pages", type=int, default=None, help=f"覆盖每词翻页上限（硬上限 {PAGE_LIMIT_HARD} 页）")
    p_once.add_argument("--page-size", type=int, default=None, help="覆盖每页条数（1..20）")

    p_inst = sub.add_parser("install-task", help="注册每天定时运行的 Windows 计划任务")
    p_inst.add_argument("--time", default="23:00", help="每天的运行时刻 HH:MM，默认 23:00")
    p_inst.add_argument("--name", default=DEFAULT_TASK_NAME, help=f"任务名，默认 {DEFAULT_TASK_NAME}")

    p_uninst = sub.add_parser("uninstall-task", help="删除计划任务")
    p_uninst.add_argument("--name", default=DEFAULT_TASK_NAME)

    p_gui = sub.add_parser("gui", help="启动本地图形界面（浏览器里查看报告 / 手动抓取）")
    p_gui.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1（仅本机）")
    p_gui.add_argument("--port", type=int, default=8848, help="监听端口，默认 8848")
    p_gui.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")

    p_auto = sub.add_parser(
        "autostart",
        help="开机自启：默认作为自启入口运行（端口已占用则静默退出）",
    )
    p_auto.add_argument("--install", action="store_true", help="把界面服务设为开机自启")
    p_auto.add_argument("--remove", action="store_true", help="取消开机自启")
    p_auto.add_argument("--status", action="store_true", help="查看是否已设置开机自启")
    p_auto.add_argument("--host", default="127.0.0.1", help="服务监听地址（写入快捷方式）")
    p_auto.add_argument("--port", type=int, default=8848, help="服务端口（写入快捷方式）")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "init":
        return cmd_init(args)

    if args.command == "gui" and not _ensure_config(args.config):
        return EXIT_CONFIG

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        return EXIT_CONFIG

    level = "DEBUG" if args.verbose else cfg.runtime.log_level
    setup_logging(cfg.log_dir, level=level, to_console=not args.no_console_log)
    log = get_logger("cli")

    try:
        if args.command == "auth-test":
            return cmd_auth_test(cfg, args)
        if args.command == "run":
            outcome = run_digest(
                cfg,
                hours=None,
                keywords=None,
                all_time=False,
                use_state=not args.no_state,
                max_pages=None,
                page_size=None,
            )
        elif args.command == "once":
            outcome = run_digest(
                cfg,
                hours=args.hours,
                keywords=normalize_keywords(args.keyword) if args.keyword else None,
                all_time=args.all_time,
                use_state=not args.no_state,
                max_pages=args.max_pages,
                page_size=args.page_size,
            )
        elif args.command == "install-task":
            from .scheduler import install_task

            return install_task(cfg, at=args.time, name=args.name)
        elif args.command == "gui":
            from .gui import serve

            return serve(
                cfg,
                host=args.host,
                port=args.port,
                open_browser=not args.no_browser,
            )
        elif args.command == "uninstall-task":
            from .scheduler import uninstall_task

            return uninstall_task(name=args.name)
        elif args.command == "autostart":
            return cmd_autostart(cfg, args)
        else:  # pragma: no cover - argparse 已保证
            parser.error(f"未知命令 {args.command}")
            return EXIT_CONFIG
    except AuthError as exc:
        print(f"[登录失败] {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except ConfigError as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except ApiError as exc:
        print(f"[接口错误] {exc}", file=sys.stderr)
        return EXIT_RUNTIME
    except RunError as exc:
        print(f"[运行失败] {exc}", file=sys.stderr)
        return EXIT_RUNTIME
    except KeyboardInterrupt:  # pragma: no cover
        print("[中断] 用户取消。", file=sys.stderr)
        return EXIT_RUNTIME
    except Exception as exc:  # pragma: no cover - 兜底，保证日志里有栈
        log.exception("未预期的错误：%s", exc)
        print(f"[未预期错误] {exc}", file=sys.stderr)
        return EXIT_RUNTIME

    if getattr(args, "print", False):
        print(render_markdown(outcome.run))
    else:
        log.info(
            "完成：命中 %s 条，报告 %s",
            len(outcome.run.topics),
            outcome.markdown_path or "（未写出）",
        )
    return EXIT_OK


# ---------------------------------------------------------------- 子命令


def _ensure_config(path: str | None) -> bool:
    """gui 启动前兜底：config.json 不存在时从模板复制一份。

    让新手可以直接 `python -m cc98digest gui` 打开网页，账号密码再在网页里登录，
    不必先手动跑 init / 编辑配置文件。已有文件时不覆盖。返回 False 表示无法就绪。
    """
    target = Path(path) if path else (PROJECT_ROOT / "config.json")
    if target.exists():
        return True
    if not EXAMPLE_CONFIG_PATH.exists():
        print(f"[配置错误] 找不到模板 {EXAMPLE_CONFIG_PATH}", file=sys.stderr)
        return False
    try:
        target.write_text(EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    except OSError as exc:
        print(f"[配置错误] 无法生成 {target}：{exc}", file=sys.stderr)
        return False
    print(f"未找到 {target}，已从模板自动生成（账号密码请在网页界面里登录填写）。")
    return True


def cmd_init(args) -> int:
    target = Path(args.config) if args.config else (PROJECT_ROOT / "config.json")
    if target.exists() and not args.force:
        print(f"已存在 {target}，未覆盖。需要重建请加 --force。")
        return EXIT_OK
    if not EXAMPLE_CONFIG_PATH.exists():
        print(f"[配置错误] 找不到模板 {EXAMPLE_CONFIG_PATH}", file=sys.stderr)
        return EXIT_CONFIG
    target.write_text(EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"已生成 {target}")
    print("请填入 credentials.username / credentials.password（或设置环境变量 CC98_USERNAME / CC98_PASSWORD）。")
    print("然后运行：python -m cc98digest auth-test")
    return EXIT_OK


def cmd_auth_test(cfg: AppConfig, args) -> int:
    token = get_token(cfg, force_login=args.force_login)
    print("登录成功。")
    print(f"  api_base      : {cfg.credentials.api_base}")
    print(f"  账号          : {cfg.credentials.username or '(来自环境变量或未配置)'}")
    print(f"  令牌有效期剩余: {token.seconds_left} 秒")
    print(f"  令牌缓存文件  : {cfg.token_cache_path}")
    print(f"  主题词        : {'、'.join(normalize_keywords(cfg.search.keywords)) or '（未配置）'}")

    # 真正打一个只读接口，确认权限足够
    # 注意：ApiClient 需要的是"令牌字符串"，不是 Token 对象
    # （这里曾误传 Token，导致 Authorization 头变成 `Bearer Token(access_token=...)`，接口一律 401）
    client = ApiClient(cfg, lambda force=False: get_token(cfg, force_login=force).access_token)
    payload = client.get_json("/topic/search", {"keyword": _first_keyword(cfg), "from": 0, "size": 1})
    from .api import as_list

    items = as_list(payload)
    print(f"  搜索接口自检  : 正常（关键词「{_first_keyword(cfg)}」返回 {len(items)} 条样例）")
    return EXIT_OK


def _first_keyword(cfg: AppConfig) -> str:
    keywords = normalize_keywords(cfg.search.keywords)
    return keywords[0] if keywords else "AI"


def cmd_autostart(cfg: AppConfig, args) -> int:
    """开机自启：安装 / 取消 / 查看；不带参数时作为自启入口运行。"""
    from .scheduler import (
        autostart_entry,
        autostart_shortcut_path,
        install_autostart,
        uninstall_autostart,
    )

    if args.install:
        return install_autostart(cfg)
    if args.remove:
        return uninstall_autostart()
    if args.status:
        path = autostart_shortcut_path()
        if path.exists():
            print(f"已设置开机自启：{path}")
            print(f"界面地址：http://{args.host}:{args.port}/")
        else:
            print("未设置开机自启。")
            print("设置：python -m cc98digest autostart --install")
        return EXIT_OK
    # 默认：作为开机自启的入口运行（端口被占用就静默退出）
    return autostart_entry(cfg, host=args.host, port=args.port)


# ---------------------------------------------------------------- 主流程


def run_digest(
    cfg: AppConfig,
    hours: float | None = None,
    keywords: list[str] | None = None,
    all_time: bool = False,
    use_state: bool = True,
    max_pages: int | None = None,
    page_size: int | None = None,
) -> RunOutcome:
    log = get_logger("digest")
    # 先把过期的检索记录清掉：这样即使这次抓取中途失败，磁盘也不会一直涨。
    # （这一次运行结束时会再清一次，那时新报告已经写盘，能顺带把多出来的组砍掉。）
    run_at = datetime.now(SHANGHAI)
    pruned_before = prune_old_reports(cfg, run_at)

    search = cfg.search
    if hours is not None:
        search = replace(search, hours=hours)
    if max_pages is not None:
        if max_pages < 1:
            raise ConfigError("--max-pages 必须 >= 1。")
        if max_pages > PAGE_LIMIT_HARD:
            get_logger("cli").warning(
                "--max-pages %s 超过检索规则硬上限，已按 %s 页处理。", max_pages, PAGE_LIMIT_HARD
            )
            max_pages = PAGE_LIMIT_HARD
        search = replace(search, max_pages_per_keyword=max_pages)
    if page_size is not None:
        if page_size < 1:
            raise ConfigError("--page-size 必须 >= 1。")
        if page_size > PAGE_SIZE_MAX:
            get_logger("cli").warning(
                "--page-size %s 超过 CC98 接口上限，已按 %s 处理。", page_size, PAGE_SIZE_MAX
            )
            page_size = PAGE_SIZE_MAX
        search = replace(search, page_size=page_size)
    cfg = replace(cfg, search=search)

    active_keywords = normalize_keywords(keywords if keywords is not None else search.keywords)
    if not active_keywords:
        raise ConfigError(
            "没有可用的主题词。请在 config.json 的 search.keywords 里配置，或用 --keyword 指定。"
        )

    window_hours = float(search.hours)
    window_start = run_at - timedelta(hours=window_hours)
    if all_time:
        window_start = datetime(1970, 1, 1, tzinfo=SHANGHAI)

    log.info(
        "开始：主题词 %s；窗口 %s → %s",
        "、".join(active_keywords),
        window_start.strftime("%Y-%m-%d %H:%M"),
        run_at.strftime("%Y-%m-%d %H:%M"),
    )

    warnings: list[str] = []
    stats: list[ScanStat] = []
    groups: list[list[Topic]] = []
    feed_note: str | None = None
    feed_pages = 0
    feed_scanned = 0
    feed_oldest = None

    token_holder: dict[str, Token] = {}

    def token_provider(force: bool = False) -> str:
        if force:
            token_holder.pop("token", None)
        token = token_holder.get("token")
        if token is None or not token.is_valid or force:
            token = get_token(cfg, force_login=force)
            token_holder["token"] = token
        return token.access_token

    client = ApiClient(cfg, token_provider)
    interrupted = False
    budget = float(search.run_time_budget_seconds)
    deadline = (time.monotonic() + budget) if budget > 0 else None
    budget_spent = False

    # ---- 第一路：全站最新主题（时效性最好，补搜索索引延迟） ----
    if int(search.recent_feed_pages) > 0 and not interrupted:
        try:
            sweep = client.sweep_recent_topics(window_start, deadline=deadline)
        except KeyboardInterrupt:
            interrupted = True
            sweep = None
            warnings.append("最新主题扫描被手动中断。")
        if sweep is not None:
            selected_feed = select_topics(
                sweep.topics,
                active_keywords,
                window_start,
                run_at,
                search.case_sensitive,
                search.match_mode,
                search.match_style,
            )
            if selected_feed.topics:
                groups.append(selected_feed.topics)
                for topic in selected_feed.topics:
                    topic.sources.append("recent-feed")
            feed_pages = sweep.pages
            feed_scanned = sweep.scanned
            feed_oldest = sweep.oldest_at
            feed_note = (
                f"最新主题扫描：{sweep.pages} 页 / {sweep.scanned} 条，"
                f"窗口内命中 {len(selected_feed.topics)} 条"
            )
            if sweep.oldest_at is not None:
                feed_note += f"，最早到 {sweep.oldest_at:%m-%d %H:%M}"
            log.info(feed_note)

    for keyword in active_keywords:
        if interrupted or budget_spent:
            reason = "因手动中断提前结束" if interrupted else "因整轮时间预算用尽"
            warnings.append(f"关键词「{keyword}」未扫描（{reason}）。")
            if budget_spent:
                log.warning("整轮时间预算用尽，跳过关键词「%s」。", keyword)
            stats.append(
                ScanStat(
                    keyword=keyword,
                    scanned=0,
                    pages=0,
                    capped=False,
                    matched=0,
                    interrupted=interrupted,
                    budget_exceeded=budget_spent,
                )
            )
            continue
        try:
            result: PaginationResult = client.search_topics(keyword, deadline=deadline)
        except KeyboardInterrupt:
            # Ctrl+C 落在两次请求之间：该词按"中断"记录，仍要出报告
            interrupted = True
            warnings.append(f"关键词「{keyword}」被手动中断，未完成扫描。")
            log.warning("关键词「%s」被手动中断，将基于已有结果出报告。", keyword)
            stats.append(
                ScanStat(
                    keyword=keyword,
                    scanned=0,
                    pages=0,
                    capped=False,
                    matched=0,
                    interrupted=True,
                )
            )
            continue
        except ApiError as exc:
            # 单个词失败不拖垮整轮：记录告警，其余关键词继续
            warnings.append(f"关键词「{keyword}」抓取失败：{exc}")
            log.error("关键词「%s」抓取失败：%s", keyword, exc)
            stats.append(
                ScanStat(
                    keyword=keyword, scanned=0, pages=0, capped=False, matched=0, failed=True
                )
            )
            continue
        selected = select_topics(
            result.topics,
            [keyword],
            window_start,
            run_at,
            search.case_sensitive,
            search.match_mode,
            search.match_style,
        )
        for topic in selected.topics:
            if "keyword-search" not in topic.sources:
                topic.sources.append("keyword-search")
        groups.append(selected.topics)
        if result.interrupted:
            interrupted = True
            warnings.append(f"关键词「{keyword}」被手动中断，结果可能不完整。")
        stats.append(
            ScanStat(
                keyword=keyword,
                scanned=result.scanned,
                pages=result.pages,
                capped=result.capped,
                matched=len(selected.topics),
                parse_failures=selected.parse_failures + result.parse_failures,
                interrupted=result.interrupted,
                budget_exceeded=result.budget_exceeded,
            )
        )
        log.info(
            "关键词「%s」完成：扫描 %s 条，窗口内命中 %s 条%s%s%s",
            keyword,
            result.scanned,
            len(selected.topics),
            "（触顶）" if result.capped else "",
            "（超预算，结果不完整）" if result.budget_exceeded else "",
            "（中断，结果不完整）" if result.interrupted else "",
        )
        if result.budget_exceeded:
            budget_spent = True
    if budget_spent:
        warnings.append("整轮时间预算用尽，未扫描的关键词已跳过（可调大 run_time_budget_seconds）。")
    if interrupted:
        warnings.append("Ctrl+C：本轮为部分结果，报告已按已抓取内容写出。")

    topics = merge_topics(*groups)

    # ---- 幂等标注 ----
    state = StateStore.load(cfg.state_path) if use_state else StateStore(cfg.state_path)
    previous_run_at = state.last_run_at
    repeated: set[int] = set()
    if use_state:
        for topic in topics:
            if state.was_reported(topic.id, topic.keywords):
                repeated.add(topic.id)
        for topic in topics:
            state.record(topic.id, topic.keywords, run_at)
        state.prune(run_at)
        state.save(run_at)

    run = DigestRun(
        run_at=run_at,
        window_start=window_start,
        window_end=run_at,
        keywords=active_keywords,
        topics=topics,
        stats=stats,
        repeated_ids=repeated,
        warnings=warnings,
        host=cfg.credentials.api_base.replace("https://", "").replace("http://", ""),
        feed_pages=feed_pages,
        feed_scanned=feed_scanned,
        feed_oldest=feed_oldest,
        feed_note=feed_note,
    )

    outcome = RunOutcome(run=run, previous_run_at=previous_run_at, state_file=cfg.state_path)
    outcome.markdown_path, outcome.latest_path, outcome.csv_path = write_reports(cfg, run)
    pruned_after = prune_old_reports(cfg, run_at)
    outcome.extra["pruned"] = pruned_before + pruned_after
    if pruned_after:
        # 只报"写盘后"这一遍：它会连这次新写的组一起纳入上限判断
        log.info("本轮共清理 %s 个过期检索记录。", pruned_before + pruned_after)
    return outcome


def write_reports(cfg: AppConfig, run: DigestRun) -> tuple[Path | None, Path | None, Path | None]:
    """写 reports/cc98-digest-YYYY-MM-DD-HHMM.{md,csv} 与 latest.md / latest.csv。"""
    log = get_logger("digest")
    out_dir = cfg.reports_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    formats = {f.lower() for f in cfg.output.formats}
    stem = run.run_at.strftime("cc98-digest-%Y-%m-%d-%H%M")

    md_path: Path | None = None
    csv_path: Path | None = None
    latest_md: Path | None = None

    if "md" in formats:
        content = render_markdown(run)
        md_path = out_dir / f"{stem}.md"
        md_path.write_text(content, encoding="utf-8")
        latest_md = out_dir / "latest.md"
        latest_md.write_text(content, encoding="utf-8")
        log.info("已写出报告：%s", md_path)

    if "csv" in formats:
        csv_content = render_csv(run)
        csv_path = out_dir / f"{stem}.csv"
        # csv 模块已返回带 BOM 的字符串，用 utf-8 写出即可
        csv_path.write_text(csv_content, encoding="utf-8")
        (out_dir / "latest.csv").write_text(csv_content, encoding="utf-8")
        log.info("已写出 CSV：%s", csv_path)

    # 固定写一份 JSON：前端界面（cc98-gui）直接读它，不需要解析 Markdown
    json_content = render_json(run)
    (out_dir / f"{stem}.json").write_text(json_content, encoding="utf-8")
    (out_dir / "latest.json").write_text(json_content, encoding="utf-8")

    return md_path, latest_md, csv_path


def prune_old_reports(cfg: AppConfig, now: datetime) -> int:
    """按保留策略清理检索记录：报告文件（md/csv/json）+ 计划任务日志。

    规则（配置见 output.keep_days / max_report_sets / max_scheduler_logs）：
      1. 早于 keep_days 天的报告文件删除；
      2. 即使都在保留期内，reports/ 里也只留最新的 max_report_sets 组（防一天跑很多次堆满）；
      3. latest.* 永远保留（界面和快捷查看依赖它）；
      4. 日志目录里只留最新的 max_scheduler_logs 个 scheduler-*.log
         （cc98-digest.log 由 RotatingFileHandler 自己滚动，不在这里动）。
    """
    log = get_logger("digest")
    keep_days = int(cfg.output.keep_days)
    if keep_days < 0:
        keep_days = 0
    removed = 0

    if keep_days > 0:
        cutoff = time.time() - keep_days * 86400
        for path in cfg.reports_dir.glob("cc98-digest-*"):
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError as exc:  # pragma: no cover
                log.warning("清理旧报告失败 %s：%s", path, exc)

    # 条数上限：按"报告组"算（同一次运行的 md/csv/json 算一组）
    max_sets = int(cfg.output.max_report_sets)
    if max_sets > 0:
        stems: dict[str, float] = {}
        for path in cfg.reports_dir.glob("cc98-digest-*"):
            if not path.is_file():
                continue
            stems[path.stem] = max(stems.get(path.stem, 0.0), path.stat().st_mtime)
        ranked = sorted(stems.items(), key=lambda kv: kv[1], reverse=True)
        for stem, _mtime in ranked[max_sets:]:
            for path in cfg.reports_dir.glob(f"{stem}.*"):
                try:
                    path.unlink()
                    removed += 1
                except OSError as exc:  # pragma: no cover
                    log.warning("清理报告失败 %s：%s", path, exc)

    # 计划任务日志：文件名带日期，按名字排序即可（scheduler-YYYYMMDD.log）
    max_logs = int(cfg.output.max_scheduler_logs)
    if max_logs > 0:
        logs = sorted(cfg.log_dir.glob("scheduler-*.log"), key=lambda p: p.name, reverse=True)
        for path in logs[max_logs:]:
            try:
                path.unlink()
                removed += 1
            except OSError as exc:  # pragma: no cover
                log.warning("清理计划任务日志失败 %s：%s", path, exc)

    if removed:
        log.info(
            "已清理 %s 个过期检索记录（保留 %s 天 / 最多 %s 组报告 / 最多 %s 个任务日志）。",
            removed,
            keep_days,
            max_sets,
            max_logs,
        )
    return removed
