"""数据面本地 IAM 快照缓存（architecture.md 4.4）：校验签名与摘要，seq 只增不减，缺失即拒绝。"""

from __future__ import annotations

import base64
import hashlib
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.tokens.keys import SigningKey, canonical_json


class SnapshotRejected(Exception):
    pass


class IamCache:
    def __init__(self, trusted_keys: list[SigningKey]) -> None:
        self._keys = trusted_keys
        self._data: dict[str, Any] | None = None
        self.seq: int | None = None

    def load(self, snapshot: dict[str, Any]) -> None:
        payload = snapshot["payload"]
        body = canonical_json(payload)
        if hashlib.sha256(body).hexdigest() != snapshot["sha256"]:
            raise SnapshotRejected("sha256 mismatch")
        sig = base64.b64decode(snapshot["signature"])
        if not any(_verifies(k, sig, body) for k in self._keys):
            raise SnapshotRejected("bad signature")
        if payload.get("seq") != snapshot["seq"]:
            raise SnapshotRejected("seq mismatch")
        if self.seq is not None and snapshot["seq"] <= self.seq:
            return  # 旧快照，忽略
        self._data = payload
        self.seq = int(snapshot["seq"])

    @property
    def data(self) -> dict[str, Any]:
        if self._data is None:
            raise PlatformError(ErrorCode.POLICY_UNAVAILABLE, "IAM 快照未加载，默认拒绝")
        return self._data

    def consumer_by_client_id(self, client_id: str) -> tuple[str, dict[str, Any]] | None:
        for cid, c in self.data["consumers"].items():
            if c["client_id"] == client_id:
                return cid, c
        return None

    def consumer_by_token_hash(self, digest: str) -> tuple[str, dict[str, Any], dict[str, Any]] | None:
        for cid, c in self.data["consumers"].items():
            tok = c["static_tokens"].get(digest)
            if tok is not None:
                return cid, c, tok
        return None


def _verifies(key: SigningKey, sig: bytes, body: bytes) -> bool:
    try:
        key.private.public_key().verify(sig, body, ec.ECDSA(hashes.SHA256()))
        return True
    except InvalidSignature:
        return False
