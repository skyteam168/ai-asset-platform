"""OAuth 2.1 authorization_code + PKCE（RFC 7636）骨架。

真浏览器同意页与 redirect 联调属 Wave B 外部验收；本模块提供 S256 校验、
一次性授权码与 stub 签发，供本地测例与后续接线。
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass
from typing import Protocol

from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.common.ids import utcnow


def new_code_verifier() -> str:
    """43–128 字符 unreserved（RFC 7636）。"""
    return secrets.token_urlsafe(32)


def s256_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def verify_pkce(verifier: str, challenge: str, method: str = "S256") -> None:
    if method != "S256":
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "仅支持 code_challenge_method=S256")
    if len(verifier) < 43 or len(verifier) > 128:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "code_verifier 长度非法")
    if s256_challenge(verifier) != challenge:
        raise PlatformError(ErrorCode.UNAUTHENTICATED, "PKCE 校验失败")


@dataclass
class AuthCode:
    code: str
    client_id: str
    user_id: str
    redirect_uri: str
    code_challenge: str
    code_challenge_method: str
    resource: str
    expires_at: float  # monotonic / epoch seconds


class AuthCodeStore(Protocol):
    def put(self, record: AuthCode) -> None: ...

    def take(self, code: str) -> AuthCode | None: ...


class MemoryAuthCodeStore:
    """进程内一次性授权码（多副本需换 Valkey；骨架够用）。"""

    def __init__(self) -> None:
        self._items: dict[str, AuthCode] = {}

    def put(self, record: AuthCode) -> None:
        self._items[record.code] = record

    def take(self, code: str) -> AuthCode | None:
        rec = self._items.pop(code, None)
        if rec is None:
            return None
        if rec.expires_at <= time.time():
            return None
        return rec


def issue_auth_code(
    store: AuthCodeStore,
    *,
    client_id: str,
    user_id: str,
    redirect_uri: str,
    code_challenge: str,
    code_challenge_method: str = "S256",
    resource: str,
    ttl_s: int = 120,
) -> str:
    if code_challenge_method != "S256":
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "仅支持 code_challenge_method=S256")
    if not code_challenge or len(code_challenge) < 43:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "缺少有效 code_challenge")
    code = secrets.token_urlsafe(24)
    store.put(
        AuthCode(
            code=code,
            client_id=client_id,
            user_id=user_id,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            resource=resource,
            expires_at=time.time() + ttl_s,
        )
    )
    _ = utcnow()  # 保持与平台时间工具一致的依赖面
    return code
