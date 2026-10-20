"""口令哈希（argon2id）、不透明令牌、TOTP 种子加密。"""

from __future__ import annotations

import hashlib
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet

from aip.libs.common.config import get_settings

_ph = PasswordHasher()  # argon2id 默认参数


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(hashed: str, password: str) -> bool:
    try:
        return _ph.verify(hashed, password)
    except (VerificationError, InvalidHashError):
        return False


def new_opaque_token(prefix: str) -> tuple[str, str]:
    """返回 (明文令牌, sha256 哈希)。高熵随机令牌用 sha256 存储即可，明文只返回一次。"""
    token = f"{prefix}_{secrets.token_urlsafe(32)}"
    return token, token_hash(token)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _box() -> Fernet:
    return Fernet(get_settings().secret_box_key.encode())


def seal(plain: str) -> str:
    """加密平台机密（TOTP 种子等）。transit 密文以 vault: 开头，可与旧的本地密文共存、逐步迁移。"""
    s = get_settings()
    if s.secret_backend == "transit":
        from aip.libs.tokens.keys import transit_client

        return transit_client().encrypt(s.transit_secret_key, plain.encode())
    return _box().encrypt(plain.encode()).decode()


def unseal(sealed: str) -> str:
    if sealed.startswith("vault:"):
        from aip.libs.tokens.keys import transit_client

        return transit_client().decrypt(get_settings().transit_secret_key, sealed).decode()
    return _box().decrypt(sealed.encode()).decode()
