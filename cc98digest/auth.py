"""令牌获取 / 刷新 / 本地缓存。

CC98 使用 IdentityServer(OpenID Connect) 的 password grant。这里只用官方端点，
不模拟网页登录，也不做任何写操作。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .config import AppConfig, Credentials
from .logging_setup import get_logger

log = get_logger("auth")

# 提前 120 秒判定过期，避免边界上被 401
EXPIRY_SKEW_SECONDS = 120


class AuthError(Exception):
    """登录/刷新失败（凭据错误、端点不可达等）。"""


@dataclass
class Token:
    access_token: str
    refresh_token: str | None
    expires_at: float  # epoch 秒

    @property
    def is_valid(self) -> bool:
        return bool(self.access_token) and time.time() < self.expires_at - EXPIRY_SKEW_SECONDS

    @property
    def seconds_left(self) -> int:
        return max(0, int(self.expires_at - time.time()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
        }


def _post_form(
    url: str, data: dict[str, str], timeout: float, user_agent: str
) -> tuple[int, dict[str, Any]]:
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": user_agent,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read().decode("utf-8", "replace")
            return resp.status, _maybe_json(payload)
    except urllib.error.HTTPError as exc:
        payload = ""
        try:
            payload = exc.read().decode("utf-8", "replace")
        except Exception:  # pragma: no cover
            pass
        return exc.code, _maybe_json(payload)
    except urllib.error.URLError as exc:
        raise AuthError(f"无法连接令牌端点 {url}：{exc.reason}") from exc
    except TimeoutError as exc:
        raise AuthError(f"连接令牌端点 {url} 超时。") from exc


def _maybe_json(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {"data": parsed}
    except json.JSONDecodeError:
        return {"_raw": text[:500]}


def _token_from_payload(payload: dict[str, Any], previous: Token | None = None) -> Token:
    access = payload.get("access_token")
    if not isinstance(access, str) or not access:
        raise AuthError(_describe_error(payload))
    expires_in = payload.get("expires_in")
    try:
        ttl = float(expires_in) if expires_in is not None else 3600.0
    except (TypeError, ValueError):
        ttl = 3600.0
    refresh = payload.get("refresh_token")
    if not isinstance(refresh, str) or not refresh:
        refresh = previous.refresh_token if previous else None
    return Token(access_token=access, refresh_token=refresh, expires_at=time.time() + ttl)


def _describe_error(payload: dict[str, Any]) -> str:
    code = payload.get("error")
    desc = payload.get("error_description")
    if code == "invalid_grant":
        return "CC98 登录被拒绝（invalid_grant）：账号或密码错误，或该账号需要先在浏览器完成一次验证。"
    if code:
        return f"CC98 登录失败：{code} - {desc or '无描述'}"
    raw = payload.get("_raw")
    if raw:
        return f"CC98 登录失败，返回内容无法解析：{raw}"
    return "CC98 登录失败：令牌端点在响应中没有 access_token。"


def load_cached_token(path: Path) -> Token | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        token = Token(
            access_token=str(data["access_token"]),
            refresh_token=data.get("refresh_token"),
            expires_at=float(data["expires_at"]),
        )
    except Exception as exc:  # 缓存损坏就当作没有
        log.warning("令牌缓存 %s 无法读取（%s），将重新登录。", path, exc)
        return None
    return token


def save_cached_token(path: Path, token: Token) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(token.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:  # 缓存写不了不影响本次运行
        log.warning("写入令牌缓存失败：%s", exc)


def request_token(
    creds: Credentials, timeout: float, user_agent: str, use_refresh: str | None
) -> Token:
    common = {
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scope": creds.scope,
    }
    # 空 scope 会让 IdentityServer 返回 400 invalid_scope，做一次兜底
    if not str(common["scope"]).strip():
        common["scope"] = "cc98-api openid offline_access"
    if use_refresh:
        data = {**common, "grant_type": "refresh_token", "refresh_token": use_refresh}
    else:
        data = {
            **common,
            "grant_type": "password",
            "username": creds.username,
            "password": creds.password,
        }
    status, payload = _post_form(creds.token_endpoint, data, timeout, user_agent)
    log.debug("令牌端点 %s 返回 %s", creds.token_endpoint, status)
    return _token_from_payload(payload)


def get_token(
    cfg: AppConfig, force_login: bool = False, refresh_token: str | None = None
) -> Token:
    """按 内存/磁盘缓存 -> refresh_token -> password 的顺序获取可用令牌。"""
    creds = cfg.credentials
    timeout = float(cfg.runtime.timeout_seconds)
    user_agent = cfg.runtime.user_agent

    if not refresh_token:
        cached = None if force_login else load_cached_token(cfg.token_cache_path)
        if cached and cached.is_valid:
            log.debug("复用本地令牌，剩余 %s 秒。", cached.seconds_left)
            return cached
        if cached and cached.refresh_token:
            try:
                token = request_token(creds, timeout, user_agent, use_refresh=cached.refresh_token)
                save_cached_token(cfg.token_cache_path, token)
                log.info("已通过 refresh_token 刷新登录。")
                return token
            except AuthError as exc:
                log.warning("refresh_token 刷新失败，回退到账号密码登录：%s", exc)

    if refresh_token:
        try:
            token = request_token(creds, timeout, user_agent, use_refresh=refresh_token)
            save_cached_token(cfg.token_cache_path, token)
            log.info("已通过 refresh_token 刷新登录。")
            return token
        except AuthError as exc:
            log.warning("refresh_token 刷新失败，回退到账号密码登录：%s", exc)

    if not creds.has_user_credentials():
        raise AuthError(
            "未配置 CC98 账号密码，且没有可用的 refresh_token。"
            "请编辑 config.json，或设置环境变量 CC98_USERNAME / CC98_PASSWORD。"
        )

    token = request_token(creds, timeout, user_agent, use_refresh=None)
    save_cached_token(cfg.token_cache_path, token)
    log.info("账号密码登录成功，令牌有效期约 %s 秒。", token.seconds_left)
    return token


def login_with_password(cfg: AppConfig, username: str, password: str) -> Token:
    """用前端传入的账号密码直接走 password grant 登录，成功后写入令牌缓存。

    与 get_token 不同：这里不读缓存、不尝试 refresh_token，就是「用这组账号密码
    重新登录一次」。成功返回 Token；失败抛 AuthError（由调用方把错误回给前端）。
    """
    creds = replace(cfg.credentials, username=username, password=password)
    token = request_token(
        creds, float(cfg.runtime.timeout_seconds), cfg.runtime.user_agent, use_refresh=None
    )
    save_cached_token(cfg.token_cache_path, token)
    log.info("前端登录成功，账号：%s，令牌有效期约 %s 秒。", username, token.seconds_left)
    return token
