"""平台签名的数据（快照、审计锚点）：canonical JSON → sha256 → ECDSA P-256 签名。"""

from __future__ import annotations

import base64
import hashlib
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from aip.libs.tokens.keys import SigningKey, canonical_json


def sign_bytes(body: bytes, key: SigningKey) -> str:
    return base64.b64encode(key.private.sign(body, ec.ECDSA(hashes.SHA256()))).decode()


def verify_bytes(body: bytes, signature: str, keys: list[SigningKey]) -> bool:
    try:
        sig = base64.b64decode(signature)
    except ValueError:
        return False
    for k in keys:
        try:
            k.private.public_key().verify(sig, body, ec.ECDSA(hashes.SHA256()))
            return True
        except InvalidSignature:
            continue
    return False


def sign_payload(payload: dict[str, Any], key: SigningKey) -> tuple[str, str]:
    """返回 (sha256 摘要, 签名)。"""
    body = canonical_json(payload)
    return hashlib.sha256(body).hexdigest(), sign_bytes(body, key)


def verify_payload(payload: dict[str, Any], digest: str, signature: str, keys: list[SigningKey]) -> bool:
    body = canonical_json(payload)
    return hashlib.sha256(body).hexdigest() == digest and verify_bytes(body, signature, keys)
