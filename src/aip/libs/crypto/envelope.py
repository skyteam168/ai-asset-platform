"""按公司 DEK 信封加密：AES-GCM 负载 + Transit/local 包装 DEK。

ref 形态（jsonb）：
  {v:1, company_id, alg, wrapped_dek, nonce, ciphertext, sha256}
审计与日志只应保留 sha256，禁止写入 ciphertext 明文解码结果。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import uuid
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from aip.libs.common.config import get_settings

_ALG = "aes-256-gcm"
_LOCAL_KEK_CACHE: dict[str, bytes] = {}


def is_envelope(obj: Any) -> bool:
    return isinstance(obj, dict) and obj.get("v") == 1 and "wrapped_dek" in obj and "ciphertext" in obj


def _company_key_name(company_id: str) -> str:
    s = get_settings()
    return f"{s.transit_company_key_prefix}{company_id}"


def _local_kek(company_id: str) -> bytes:
    if company_id not in _LOCAL_KEK_CACHE:
        # 派生自 secret_box_key + company_id，进程内稳定、跨进程在同一 secret_box_key 下一致
        material = f"{get_settings().secret_box_key}:{company_id}".encode()
        _LOCAL_KEK_CACHE[company_id] = hashlib.sha256(material).digest()
    return _LOCAL_KEK_CACHE[company_id]


def _wrap_dek(company_id: str, dek: bytes) -> str:
    s = get_settings()
    if s.envelope_backend == "transit":
        from aip.libs.tokens.keys import transit_client

        name = _company_key_name(company_id)
        client = transit_client()
        client.ensure_key(name, "aes256-gcm96")
        return client.encrypt(name, dek)
    # local：KEK 加密 DEK（AES-GCM），前缀 local:
    kek = _local_kek(company_id)
    nonce = os.urandom(12)
    ct = AESGCM(kek).encrypt(nonce, dek, company_id.encode())
    return "local:" + base64.urlsafe_b64encode(nonce + ct).decode()


def _unwrap_dek(company_id: str, wrapped: str) -> bytes:
    if wrapped.startswith("local:"):
        raw = base64.urlsafe_b64decode(wrapped[6:].encode())
        nonce, ct = raw[:12], raw[12:]
        return AESGCM(_local_kek(company_id)).decrypt(nonce, ct, company_id.encode())
    from aip.libs.tokens.keys import transit_client

    return transit_client().decrypt(_company_key_name(company_id), wrapped)


def encrypt_json(company_id: str | uuid.UUID, payload: Any) -> dict[str, Any]:
    cid = str(company_id)
    plain = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).encode()
    dek = AESGCM.generate_key(bit_length=256)
    nonce = os.urandom(12)
    ct = AESGCM(dek).encrypt(nonce, plain, cid.encode())
    return {
        "v": 1,
        "company_id": cid,
        "alg": _ALG,
        "wrapped_dek": _wrap_dek(cid, dek),
        "nonce": base64.urlsafe_b64encode(nonce).decode(),
        "ciphertext": base64.urlsafe_b64encode(ct).decode(),
        "sha256": hashlib.sha256(plain).hexdigest(),
    }


def decrypt_json(company_id: str | uuid.UUID, ref: dict[str, Any]) -> Any:
    if not is_envelope(ref):
        raise ValueError("不是信封引用")
    cid = str(company_id)
    if str(ref.get("company_id")) != cid:
        raise ValueError("公司密钥不匹配")
    dek = _unwrap_dek(cid, str(ref["wrapped_dek"]))
    nonce = base64.urlsafe_b64decode(str(ref["nonce"]).encode())
    ct = base64.urlsafe_b64decode(str(ref["ciphertext"]).encode())
    plain = AESGCM(dek).decrypt(nonce, ct, cid.encode())
    expect = ref.get("sha256")
    if expect and hashlib.sha256(plain).hexdigest() != expect:
        raise ValueError("信封完整性校验失败")
    return json.loads(plain.decode())
