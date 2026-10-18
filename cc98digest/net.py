"""网络层公共逻辑：代理策略、统一 opener、连接错误的中文解释、网络自检。

为什么需要这一层：
- CC98（cc98.org / api.cc98.org / openid.cc98.org）是**浙大校内服务**，真实地址是
  10.10.98.98 这类内网 IP，走代理基本必然失败；
- 但 Python 的 urllib 默认会读取 Windows 系统代理（注册表 ProxyEnable/ProxyServer）
  以及 HTTP(S)_PROXY 环境变量。用户机器上只要开着 Clash / v2ray 之类软件，
  请求就会被导向 127.0.0.1:789x；代理没运行或不转发内网地址时，
  报错就是 `[WinError 10061] 由于目标计算机积极拒绝，无法连接。`——很难看出真正原因。
- 因此默认 **direct**（忽略代理），需要时可在 config.json 里改成 system 或自定义代理。
"""

from __future__ import annotations

import os
import socket
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from .config import AppConfig
from .logging_setup import get_logger

log = get_logger("net")

# 代理软件常见的 fake-ip 网段（Clash / mihomo 默认 198.18.0.0/15）。
# 解析结果落在这里通常意味着"DNS 被代理软件接管了"，连不通时极易误判。
FAKE_IP_PREFIXES = ("198.18.", "198.19.")

PROXY_MODES = ("direct", "system")


def _system_proxy_map() -> dict[str, str]:
    """读取系统代理（Windows 注册表 / 环境变量），失败返回空。"""
    try:
        return urllib.request.getproxies() or {}
    except Exception:  # pragma: no cover - 极端环境
        return {}


def resolve_proxy(cfg: AppConfig | None) -> dict[str, str] | None:
    """按配置决定代理字典。

    None   = 使用 urllib 默认（等价于 system）
    {}     = 显式禁用所有代理（direct）
    {...}  = 指定代理
    """
    mode = "direct"
    if cfg is not None:
        mode = str(getattr(cfg.runtime, "proxy", "direct") or "direct").strip()
    if mode in ("", "direct", "none", "off"):
        return {}
    if mode in ("system", "auto"):
        return None
    if mode.startswith("http://") or mode.startswith("https://"):
        return {"http": mode, "https": mode}
    log.warning("无法识别的 runtime.proxy=%r，按 direct 处理。", mode)
    return {}


def build_opener(cfg: AppConfig | None, *, context: ssl.SSLContext | None = None):
    """构建绕开/使用代理的 opener（无状态，可复用）。

    注意 ProxyHandler 的语义：
    * 传 `{}` → 不注册任何代理方法 → 相当于「完全没有代理处理」，即直连；
    * **不传** → urllib 用默认 ProxyHandler（读系统代理 getproxies()）。
    所以 system 模式下不能传 `{}`，否则就成了直连（曾经写错过）。
    """
    handlers: list[Any] = []
    proxy_map = resolve_proxy(cfg)
    if proxy_map is not None:  # direct / 自定义代理
        handlers.append(urllib.request.ProxyHandler(proxy_map))
    if context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=context))
    return urllib.request.build_opener(*handlers)


def is_fake_ip(ip: str) -> bool:
    return any(str(ip).startswith(prefix) for prefix in FAKE_IP_PREFIXES)


