"""本地图形界面服务：标准库 HTTP 服务 + 单页前端。

设计要点：
- 只用标准库（http.server / socketserver / threading / json / webbrowser），不引入新依赖；
- 前端是内嵌在 static/index.html 的单页应用（HTML+CSS+JS，无构建步骤、无 CDN）；
- 抓取在后台线程跑，前端靠轮询 /api/status 拿实时进度、靠 SSE /api/logs/stream 看日志滚动；
- 所有输出走 /api/*，stdout 保持干净（GUI 模式下重定向日志输出，避免污染响应体）。
"""

from __future__ import annotations

import io
import json
import logging
import os
import subprocess
import threading
import time
import webbrowser
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import (
    AppConfig,
    ConfigError,
    KEYWORD_MAX_COUNT,
    KEYWORD_MAX_LEN,
    normalize_keywords,
    update_credentials,
    update_keywords,
)
from .logging_setup import get_logger
from .window import SHANGHAI

log = get_logger("gui")

STATIC_DIR = Path(__file__).resolve().parent / "static"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8848
LOG_BUFFER_SIZE = 800
# 计划任务状态的缓存时间：前端每秒轮询，不能让每次轮询都去拉一个子进程。
# 计划任务的「下次运行时间」变化很慢，5 分钟刷新一次足够。
TASK_STATUS_CACHE_SECONDS = 300.0


def _no_window_flags() -> int:
    """Windows 下让子进程不弹控制台窗口。

    服务由 pythonw.exe 运行（无控制台），若子进程不带这个标志，Windows 会为它
    新建一个控制台——那种"每秒闪一下空白终端"就是这么来的。
    """
    if os.name != "nt":
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


class LogBuffer(logging.Handler):
    """把日志按行放进环形缓冲，并通知等待中的 SSE 连接。"""

    def __init__(self, capacity: int = LOG_BUFFER_SIZE):
        super().__init__()
        self._lines: deque[str] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._seq = 0
        self._condition = threading.Condition(self._lock)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = self.format(record)
        except Exception:  # pragma: no cover - 日志失败不能影响主流程
            return
        with self._condition:
            self._seq += 1
            self._lines.append(f"{self._seq}\t{text}")
            self._condition.notify_all()

    def snapshot(self, since: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            out = []
            for raw in self._lines:
                seq_text, _, text = raw.partition("\t")
                seq = int(seq_text)
                if seq > since:
                    out.append({"seq": seq, "text": text})
            return out

    @property
    def seq(self) -> int:
        with self._lock:
            return self._seq

    def wait_for_new(self, since: int, timeout: float = 15.0) -> list[dict[str, Any]]:
        """阻塞等待新日志（用于 SSE），超时返回空列表让上层发心跳。"""
        with self._condition:
            if self._seq <= since:
                self._condition.wait(timeout=timeout)
            out = []
            for raw in self._lines:
                seq_text, _, text = raw.partition("\t")
                seq = int(seq_text)
                if seq > since:
                    out.append({"seq": seq, "text": text})
            return out


@dataclass
class RunState:
    running: bool = False
    started_at: datetime | None = None
    finished_at: datetime | None = None
    outcome: str = "idle"  # idle | running | ok | error
    error: str | None = None
    message: str = ""
    trigger: str = "manual"
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "status": self.outcome,
            "trigger": self.trigger,
            "message": self.message,
            "error": self.error,
            "startedAt": self.started_at.isoformat() if self.started_at else None,
            "finishedAt": self.finished_at.isoformat() if self.finished_at else None,
            "elapsed": (
                round(((self.finished_at or datetime.now(SHANGHAI)) - self.started_at).total_seconds(), 1)
                if self.started_at
                else None
            ),
            "params": self.params,
        }


