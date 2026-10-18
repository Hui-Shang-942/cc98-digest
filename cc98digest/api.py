"""CC98 REST API 客户端：限速、重试、响应归一化、搜索分页。

只用只读接口：
    GET /topic/search?keyword=&from=&size=
"""

from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from .config import AppConfig
from .logging_setup import get_logger

log = get_logger("api")

from . import net  # noqa: E402

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 3
# CC98 在请求过密时会直接回 403（body 为空），恢复需要几十秒量级
THROTTLE_STATUS = 403
THROTTLE_COOLDOWN_SECONDS = 45.0


class ApiError(Exception):
    """API 调用失败（重试后仍失败 / 返回体无法解析）。"""


@dataclass
class RecentSweep:
    """全站"最新主题"扫描结果（时效性最好的补充来源）。"""

    topics: list[dict[str, Any]] = None  # type: ignore[assignment]
    pages: int = 0
    scanned: int = 0
    oldest_at: Any = None
    stopped_before_window: bool = False
    capped: bool = False

    def __post_init__(self) -> None:
        if self.topics is None:
            self.topics = []


@dataclass
class PaginationResult:
    """单关键词的扫描结果（用于报告里的数据完备性说明）。"""

    keyword: str
    scanned: int = 0
    matched: int = 0
    pages: int = 0
    capped: bool = False
    interrupted: bool = False
    budget_exceeded: bool = False
    parse_failures: int = 0
    topics: list[dict[str, Any]] = None  # type: ignore[assignment]

    @property
    def partial(self) -> bool:
        """结果是否可能不完整（触顶 / 被中断 / 超出时间预算）。"""
        return self.capped or self.interrupted or self.budget_exceeded

    def __post_init__(self) -> None:
        if self.topics is None:
            self.topics = []


def as_list(payload: Any) -> list[dict[str, Any]]:
    """把 CC98 可能的多种响应形态统一成 list[dict]。

    兼容：裸数组、{"data": [...]}、{"value": [...]}、{"items": [...]}。
    非 dict 元素被丢弃（避免下游到处防御）。
    """
    if payload is None:
        return []
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("data", "value", "items", "results", "topics"):
            inner = payload.get(key)
            if isinstance(inner, list):
                return [x for x in inner if isinstance(x, dict)]
        # 单对象响应：包装成 1 元素列表，交由上层按字段判断
        return [payload] if payload.get("id") is not None else []
    return []