def describe_error(exc: BaseException, url: str, cfg: AppConfig | None = None) -> str:
    """把连接层的异常翻译成能直接照做的中文建议。"""
    reason = exc
    if isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, BaseException):
        reason = exc.reason

    text = str(exc)
    lower = text.lower()
    host = urlparse(url).hostname or url

    refused = isinstance(reason, ConnectionRefusedError) or "10061" in text or "refused" in lower
    dns_fail = isinstance(reason, socket.gaierror) or "getaddrinfo" in lower or "11001" in text
    tls_fail = isinstance(reason, (ssl.SSLError,)) or "ssl" in lower or "eof" in lower
    timeout = isinstance(reason, TimeoutError) or "timed out" in lower

    system_proxy = _system_proxy_map()
    proxy_hint = ""
    if system_proxy:
        proxy_hint = f"（本机系统代理：{system_proxy.get('https') or system_proxy.get('http')}）"
    mode = getattr(cfg.runtime, "proxy", "direct") if cfg is not None else "direct"

    if refused:
        return (
            f"连接被拒绝：[WinError 10061] 无法连到 {host}。"
            f"CC98 是校内服务，必须直连；本项目默认已忽略系统代理（runtime.proxy={mode}）。"
            f"{proxy_hint} 若你手动把它改成了 system/自定义代理，请改回 direct；"
            f"另外确认该主机名解析到的是校内地址（10.10.98.98），而不是代理的 198.18.x.x。"
        )
    if dns_fail:
        return (
            f"域名解析失败：{host} 无法解析。"
            "请确认已连上校园网/RVPN；若开了代理软件的 fake-ip，先关闭它或把 *.cc98.org 设为直连。"
        )
    if tls_fail:
        return (
            f"TLS 握手被中断：{host}。常见原因是流量被代理软件接管后转发到了境外节点"
            f"（CC98 只能校内访问）{proxy_hint}。请把 *.cc98.org 设为 DIRECT 或关闭代理后重试。"
        )
    if timeout:
        return (
            f"连接超时：{host}。CC98 需要校园网/RVPN；校外直连会超时。"
            "手机上开热点或用非校网时也会这样。"
        )
    return f"网络错误：{host} -> {type(reason).__name__}: {reason}"


# ---------------------------------------------------------------------------
# 网络自检（python -m cc98digest netcheck）
# ---------------------------------------------------------------------------


@dataclass
class ProbeResult:
    label: str
    target: str
    ok: bool
    detail: str
    elapsed_ms: float


def _probe(label: str, url: str, cfg: AppConfig | None, timeout: float = 8.0) -> ProbeResult:
    """发一个真实请求，确认端到端可用（HTTPS 走完整 TLS 握手）。"""
    opener = build_opener(cfg)
    request = urllib.request.Request(url, headers={"User-Agent": "cc98-digest-netcheck/1.0"})
    started = time.monotonic()
    try:
        with opener.open(request, timeout=timeout) as resp:
            elapsed = (time.monotonic() - started) * 1000
            return ProbeResult(label, url, True, f"HTTP {resp.status}", elapsed)
    except urllib.error.HTTPError as exc:
        elapsed = (time.monotonic() - started) * 1000
        # 401/400 说明网络与 TLS 都通，只是没带凭据/参数
        ok = exc.code in (400, 401, 403, 404, 405)
        detail = f"HTTP {exc.code}" + ("（网络可达）" if ok else "")
        return ProbeResult(label, url, ok, detail, elapsed)
    except Exception as exc:
        elapsed = (time.monotonic() - started) * 1000
        return ProbeResult(label, url, False, describe_error(exc, url, cfg), elapsed)


def diagnose(cfg: AppConfig) -> dict[str, Any]:
    """收集诊断信息：代理、DNS、真实连通性。"""
    token_url = cfg.credentials.token_endpoint
    api_base = cfg.credentials.api_base.rstrip("/")
    hosts = {
        "令牌端点": urlparse(token_url).hostname,
        "API": urlparse(api_base).hostname,
    }

    dns: dict[str, Any] = {}
    for label, host in hosts.items():
        if not host:
            continue
        try:
            infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
            ips = sorted({info[4][0] for info in infos})
            dns[label] = {
                "host": host,
                "ips": ips,
                "fake_ip": any(is_fake_ip(ip) for ip in ips),
            }
        except Exception as exc:
            dns[label] = {"host": host, "ips": [], "error": str(exc)}

    probes = [
        _probe("令牌端点", token_url, cfg),
        _probe("API 公共接口", f"{api_base}/board/all", cfg),
    ]
    return {
        "proxy_mode": getattr(cfg.runtime, "proxy", "direct"),
        "system_proxy": _system_proxy_map(),
        "env_proxy": {
            key: value
            for key, value in os.environ.items()
            if key.lower() in ("http_proxy", "https_proxy", "all_proxy", "no_proxy")
        },
        "dns": dns,
        "probes": probes,
        "hosts_file_hint": "若解析结果不是 10.10.98.98，说明 DNS 被代理接管或不在校网。",
    }
