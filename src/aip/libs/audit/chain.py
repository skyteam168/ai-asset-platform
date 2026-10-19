"""审计哈希链（architecture.md 12.4）：每个 shard（实例 × 日期）一条独立的链，避免全局串行。

hash(n) = sha256(prev_hash(n) + "\\n" + canonical_json(记录正文))，seq 从 1 连续递增，首条 prev_hash 为 64 个 0。
"""

from __future__ import annotations

import hashlib
from typing import Any

from aip.libs.tokens.keys import canonical_json

GENESIS = "0" * 64


def body(
    shard_id: str, seq: int, occurred_at: str, company_id: str | None, kind: str, record: dict[str, Any]
) -> dict[str, Any]:
    return {
        "shard_id": shard_id,
        "seq": seq,
        "occurred_at": occurred_at,
        "company_id": company_id,
        "kind": kind,
        "record": record,
    }


def chain_hash(prev_hash: str, entry: dict[str, Any]) -> str:
    return hashlib.sha256(prev_hash.encode() + b"\n" + canonical_json(entry)).hexdigest()


def anchor_message(shard_id: str, up_to_seq: int, head_hash: str) -> bytes:
    return f"aip-audit-anchor|{shard_id}|{up_to_seq}|{head_hash}".encode()