class ApiClient:
    def __init__(self, cfg: AppConfig, token_provider: Callable[..., str]):
        self.cfg = cfg
        self.api_base = cfg.credentials.api_base.rstrip("/")
        self.timeout = float(cfg.runtime.timeout_seconds)
        self.user_agent = cfg.runtime.user_agent
        self.rate_limit = float(cfg.search.rate_limit_seconds)
        self._token_provider = token_provider
        self._last_request_at = 0.0
        # 默认忽略系统代理：CC98 是校内服务，走代理会得到 10061 / TLS 中断
        self._opener = net.build_opener(cfg)

    # ---- 底层 HTTP ----
    def _throttle(self) -> None:
        if self.rate_limit <= 0:
            return
        wait = self.rate_limit - (time.monotonic() - self._last_request_at)
        if wait > 0:
            log.debug("限速等待 %.2fs", wait)
            time.sleep(wait)

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = self.api_base + path
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            url = f"{url}?{urllib.parse.urlencode(clean)}"

        last_error: Exception | None = None
        attempts = MAX_ATTEMPTS
        attempt = 0
        while attempt < attempts:
            attempt += 1
            self._throttle()
            try:
                return self._single_get(url)
            except _RetryableError as exc:
                last_error = exc
                if attempt == attempts:
                    break
                if exc.retry_after is not None:
                    backoff = exc.retry_after
                elif exc.throttled:
                    backoff = THROTTLE_COOLDOWN_SECONDS
                else:
                    backoff = 2 ** (attempt - 1)
                backoff += random.uniform(0, 0.3)  # 抖动，避免整点撞车
                log.warning(
                    "请求失败（第 %s/%s 次）：%s；%.1fs 后重试。",
                    attempt,
                    attempts,
                    exc,
                    backoff,
                )
                time.sleep(backoff)
        raise ApiError(f"请求 {url} 连续失败 {attempts} 次：{last_error}")

    def _single_get(self, url: str) -> Any:
        """一次实际请求。401 时强制换新令牌并就地重试一次。"""
        for token_attempt in (1, 2):
            token = self._token_provider(force=(token_attempt == 2))
            if not isinstance(token, str) or not token.strip():
                # 防御：曾经因为传了 Token 对象，拼出 `Bearer Token(access_token=...)` 而全 401
                raise ApiError(
                    f"token_provider 必须返回令牌字符串，实际返回 {type(token).__name__}。"
                )
            req = urllib.request.Request(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                    "User-Agent": self.user_agent,
                },
            )
            try:
                with self._opener.open(req, timeout=self.timeout) as resp:
                    self._last_request_at = time.monotonic()
                    raw = resp.read().decode("utf-8", "replace")
                    return _parse_json(raw, url)
            except urllib.error.HTTPError as exc:
                self._last_request_at = time.monotonic()
                if exc.code == 401:
                    if token_attempt == 1:
                        log.warning("接口返回 401，强制刷新令牌后重试一次。")
                        self._forget_token()
                        continue
                    raise ApiError(
                        f"请求 {url} 始终返回 401 Unauthorized，请检查账号权限或重新运行 auth-test。"
                    ) from exc
                if exc.code in RETRYABLE_STATUS:
                    retry_after = _retry_after_seconds(exc)
                    raise _RetryableError(f"HTTP {exc.code} {exc.reason}", retry_after=retry_after)
                if exc.code == THROTTLE_STATUS:
                    # CC98 请求过密时回 403 且 body 为空；这里当作"被限流"并长退避
                    raise _RetryableError(
                        f"HTTP {exc.code}（疑似请求过密被限流）",
                        retry_after=_retry_after_seconds(exc),
                        throttled=True,
                    )
                body = ""
                try:
                    body = exc.read().decode("utf-8", "replace")[:300]
                except Exception:  # pragma: no cover
                    pass
                raise ApiError(f"请求 {url} 返回 HTTP {exc.code} {exc.reason} {body}") from exc
            except urllib.error.URLError as exc:
                self._last_request_at = time.monotonic()
                raise _RetryableError(net.describe_error(exc, url, self.cfg)) from exc
            except TimeoutError as exc:
                self._last_request_at = time.monotonic()
                raise _RetryableError(net.describe_error(exc, url, self.cfg)) from exc
        raise ApiError(f"请求 {url} 失败：401 重试后仍未成功。")  # pragma: no cover

    def _forget_token(self) -> None:
        """尽力让上层忘掉当前令牌（支持 forget() 钩子时优先用它）。"""
        forget = getattr(self._token_provider, "forget", None)
        if callable(forget):
            try:
                forget()
            except Exception as exc:  # pragma: no cover - 钩子失败不影响后续重试
                log.warning("调用 token_provider.forget() 失败：%s", exc)

    # ---- 业务接口 ----
    def sweep_recent_topics(
        self,
        window_start: Any,
        deadline: float | None = None,
        max_pages: int | None = None,
    ) -> RecentSweep:
        """按时间倒序扫全站最新主题（`/topic/new`），作为搜索之外的时效性补充。

        `/topic/new` 每页 20 条、约 15 分钟的时间跨度，并且**是按发布时间倒序**的——
        这是唯一能"按时间向前走"的接口，正好补上搜索索引对刚发布主题的延迟。
        某页里已经出现早于窗口的主题时停止（再往后只会更旧）。
        """
        from .window import parse_cc98_time  # 局部导入，避免循环依赖

        size = int(self.cfg.search.page_size)
        limit = int(max_pages if max_pages is not None else self.cfg.search.recent_feed_pages)
        sweep = RecentSweep()
        if limit <= 0:
            return sweep

        for page in range(limit):
            if deadline is not None and time.monotonic() >= deadline:
                sweep.capped = True
                log.warning("整轮时间预算已用尽：最新主题扫描停在 %s 页。", sweep.pages)
                return sweep
            try:
                payload = self.get_json("/topic/new", {"from": page * size, "size": size})
            except ApiError as exc:
                log.warning("最新主题扫描第 %s 页失败：%s", page + 1, exc)
                sweep.capped = True
                return sweep
            items = as_list(payload)
            sweep.pages += 1
            sweep.scanned += len(items)
            sweep.topics.extend(items)
            log.info("最新主题第 %s 页：%s 条", page + 1, len(items))
            if not items:
                return sweep

            times = [t for t in (parse_cc98_time(i.get("time")) for i in items) if t]
            if times:
                sweep.oldest_at = min(times)
                if window_start is not None and min(times) < window_start:
                    sweep.stopped_before_window = True
                    log.debug("最新主题第 %s 页已早于时间窗口，停止。", page + 1)
                    return sweep
            if len(items) < size:
                return sweep

        sweep.capped = True
        log.warning("最新主题扫描达到 %s 页上限（共 %s 条）。", limit, sweep.scanned)
        return sweep

    def search_topics(
        self,
        keyword: str,
        on_page: Callable[[list[dict[str, Any]]], int] | None = None,
        deadline: float | None = None,
    ) -> PaginationResult:
        """按关键词搜索主题帖并翻页，直到自然结束或达到页数上限。

        on_page 回调接收每页主题列表，返回该页"需要继续翻页的条数"；返回 0 表示可以提前
        停止（例如本页所有帖子都早于时间窗口）。默认不提前停止。
        deadline 是 time.monotonic() 时钟上的整轮截止时刻；到点后按已抓到的内容返回。
        """
        size = int(self.cfg.search.page_size)
        max_pages = self.cfg.search.effective_max_pages()
        result = PaginationResult(keyword=keyword)

        for page in range(max_pages):
            if deadline is not None and time.monotonic() >= deadline:
                result.budget_exceeded = True
                log.warning(
                    "整轮时间预算已用尽：关键词「%s」停在已抓到的 %s 页 / %s 条。",
                    keyword,
                    result.pages,
                    result.scanned,
                )
                return result
            from_ = page * size
            try:
                payload = self.get_json(
                    "/topic/search", {"keyword": keyword, "from": from_, "size": size}
                )
            except KeyboardInterrupt:
                # 用户按了 Ctrl+C：保留已抓到的结果，回上层继续出报告
                result.interrupted = True
                log.warning(
                    "关键词「%s」被手动中断（已扫 %s 条），将基于已有结果出报告。",
                    keyword,
                    result.scanned,
                )
                return result
            page_items = as_list(payload)
            result.pages += 1
            result.scanned += len(page_items)
            result.topics.extend(page_items)
            log.info("关键词「%s」第 %s 页：%s 条", keyword, page + 1, len(page_items))

            if len(page_items) < size:
                log.debug("关键词「%s」已到结果末尾。", keyword)
                return result
            if on_page is not None and on_page(page_items) == 0:
                log.debug("关键词「%s」本页已全部早于时间窗口（或全被过滤），停止翻页。", keyword)
                return result

        result.capped = True
        log.warning(
            "关键词「%s」达到翻页上限 %s 页（%s 条），结果可能不完整。",
            keyword,
            max_pages,
            result.scanned,
        )
        return result


class _RetryableError(Exception):
    def __init__(
        self, message: str, retry_after: float | None = None, throttled: bool = False
    ):
        super().__init__(message)
        self.retry_after = retry_after
        self.throttled = throttled


def _retry_after_seconds(exc: urllib.error.HTTPError) -> float | None:
    value = exc.headers.get("Retry-After") if exc.headers else None
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


def _parse_json(raw: str, url: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ApiError(f"响应不是合法 JSON（{url}）：{exc}; 前 200 字符：{raw[:200]}") from exc
