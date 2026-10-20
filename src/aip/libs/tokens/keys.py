"""平台签名密钥（ES256）：本地密钥（开发 / 测试）或 OpenBao Transit（生产，私钥不出 OpenBao）。

所有签名都经 SigningKey.sign()（返回 DER 编码的 ECDSA 签名）；验签只用公钥。
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from aip.libs.common.config import get_settings


def _b64(n: int, length: int) -> str:
    return base64.urlsafe_b64encode(n.to_bytes(length, "big")).rstrip(b"=").decode()


@dataclass(frozen=True)
class SigningKey:
    kid: str
    public_key: ec.EllipticCurvePublicKey
    signer: Callable[[bytes], bytes] = field(repr=False, compare=False)
    private: ec.EllipticCurvePrivateKey | None = field(default=None, repr=False, compare=False)

    @staticmethod
    def from_private(key: ec.EllipticCurvePrivateKey) -> SigningKey:
        return SigningKey(
            _kid(key.public_key()), key.public_key(), lambda body: key.sign(body, ec.ECDSA(hashes.SHA256())), key
        )

    @staticmethod
    def from_pem(pem: str) -> SigningKey:
        key = serialization.load_pem_private_key(pem.encode(), password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey):
            raise ValueError("signing key must be EC P-256")
        return SigningKey.from_private(key)

    @staticmethod
    def generate() -> SigningKey:
        return SigningKey.from_private(ec.generate_private_key(ec.SECP256R1()))

    def sign(self, body: bytes) -> bytes:
        """返回 DER 编码的 ECDSA-SHA256 签名。"""
        return self.signer(body)

    def public_jwk(self) -> dict[str, str]:
        nums = self.public_key.public_numbers()
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


class TransitClient:
    """OpenBao Transit 的最小同步客户端（签名与加解密都是本机 / 内网短调用；令牌签发频率很低）。"""

    def __init__(self, addr: str, token: str, mount: str = "transit") -> None:
        self._http = httpx.Client(base_url=addr, headers={"X-Vault-Token": token}, timeout=3)
        self.mount = mount

    def ensure_key(self, name: str, key_type: str) -> None:
        mounts = self._http.get("/v1/sys/mounts").json()
        if f"{self.mount}/" not in mounts.get("data", mounts):
            self._http.post(f"/v1/sys/mounts/{self.mount}", json={"type": "transit"}).raise_for_status()
        if self._http.get(f"/v1/{self.mount}/keys/{name}").status_code == 404:
            self._http.post(f"/v1/{self.mount}/keys/{name}", json={"type": key_type}).raise_for_status()

    def public_keys(self, name: str) -> dict[int, ec.EllipticCurvePublicKey]:
        r = self._http.get(f"/v1/{self.mount}/keys/{name}")
        r.raise_for_status()
        out = {}
        for version, info in r.json()["data"]["keys"].items():
            pub = serialization.load_pem_public_key(info["public_key"].encode())
            if not isinstance(pub, ec.EllipticCurvePublicKey):
                raise ValueError("transit key must be ecdsa-p256")
            out[int(version)] = pub
        return out

    def sign(self, name: str, body: bytes) -> bytes:
        r = self._http.post(
            f"/v1/{self.mount}/sign/{name}",
            json={
                "input": base64.b64encode(body).decode(),
                "hash_algorithm": "sha2-256",
                "marshaling_algorithm": "asn1",
            },
        )
        r.raise_for_status()
        return base64.b64decode(r.json()["data"]["signature"].split(":")[-1])

    def encrypt(self, name: str, plain: bytes) -> str:
        r = self._http.post(f"/v1/{self.mount}/encrypt/{name}", json={"plaintext": base64.b64encode(plain).decode()})
        r.raise_for_status()
        return str(r.json()["data"]["ciphertext"])

    def decrypt(self, name: str, ciphertext: str) -> bytes:
        r = self._http.post(f"/v1/{self.mount}/decrypt/{name}", json={"ciphertext": ciphertext})
        r.raise_for_status()
        return base64.b64decode(r.json()["data"]["plaintext"])

    def rotate(self, name: str) -> None:
        self._http.post(f"/v1/{self.mount}/keys/{name}/rotate").raise_for_status()


def transit_keys(client: TransitClient, name: str) -> list[SigningKey]:
    """当前版本用于签名；全部版本的公钥都发布到 JWKS，轮换期间旧令牌与旧快照仍可验签。"""
    pubs = client.public_keys(name)
    latest = max(pubs)

    def signer_for(version: int) -> Callable[[bytes], bytes]:
        def sign(body: bytes) -> bytes:
            if version != latest:
                raise RuntimeError("只有最新版本的 Transit 密钥用于签名")
            return client.sign(name, body)

        return sign

    keys = [SigningKey(_kid(p), p, signer_for(v)) for v, p in sorted(pubs.items(), reverse=True)]
    return keys


@lru_cache
def transit_client() -> TransitClient:
    s = get_settings()
    return TransitClient(s.openbao_addr, s.openbao_token)


@lru_cache
def platform_keys() -> tuple[SigningKey, ...]:
    """第一把用于签名，其余只用于验签（轮换）。"""
    s = get_settings()
    if s.signing_backend == "transit":
        return tuple(transit_keys(transit_client(), s.transit_signing_key))
    return (SigningKey.from_pem(s.signing_key_pem) if s.signing_key_pem else SigningKey.generate(),)


def platform_key() -> SigningKey:
    return platform_keys()[0]


def jwks(keys: list[SigningKey]) -> dict[str, list[dict[str, str]]]:
    return {"keys": [k.public_jwk() for k in keys]}


def canonical_json(obj: object) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode()
