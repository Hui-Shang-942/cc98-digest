"""配置加载、校验与脱敏。

设计要点：
- 只依赖标准库；
- 所有相对路径都以项目根目录（本包所在目录的父目录）为基准解析，保证计划任务以任意
  工作目录启动时行为一致；
- 凭据优先级：环境变量 CC98_USERNAME / CC98_PASSWORD > config.json；
- 对外只暴露脱敏后的字典，密码/密钥/token 永不出现在日志里。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from .logging_setup import get_logger

log = get_logger("config")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.json"
EXAMPLE_CONFIG_PATH = PROJECT_ROOT / "config.example.json"

# CC98 /topic/search 实测的 size 上限：size>20 一律返回 HTTP 400 invalid_from_or_size
# （网络上不少第三方客户端文档写的是 50，与实测不符）
PAGE_SIZE_MAX = 20

# 每个关键词的翻页硬上限：无论配置里写多少、--max-pages 传多少，都不会超过这个值。
# 当前检索规则要求"命中后最多看两页"，所以这里锁死 2。
PAGE_LIMIT_HARD = 2

# 计划任务安装脚本引用的固定入口，避免误配
DEFAULT_TASK_NAME = "CC98TopicDigest"

SENSITIVE_KEYS = {
    "password",
    "client_secret",
    "access_token",
    "refresh_token",
    "token",
    "authorization",
}


class ConfigError(Exception):
    """配置缺失或非法。"""


def mask_secrets(value: Any) -> Any:
    """递归复制一份并把敏感字段替换为 ***，用于日志输出。"""
    if isinstance(value, dict):
        return {
            k: ("***" if k.lower() in SENSITIVE_KEYS else mask_secrets(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [mask_secrets(v) for v in value]
    return value


@dataclass
class Credentials:
    username: str = ""
    password: str = ""
    client_id: str = "9a1fd200-8687-44b1-4c20-08d50a96e5cd"
    client_secret: str = "8b53f727-08e2-4509-8857-e34bf92b27f2"
    token_endpoint: str = "https://openid.cc98.org/connect/token"
    api_base: str = "https://api.cc98.org"
    scope: str = "cc98-api openid offline_access"

    def has_user_credentials(self) -> bool:
        return bool(self.username and self.password)


@dataclass
class SearchSettings:
    keywords: list[str] = field(default_factory=list)
    match_mode: str = "any"
    # auto：只对关键词两端的 ASCII 字符要求词边界（"AI" 不误命中 "AirPods"/"MacBook Air"）
    # substring：纯子串；word：一律要求词边界
    match_style: str = "auto"
    case_sensitive: bool = False
    hours: float = 24
    # CC98 /topic/search 实测 size 上限为 20（>20 返回 400 invalid_from_or_size）
    page_size: int = 20
    # 每个关键词最多翻几页。实际生效值 = min(本值, PAGE_LIMIT_HARD)，硬上限见常量。
    max_pages_per_keyword: int = PAGE_LIMIT_HARD
    # 整轮运行的抓取时间预算（秒）；0 = 不限。到点后停止未完成的关键词并按已抓到的
    # 内容出报告（标注"不完整"）。避免多关键词把一次运行拖到十几分钟。
    run_time_budget_seconds: float = 180
    # 额外扫一遍全站"最新主题"（/topic/new，按发布时间倒序）作为时效性补充：
    # 搜索索引对刚发布的主题有延迟，这一路负责补上"最近这段"。
    # 实测每页 20 条 ≈ 15 分钟，5 页 ≈ 1 小时多一点。0 = 关闭。
    recent_feed_pages: int = 0
    rate_limit_seconds: float = 1.0

    def effective_max_pages(self) -> int:
        """真正生效的翻页上限：受硬上限约束。"""
        return max(1, min(int(self.max_pages_per_keyword), PAGE_LIMIT_HARD))


@dataclass
class OutputSettings:
    dir: str = "reports"
    formats: list[str] = field(default_factory=lambda: ["md", "csv"])
    # 检索记录保留天数：早于这个天数的报告（md/csv/json）、计划任务日志会被自动删除。
    # 0 = 不清理。当前策略是 3 天。
    keep_days: int = 3
    # 额外保险：reports/ 里最多保留多少组历史报告（0 = 不限）。
    # 每组 = 一次运行的 md/csv/json。默认 10 组，够看最近几轮，又不至于被频繁手动跑堆满。
    max_report_sets: int = 10
    # 计划任务日志最多保留几个文件
    max_scheduler_logs: int = 3
    include_url: bool = True


@dataclass
class RuntimeSettings:
    state_file: str = "state.json"
    log_dir: str = "logs"
    log_level: str = "INFO"
    token_cache: str = ".token.json"
    user_agent: str = "cc98-digest/1.0 (personal read-only digest)"
    timeout_seconds: float = 20.0


@dataclass
class AppConfig:
    credentials: Credentials
    search: SearchSettings
    output: OutputSettings
    runtime: RuntimeSettings
    config_path: Path
    project_root: Path = PROJECT_ROOT
    raw: dict[str, Any] = field(default_factory=dict)

    # ---- 路径解析 ----
    def resolve(self, value: str) -> Path:
        p = Path(value)
        return p if p.is_absolute() else (self.project_root / p)

    @property
    def state_path(self) -> Path:
        return self.resolve(self.runtime.state_file)

    @property
    def token_cache_path(self) -> Path:
        return self.resolve(self.runtime.token_cache)

    @property
    def reports_dir(self) -> Path:
        return self.resolve(self.output.dir)

    @property
    def log_dir(self) -> Path:
        return self.resolve(self.runtime.log_dir)

    def masked(self) -> dict[str, Any]:
        return mask_secrets(self.raw)


def _section(cls: type, data: Any) -> Any:
    """从 dict 构造 dataclass，忽略未知键，保留默认值。"""
    if not isinstance(data, dict):
        return cls()
    allowed = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in allowed})


def load_config(
    path: str | os.PathLike[str] | None = None,
    project_root: Path | None = None,
) -> AppConfig:
    """读取并校验配置。文件不存在时给出可操作的报错。

    project_root 只影响相对路径（reports/logs/state）的解析，测试用；默认项目根目录。
    """
    cfg_path = Path(path) if path else (project_root or PROJECT_ROOT) / "config.json"
    if not cfg_path.exists():
        raise ConfigError(
            f"找不到配置文件 {cfg_path}。请先运行："
            f"python -m cc98digest init"
        )
    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:  # 明确指出行号，便于手改后排查
        raise ConfigError(f"配置文件 {cfg_path} 不是合法 JSON：{exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"配置文件 {cfg_path} 顶层必须是 JSON 对象。")

    creds = _section(Credentials, raw.get("credentials"))
    # 环境变量优先于配置文件
    creds.username = os.environ.get("CC98_USERNAME") or creds.username
    creds.password = os.environ.get("CC98_PASSWORD") or creds.password
    if os.environ.get("CC98_API_BASE"):
        creds.api_base = os.environ["CC98_API_BASE"].rstrip("/")
    if os.environ.get("CC98_TOKEN_ENDPOINT"):
        creds.token_endpoint = os.environ["CC98_TOKEN_ENDPOINT"]

    search = _section(SearchSettings, raw.get("search"))
    output = _section(OutputSettings, raw.get("output"))
    runtime = _section(RuntimeSettings, raw.get("runtime"))

    cfg = AppConfig(
        credentials=creds,
        search=search,
        output=output,
        runtime=runtime,
        config_path=cfg_path,
        project_root=Path(project_root) if project_root else PROJECT_ROOT,
        raw=raw,
    )
    _reconcile(cfg)
    _validate(cfg)
    return cfg


def _reconcile(cfg: AppConfig) -> None:
    """把历史配置里超出接口能力的值就地修正，避免用户升级后被一条报错挡住。"""
    if int(cfg.search.page_size) > PAGE_SIZE_MAX:
        log.warning(
            "search.page_size=%s 超过 CC98 接口上限，已自动降到 %s"
            "（size>%s 会返回 HTTP 400 invalid_from_or_size）。",
            cfg.search.page_size,
            PAGE_SIZE_MAX,
            PAGE_SIZE_MAX,
        )
        cfg.search.page_size = PAGE_SIZE_MAX


def _validate(cfg: AppConfig) -> None:
    if cfg.search.hours <= 0:
        raise ConfigError("search.hours 必须大于 0。")
    if not (1 <= int(cfg.search.page_size) <= PAGE_SIZE_MAX):
        raise ConfigError(
            f"search.page_size 必须在 1..{PAGE_SIZE_MAX} 之间（CC98 /topic/search 实测上限 {PAGE_SIZE_MAX}）。"
        )
    if int(cfg.search.max_pages_per_keyword) < 1:
        raise ConfigError("search.max_pages_per_keyword 必须 >= 1。")
    if cfg.search.run_time_budget_seconds < 0:
        raise ConfigError("search.run_time_budget_seconds 不能为负数（0 表示不限）。")
    if int(cfg.search.recent_feed_pages) < 0:
        raise ConfigError("search.recent_feed_pages 不能为负数（0 表示不扫最新主题）。")
    if cfg.search.rate_limit_seconds < 0:
        raise ConfigError("search.rate_limit_seconds 不能为负数。")
    if cfg.search.match_mode not in {"any", "all"}:
        raise ConfigError("search.match_mode 只能是 any 或 all。")
    if cfg.search.match_style not in {"auto", "substring", "word"}:
        raise ConfigError("search.match_style 只能是 auto / substring / word。")
    formats = {f.lower() for f in cfg.output.formats}
    unknown = formats - {"md", "csv"}
    if unknown:
        raise ConfigError(f"output.formats 只支持 md / csv，收到：{sorted(unknown)}")
    if int(cfg.output.keep_days) < 0:
        raise ConfigError("output.keep_days 不能为负数（0 表示不清理）。")
    if int(cfg.output.max_report_sets) < 0:
        raise ConfigError("output.max_report_sets 不能为负数（0 表示不限）。")
    if not cfg.credentials.api_base.startswith("http"):
        raise ConfigError("credentials.api_base 必须是 http(s) URL。")
    if not cfg.credentials.token_endpoint.startswith("http"):
        raise ConfigError("credentials.token_endpoint 必须是 http(s) URL。")


def normalize_keywords(keywords: list[str] | None) -> list[str]:
    """去空格、去重、保持顺序，忽略空串。"""
    out: list[str] = []
    seen: set[str] = set()
    for kw in keywords or []:
        k = (kw or "").strip()
        if not k:
            continue
        if k in seen:
            continue
        seen.add(k)
        out.append(k)
    return out


# ---------------------------------------------------------------------------
# 主题词写回（图形界面的增删改查用）
# ---------------------------------------------------------------------------

KEYWORD_MAX_LEN = 64
KEYWORD_MAX_COUNT = 50


def sanitize_keywords(keywords: list[str] | None) -> list[str]:
    """清洗输入：转字符串、去首尾空白、丢空串、去掉完全重复项（保持顺序）。"""
    out: list[str] = []
    seen: set[str] = set()
    for raw in keywords or []:
        text = str(raw or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def validate_keywords(keywords: list[str]) -> None:
    """按主题词的业务规则校验；不合法就抛 ConfigError（消息可直接回给界面）。"""
    if not keywords:
        raise ConfigError("主题词至少要留一个。")
    if len(keywords) > KEYWORD_MAX_COUNT:
        raise ConfigError(f"主题词最多 {KEYWORD_MAX_COUNT} 个，当前 {len(keywords)} 个。")
    for kw in keywords:
        if len(kw) > KEYWORD_MAX_LEN:
            raise ConfigError(f"主题词「{kw[:20]}…」太长（上限 {KEYWORD_MAX_LEN} 字）。")
        if any(ch in kw for ch in "\r\n\t"):
            raise ConfigError(f"主题词「{kw}」里有换行/制表符，请去掉。")


def apply_keywords_change(
    current: list[str],
    operation: str,
    value: str | None = None,
    new_value: str | None = None,
    target: str | None = None,
) -> list[str]:
    """纯函数：按操作算出新的主题词列表（不写盘，方便单测）。

    operation:
        add      增加 value
        remove    删除 value
        replace  把整份列表替换成 value（多词用换行或逗号分隔）
        update    把 target 改成 new_value
    """
    result = sanitize_keywords(current)

    if operation == "add":
        text = str(value or "").strip()
        if not text:
            raise ConfigError("要添加的主题词不能为空。")
        if text in result:
            raise ConfigError(f"主题词「{text}」已存在。")
        result.append(text)

    elif operation == "remove":
        text = str(value or "").strip()
        if text not in result:
            raise ConfigError(f"主题词「{text}」不存在。")
        result = [k for k in result if k != text]

    elif operation == "update":
        old = str(target or "").strip()
        new = str(new_value or "").strip()
        if old not in result:
            raise ConfigError(f"要修改的主题词「{old}」不存在。")
        if not new:
            raise ConfigError("新的主题词不能为空。")
        if new != old and new in result:
            raise ConfigError(f"主题词「{new}」已存在。")
        result = [new if k == old else k for k in result]

    elif operation == "replace":
        parts = re.split(r"[\n,，、;；]+", str(value or ""))
        result = sanitize_keywords(parts)

    else:
        raise ConfigError(f"不支持的操作：{operation}")

    validate_keywords(result)
    return result


def update_keywords(
    cfg: AppConfig,
    operation: str,
    value: str | None = None,
    new_value: str | None = None,
    target: str | None = None,
) -> list[str]:
    """改 config.json 里的 search.keywords，原子写盘，并同步到内存里的 cfg。

    只碰 keywords 这一个字段：会把磁盘上的 JSON 原样读回来改（而不是拿 cfg.raw 去写），
    避免把凭据等字段写出意外的形态。配置里的 passwords 不做任何改动。
    """
    path = cfg.config_path
    if not path.exists():
        raise ConfigError(f"找不到配置文件：{path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置文件不是合法 JSON，先手工修好再改主题词：{exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("配置文件顶层必须是 JSON 对象。")

    current = list((raw.get("search") or {}).get("keywords") or [])
    updated = apply_keywords_change(current, operation, value, new_value, target)

    raw.setdefault("search", {})
    if not isinstance(raw["search"], dict):
        raise ConfigError("配置里的 search 必须是对象。")
    raw["search"]["keywords"] = updated

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)

    cfg.search.keywords = list(updated)
    cfg.raw = raw
    log.info("主题词已更新（%s）：%s", operation, "、".join(updated))
    return updated


def update_credentials(cfg: AppConfig, username: str, password: str) -> None:
    """把登录成功的账号密码写回 config.json（原子写盘），并同步内存 cfg。

    只碰 credentials.username / credentials.password 两个字段，其余（client_id、
    client_secret、token_endpoint 等公共值）保持原样。由登录流程在「换令牌成功之后」
    调用，因此这里不做非空校验——传空串即用于「退出登录」清空凭据。
    """
    path = cfg.config_path
    if not path.exists():
        raise ConfigError(f"找不到配置文件：{path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置文件不是合法 JSON，先手工修好再登录：{exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("配置文件顶层必须是 JSON 对象。")

    raw.setdefault("credentials", {})
    if not isinstance(raw["credentials"], dict):
        raise ConfigError("配置里的 credentials 必须是对象。")
    raw["credentials"]["username"] = username
    raw["credentials"]["password"] = password

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)

    cfg.credentials.username = username
    cfg.credentials.password = password
    cfg.raw = raw
    log.info("登录账号已写入 config.json：%s", username)


# 提供 init 子命令使用的模板数据
def load_example_dict() -> dict[str, Any]:
    return json.loads(EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8"))
