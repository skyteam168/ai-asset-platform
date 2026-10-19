"""平台签名密钥（ES256）。生产由 OpenBao Transit 托管（M2）；这里提供本地实现与 JWKS 导出。"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from aip.libs.common.config import get_settings


def _b64(n: int, length: int) -> str:
    return base64.urlsafe_b64encode(n.to_bytes(length, "big")).rstrip(b"=").decode()


@dataclass(frozen=True)
class SigningKey:
    kid: str
    private: ec.EllipticCurvePrivateKey

    @staticmethod
    def from_pem(pem: str) -> SigningKey:
        key = serialization.load_pem_private_key(pem.encode(), password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey):
            raise ValueError("signing key must be EC P-256")
        return SigningKey(_kid(key.public_key()), key)

    @staticmethod
    def generate() -> SigningKey:
        key = ec.generate_private_key(ec.SECP256R1())
        return SigningKey(_kid(key.public_key()), key)

    def public_jwk(self) -> dict[str, str]:
        nums = self.private.public_key().public_numbers()
        return {
            "kty": "EC",
            "crv": "P-256",
            "x": _b64(nums.x, 32),
            "y": _b64(nums.y, 32),
            "kid": self.kid,
            "alg": "ES256",
            "use": "sig",
        }


def _kid(pub: ec.EllipticCurvePublicKey) -> str:
    der = pub.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(der).hexdigest()[:16]


@lru_cache
def platform_key() -> SigningKey:
    pem = get_settings().signing_key_pem
    return SigningKey.from_pem(pem) if pem else SigningKey.generate()


def jwks(keys: list[SigningKey]) -> dict[str, list[dict[str, str]]]:
    return {"keys": [k.public_jwk() for k in keys]}


def canonical_json(obj: object) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode()