class RunManager:
    """管理"手动触发一次抓取"的后台线程，同一时刻只允许一个。"""

    def __init__(self, cfg: AppConfig, log_buffer: LogBuffer):
        self.cfg = cfg
        self.log_buffer = log_buffer
        self.state = RunState()
        self._lock = threading.Lock()

    def start(
        self,
        hours: float | None = None,
        keywords: list[str] | None = None,
        all_time: bool = False,
        page_size: int | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if self.state.running:
                return {"started": False, "reason": "已有一次抓取在进行中"}
            self.state = RunState(
                running=True,
                outcome="running",
                started_at=datetime.now(SHANGHAI),
                message="正在抓取…",
                trigger="manual",
                params={
                    "hours": hours,
                    "keywords": keywords,
                    "allTime": all_time,
                    "pageSize": page_size,
                },
            )

        thread = threading.Thread(
            target=self._worker,
            args=(hours, keywords, all_time, page_size),
            name="cc98-digest-run",
            daemon=True,
        )
        thread.start()
        return {"started": True}

    def _worker(
        self,
        hours: float | None,
        keywords: list[str] | None,
        all_time: bool,
        page_size: int | None,
    ) -> None:
        from .cli import run_digest  # 延迟导入，避免循环依赖

        log.info("界面触发抓取：hours=%s keywords=%s", hours, keywords)
        try:
            outcome = run_digest(
                self.cfg,
                hours=hours,
                keywords=normalize_keywords(keywords) if keywords else None,
                all_time=all_time,
                page_size=page_size,
            )
            self.state.outcome = "ok"
            self.state.message = f"完成：命中 {len(outcome.run.topics)} 条"
            log.info(self.state.message)
        except ConfigError as exc:
            self.state.outcome = "error"
            self.state.error = f"配置错误：{exc}"
            log.error(self.state.error)
        except Exception as exc:  # 网络/登录/接口错误都归到这里，页面会显示原因
            self.state.outcome = "error"
            self.state.error = f"{type(exc).__name__}: {exc}"
            log.error("抓取失败：%s", self.state.error)
        finally:
            self.state.running = False
            self.state.finished_at = datetime.now(SHANGHAI)


class GuiHandler(BaseHTTPRequestHandler):
    server_version = "cc98-digest-gui"
    protocol_version = "HTTP/1.1"

    # ---- 基础设施 ----
    @property
    def app(self) -> "GuiApp":
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:  # 别把访问日志写进业务日志
        log.debug("http %s", fmt % args)

    def _send(self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError):  # 浏览器提前关连接
            pass

    def _send_json(self, payload: Any, status: int = 200) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _send_text(self, text: str, status: int = 200, content_type="text/plain; charset=utf-8"):
        self._send(status, text.encode("utf-8"), content_type)

    def _body_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}

    # ---- 路由 ----
    def do_GET(self) -> None:  # noqa: N802 (标准库命名)
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._serve_static("index.html")
        if path == "/favicon.ico":
            return self._serve_static("favicon.svg", fallback_status=200,
                                      fallback_type="image/svg+xml")
        if path.startswith("/static/"):
            return self._serve_static(path[len("/static/"):])
        if path == "/api/report":
            return self._api_report()
        if path == "/api/keywords":
            return self._send_json({"ok": True, "keywords": self.app.keywords()})
        if path == "/api/netcheck":
            return self._api_netcheck()
        if path == "/api/status":
            return self._api_status()
        if path == "/api/history":
            return self._api_history()
        if path == "/api/logs":
            since = int(parse_qs(urlparse(self.path).query).get("since", ["0"])[0] or 0)
            return self._send_json({"lines": self.app.log_buffer.snapshot(since)})
        if path == "/api/logs/stream":
            return self._api_log_stream()
        return self._send_json({"error": "not found", "path": path}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/run":
            return self._api_run()
        if path == "/api/auth-test":
            return self._api_auth_test()
        if path == "/api/login":
            return self._api_login()
        if path == "/api/logout":
            return self._api_logout()
        if path == "/api/keywords":
            return self._api_keywords_write()
        if path == "/api/shutdown":
            self._send_json({"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        return self._send_json({"error": "not found", "path": path}, status=404)

    def do_PUT(self) -> None:  # noqa: N802
        if urlparse(self.path).path == "/api/keywords":
            return self._api_keywords_replace()
        return self._send_json({"error": "not found", "path": self.path}, status=404)

    def do_PATCH(self) -> None:  # noqa: N802
        if urlparse(self.path).path == "/api/keywords":
            return self._api_keywords_patch()
        return self._send_json({"error": "not found", "path": self.path}, status=404)

    def do_DELETE(self) -> None:  # noqa: N802
        if urlparse(self.path).path == "/api/keywords":
            return self._api_keywords_delete()
        return self._send_json({"error": "not found", "path": self.path}, status=404)

    # ---- 静态文件 ----
    def _serve_static(self, name: str, fallback_status: int = 404, fallback_type: str | None = None):
        # 防目录穿越：只允许 STATIC_DIR 下的普通文件
        candidate = (STATIC_DIR / name).resolve()
        if STATIC_DIR.resolve() not in candidate.parents and candidate != STATIC_DIR.resolve():
            return self._send_json({"error": "forbidden"}, status=403)
        if not candidate.is_file():
            if fallback_type:
                body = _FALLBACK_FAVICON.encode("utf-8")
                return self._send(fallback_status, body, fallback_type)
            return self._send_json({"error": f"{name} not found"}, status=404)
        content_type = _CONTENT_TYPES.get(candidate.suffix.lower(), "application/octet-stream")
        self._send(200, candidate.read_bytes(), content_type)

    # ---- API ----
    def _api_report(self) -> None:
        requested = parse_qs(urlparse(self.path).query).get("file", [None])[0]
        payload = self.app.load_report(requested)
        if payload is None:
            return self._send_json(
                {
                    "schema": 1,
                    "empty": True,
                    "hint": "还没有报告。点右上角「立即抓取」跑一次，或先跑一次命令行。",
                    "config": self.app.public_config(),
                    "status": self.app.manager.state.to_dict(),
                }
            )
        payload["config"] = self.app.public_config()
        payload["status"] = self.app.manager.state.to_dict()
        self._send_json(payload)

    def _api_status(self) -> None:
        self._send_json(
            {
                "status": self.app.manager.state.to_dict(),
                "config": self.app.public_config(),
                "task": self.app.task_status(),
                "lastLogSeq": self.app.log_buffer.seq,
            }
        )

    def _api_history(self) -> None:
        self._send_json({"items": self.app.history()})

    def _api_run(self) -> None:
        body = self._body_json()
        hours = body.get("hours")
        try:
            hours = float(hours) if hours not in (None, "") else None
        except (TypeError, ValueError):
            return self._send_json({"started": False, "reason": "hours 必须是数字"}, status=400)
        page_size = body.get("pageSize")
        try:
            page_size = int(page_size) if page_size not in (None, "") else None
        except (TypeError, ValueError):
            return self._send_json({"started": False, "reason": "pageSize 必须是整数"}, status=400)
        keywords = body.get("keywords") or None
        if isinstance(keywords, str):
            keywords = [k.strip() for k in keywords.replace("，", ",").split(",") if k.strip()]
        result = self.app.manager.start(
            hours=hours,
            keywords=keywords if isinstance(keywords, list) else None,
            all_time=bool(body.get("allTime")),
            page_size=page_size,
        )
        self._send_json(result, status=202 if result.get("started") else 409)

    def _api_netcheck(self) -> None:
        """网络自检：代理 / DNS / 到 CC98 的连通性。连不上时先看这个。"""
        from . import net as net_mod

        try:
            diag = net_mod.diagnose(self.app.cfg)
        except Exception as exc:
            return self._send_json(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=500
            )
        self._send_json(
            {
                "ok": True,
                "proxyMode": diag["proxy_mode"],
                "systemProxy": diag.get("system_proxy") or {},
                "envProxy": diag.get("env_proxy") or {},
                "dns": diag["dns"],
                "probes": [
                    {
                        "label": p.label,
                        "target": p.target,
                        "ok": p.ok,
                        "detail": p.detail,
                        "elapsedMs": round(p.elapsed_ms),
                    }
                    for p in diag["probes"]
                ],
                "allOk": all(p.ok for p in diag["probes"]),
            }
        )

    def _api_auth_test(self) -> None:
        from .auth import AuthError, get_token
        from .api import ApiClient, as_list

        try:
            token = get_token(self.app.cfg, force_login=True)
            client = ApiClient(self.app.cfg, lambda force=False: get_token(self.app.cfg, force_login=force).access_token)
            keywords = normalize_keywords(self.app.cfg.search.keywords) or ["AI"]
            probe = as_list(client.get_json("/topic/search", {"keyword": keywords[0], "from": 0, "size": 1}))
            self._send_json(
                {
                    "ok": True,
                    "account": self.app.cfg.credentials.username,
                    "expiresIn": token.seconds_left,
                    "probe": f"关键词「{keywords[0]}」返回 {len(probe)} 条样例",
                }
            )
        except AuthError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=200)
        except Exception as exc:
            self._send_json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=200)

    def _api_login(self) -> None:
        from .auth import AuthError, login_with_password

        body = self._body_json()
        username = str(body.get("username") or "").strip()
        password = str(body.get("password") or "")
        if not username or not password:
            return self._send_json({"ok": False, "error": "用户名和密码都不能为空。"}, status=400)
        if self.app.manager.state.running:
            return self._send_json(
                {"ok": False, "error": "正在抓取中，等这次跑完再登录（避免换账号和结果对不上）。"},
                status=409,
            )
        try:
            token = login_with_password(self.app.cfg, username, password)
            update_credentials(self.app.cfg, username, password)
            self._send_json({"ok": True, "account": username, "expiresIn": token.seconds_left})
        except AuthError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=200)
        except Exception as exc:
            self._send_json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=200)

    def _api_logout(self) -> None:
        update_credentials(self.app.cfg, "", "")
        try:
            self.app.cfg.token_cache_path.unlink(missing_ok=True)
        except OSError:
            pass
        self._send_json({"ok": True, "account": ""})

    # ---- 主题词增删改查 ----
    def _keywords_result(self, operation: str, keywords: list[str]) -> None:
        self._send_json(
            {
                "ok": True,
                "operation": operation,
                "keywords": keywords,
                "note": "已写入 config.json；当前报告不会变，点「立即抓取」才会按新词检索。",
            }
        )

    def _keywords_error(self, exc: Exception) -> None:
        self._send_json({"ok": False, "error": str(exc)}, status=400)

    def _api_keywords_write(self) -> None:
        """POST /api/keywords —— 增加一个主题词。"""
        body = self._body_json()
        value = body.get("value")
        if value in (None, ""):
            return self._send_json({"ok": False, "error": "缺少 value"}, status=400)
        try:
            updated = self.app.change_keywords("add", value=str(value))
        except ConfigError as exc:
            return self._keywords_error(exc)
        self._keywords_result("add", updated)

    def _api_keywords_replace(self) -> None:
        """PUT /api/keywords —— 用一份新列表整表替换。"""
        body = self._body_json()
        raw = body.get("keywords")
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            return self._send_json({"ok": False, "error": "keywords 必须是数组"}, status=400)
        try:
            updated = self.app.change_keywords("replace", value="\n".join(str(x) for x in raw))
        except ConfigError as exc:
            return self._keywords_error(exc)
        self._keywords_result("replace", updated)

    def _api_keywords_delete(self) -> None:
        """DELETE /api/keywords?value=xxx —— 删除一个主题词。"""
        query = parse_qs(urlparse(self.path).query)
        value = query.get("value", [None])[0]
        if value in (None, ""):
            return self._send_json({"ok": False, "error": "缺少 value 参数"}, status=400)
        try:
            updated = self.app.change_keywords("remove", value=value)
        except ConfigError as exc:
            return self._keywords_error(exc)
        self._keywords_result("remove", updated)

    def _api_keywords_patch(self) -> None:
        """PATCH /api/keywords —— 把某个主题词改成新的。"""
        body = self._body_json()
        target = body.get("target") or body.get("value")
        new_value = body.get("newValue") or body.get("new")
        if not target or not new_value:
            return self._send_json({"ok": False, "error": "缺少 target / newValue"}, status=400)
        try:
            updated = self.app.change_keywords("update", target=str(target), new_value=str(new_value))
        except ConfigError as exc:
            return self._keywords_error(exc)
        self._keywords_result("update", updated)

    def _api_log_stream(self) -> None:
        """SSE：持续推送新日志行。"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        since = int(parse_qs(urlparse(self.path).query).get("since", ["0"])[0] or 0)
        try:
            for line in self.app.log_buffer.snapshot(since):
                self._sse("log", line)
                since = line["seq"]
            while True:
                lines = self.app.log_buffer.wait_for_new(since, timeout=15.0)
                if not lines:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    continue
                for line in lines:
                    self._sse("log", line)
                    since = line["seq"]
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
            return

    def _sse(self, event: str, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False)
        self.wfile.write(f"event: {event}\ndata: {data}\n\n".encode("utf-8"))
        self.wfile.flush()


class GuiApp:
    def __init__(self, cfg: AppConfig, log_buffer: LogBuffer):
        self.cfg = cfg
        self.log_buffer = log_buffer
        self.manager = RunManager(cfg, log_buffer)
        self._keyword_lock = threading.Lock()  # 主题词写盘的读-改-写要串行，避免互相覆盖
        self._task_cache: dict[str, Any] | None = None
        self._task_cache_at: float = 0.0
        self._task_refreshing = False

    # ---- 数据 ----
    def load_report(self, filename: str | None = None) -> dict[str, Any] | None:
        """读一份报告 JSON；filename 为空时读 latest.json。文件名做白名单校验，防目录穿越。"""
        if filename:
            name = Path(filename).name  # 只取文件名部分
            if not (name.startswith("cc98-digest-") and name.endswith(".json")):
                log.warning("拒绝读取非报告文件：%s", filename)
                return None
        else:
            name = "latest.json"
        path = self.cfg.reports_dir / name
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("读取 %s 失败：%s", path, exc)
            return None

    def history(self, limit: int = 30) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for path in sorted(self.cfg.reports_dir.glob("cc98-digest-*.json"), reverse=True)[:limit]:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            items.append(
                {
                    "file": path.name,
                    "runAt": data.get("runAt"),
                    "runLabel": data.get("runLabel"),
                    "windowLabel": data.get("windowLabel"),
                    "totalTopics": data.get("totalTopics"),
                    "totalScanned": data.get("totalScanned"),
                    "keywords": data.get("keywords"),
                }
            )
        return items

    def public_config(self) -> dict[str, Any]:
        """给前端看的配置摘要（绝不含密码）。"""
        return {
            "keywords": normalize_keywords(self.cfg.search.keywords),
            "hours": self.cfg.search.hours,
            "pageSize": self.cfg.search.page_size,
            "maxPages": self.cfg.search.effective_max_pages(),
            "matchStyle": self.cfg.search.match_style,
            "matchMode": self.cfg.search.match_mode,
            "rateLimit": self.cfg.search.rate_limit_seconds,
            "reportsDir": str(self.cfg.reports_dir),
            "projectRoot": str(self.cfg.project_root),
            "apiBase": self.cfg.credentials.api_base,
            "account": self.cfg.credentials.username,
            "keywordLimits": {"maxLen": KEYWORD_MAX_LEN, "maxCount": KEYWORD_MAX_COUNT},
        }

    # ---- 主题词（读写 config.json） ----
    def keywords(self) -> list[str]:
        return normalize_keywords(self.cfg.search.keywords)

    def change_keywords(
        self,
        operation: str,
        value: str | None = None,
        new_value: str | None = None,
        target: str | None = None,
    ) -> list[str]:
        with self._keyword_lock:
            if self.manager.state.running:
                raise ConfigError("正在抓取中，等这次跑完再改主题词（避免这次运行的词和结果对不上）。")
            return update_keywords(
                self.cfg, operation, value=value, new_value=new_value, target=target
            )

    # ---- 计划任务状态：后台异步刷新 + 缓存，绝不阻塞请求、绝不在请求线程里拉进程 ----
    def task_status(self) -> dict[str, Any]:
        """Windows 计划任务状态。

        三个要点：
        1. 请求线程只读缓存、立刻返回——前端每秒轮询，不能每次都去调 PowerShell；
        2. 缓存过期时在**后台线程**刷新（同一时刻只允许一个刷新线程）；
        3. 子进程带 CREATE_NO_WINDOW：服务是 pythonw 起的（无控制台），
           不带这个标志 Windows 会给 powershell 新建控制台窗口，表现为「每秒闪一下空白终端」。
        """
        now = time.monotonic()
        stale = self._task_cache is None or (now - self._task_cache_at) >= TASK_STATUS_CACHE_SECONDS
        if stale and not self._task_refreshing:
            self._task_refreshing = True
            threading.Thread(
                target=self._refresh_task_status, name="cc98-task-status", daemon=True
            ).start()
        return self._task_cache or {
            "available": False,
            "checking": True,
            "name": "CC98TopicDigest",
        }

    def _refresh_task_status(self) -> None:
        try:
            self._task_cache = self._query_task_status()
            self._task_cache_at = time.monotonic()
        except Exception as exc:  # 后台线程异常不能静默吞掉
            log.warning("查询计划任务状态失败：%s", exc)
            self._task_cache = {"available": False, "name": "CC98TopicDigest", "error": str(exc)}
            self._task_cache_at = time.monotonic()
        finally:
            self._task_refreshing = False

    def _query_task_status(self) -> dict[str, Any]:
        name = "CC98TopicDigest"
        try:
            completed = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-NonInteractive",
                    "-WindowStyle",
                    "Hidden",
                    "-Command",
                    f"$t = Get-ScheduledTask -TaskName '{name}' -ErrorAction SilentlyContinue; "
                    "if ($null -eq $t) { 'MISSING' } else { "
                    "$i = $t | Get-ScheduledTaskInfo; "
                    '"STATE=$($t.State)|NEXT=$($i.NextRunTime)|LAST=$($i.LastTaskResult)" }',
                ],
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
                creationflags=_no_window_flags(),
            )
            out = (completed.stdout or "").strip()
        except Exception as exc:
            return {"available": False, "name": name, "error": str(exc)}
        if not out or "MISSING" in out:
            return {"available": False, "name": name, "installed": False}
        parts = dict(p.split("=", 1) for p in out.split("|") if "=" in p)
        return {
            "available": True,
            "installed": True,
            "name": name,
            "state": parts.get("STATE"),
            "nextRun": parts.get("NEXT"),
            "lastResult": parts.get("LAST"),
        }


_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".json": "application/json; charset=utf-8",
    ".woff2": "font/woff2",
    ".ico": "image/x-icon",
}

_FALLBACK_FAVICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    '<rect width="64" height="64" rx="8" fill="#1c1a17"/>'
    '<text x="32" y="44" font-size="36" text-anchor="middle" fill="#d6452c" '
    'font-family="serif">98</text></svg>'
)


def _silence_stdout() -> None:
    """GUI 模式把 print 的输出接住，保证 stdout 只留给服务本身的提示。"""
    import sys

    sys.stdout = io.StringIO()


def serve(
    cfg: AppConfig,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    open_browser: bool = True,
    quiet: bool = False,
) -> int:
    """启动界面服务。

    quiet=True 用于开机自启：不接管 stdout、不打印提示（那个进程没有控制台）。
    """
    log_buffer = LogBuffer()
    log_buffer.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", "%H:%M:%S"))
    logging.getLogger("cc98digest").addHandler(log_buffer)

    app = GuiApp(cfg, log_buffer)
    try:
        httpd = ThreadingHTTPServer((host, port), GuiHandler)
    except OSError as exc:
        raise ConfigError(
            f"无法监听 {host}:{port}（{exc}）。换个端口：python -m cc98digest gui --port 8849"
        ) from exc
    httpd.app = app  # type: ignore[attr-defined]
    httpd.daemon_threads = True

    url = f"http://{host}:{port}/"
    if not quiet:
        _silence_stdout()
    log.info("界面已启动：%s", url)
    log.info("账号：%s；主题词：%s", cfg.credentials.username or "（未配置）",
             "、".join(normalize_keywords(cfg.search.keywords)) or "（未配置）")
    if not quiet:
        print(f"cc98-digest 界面已启动：{url}")
        print("按 Ctrl+C 停止。")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        if not quiet:
            print("\n正在停止…")
    finally:
        httpd.shutdown()
        httpd.server_close()
    return 0
