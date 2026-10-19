"""审计入库、锚定与校验（architecture.md 12.4）。"""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from aip.libs.audit.chain import GENESIS, anchor_message, body, chain_hash
from aip.libs.audit.spool import AuditSpool
from aip.libs.tokens.keys import SigningKey
from aip.libs.tokens.signed import sign_bytes, verify_bytes

INSERT = text(
    "INSERT INTO audit.audit_log (shard_id, seq, occurred_at, company_id, kind, record, prev_hash, hash) "
    "VALUES (:shard_id, :seq, :occurred_at, CAST(:company_id AS uuid), :kind, CAST(:record AS jsonb), "
    ":prev_hash, :hash) "
    "ON CONFLICT DO NOTHING"
)


async def ship(spool: AuditSpool, engine: AsyncEngine, batch: int = 500) -> int:
    """把 spool 中未入库的记录写入审计库；可重复执行（主键冲突忽略），入库成功后才推进 offset。"""
    shipped = 0
    for path in sorted(spool.dir.glob("*.jsonl")):
        off_path = spool.dir / f"{path.stem}.offset"
        offset = int(off_path.read_text()) if off_path.exists() else 0
        with path.open("rb") as f:
            f.seek(offset)
            data = f.read()
        lines = [ln for ln in data.split(b"\n") if ln.strip()]
        complete = data[: data.rfind(b"\n") + 1] if b"\n" in data else b""
        if not lines or not complete:
            continue
        rows = [json.loads(ln) for ln in complete.split(b"\n") if ln.strip()]
        months = {r["occurred_at"][:7] for r in rows}
        async with engine.begin() as conn:
            for m in months:
                await conn.execute(text("SELECT audit.ensure_partition(:d)"), {"d": dt.date.fromisoformat(f"{m}-01")})
            for i in range(0, len(rows), batch):
                await conn.execute(
                    INSERT,
                    [
                        {
                            **r,
                            "occurred_at": dt.datetime.fromisoformat(r["occurred_at"]),
                            "record": json.dumps(r["record"], ensure_ascii=False),
                        }
                        for r in rows[i : i + batch]
                    ],
                )
        tmp = off_path.with_suffix(".tmp")
        tmp.write_text(str(offset + len(complete)))
        os.replace(tmp, off_path)
        shipped += len(rows)
    return shipped


async def anchor(conn: AsyncConnection, key: SigningKey) -> int:
    """为每个 shard 的当前链头签名并写入 audit_anchor（每小时运行；已锚定的跳过）。"""
    heads = (
        await conn.execute(
            text("SELECT DISTINCT ON (shard_id) shard_id, seq, hash FROM audit.audit_log ORDER BY shard_id, seq DESC")
        )
    ).all()
    n = 0
    for shard_id, seq, h in heads:
        res = await conn.execute(
            text(
                "INSERT INTO audit.audit_anchor (shard_id, up_to_seq, head_hash, signature, key_id) "
                "VALUES (:s, :q, :h, :sig, :kid) ON CONFLICT DO NOTHING"
            ),
            {"s": shard_id, "q": seq, "h": h, "sig": sign_bytes(anchor_message(shard_id, seq, h), key), "kid": key.kid},
        )
        n += res.rowcount or 0
    return n


@dataclass(frozen=True)
class Problem:
    shard_id: str
    seq: int
    reason: str

    def __str__(self) -> str:
        return f"shard={self.shard_id} seq={self.seq}: {self.reason}"


async def verify(conn: AsyncConnection, keys: list[SigningKey]) -> list[Problem]:
    """逐 shard 重算哈希链并校验锚点签名；返回发现的问题（空列表表示完整）。"""
    problems: list[Problem] = []
    hashes: dict[tuple[str, int], str] = {}
    shard, expected_seq, prev = None, 1, GENESIS
    rows = await conn.execute(
        text(
            "SELECT shard_id, seq, occurred_at, company_id, kind, record, prev_hash, hash FROM audit.audit_log "
            "ORDER BY shard_id, seq"
        )
    )
    for r in rows.mappings():
        if r["shard_id"] != shard:
            shard, expected_seq, prev = r["shard_id"], 1, GENESIS
        if r["seq"] != expected_seq:
            problems.append(Problem(r["shard_id"], expected_seq, f"缺失记录（下一条是 seq={r['seq']}）"))
        entry = body(
            r["shard_id"],
            r["seq"],
            r["occurred_at"].isoformat(),
            str(r["company_id"]) if r["company_id"] else None,
            r["kind"],
            _json(r["record"]),
        )
        if r["prev_hash"] != prev:
            problems.append(Problem(r["shard_id"], r["seq"], "prev_hash 与上一条不连续"))
        if chain_hash(r["prev_hash"], entry) != r["hash"]:
            problems.append(Problem(r["shard_id"], r["seq"], "内容哈希不匹配（记录被篡改）"))
        hashes[(r["shard_id"], r["seq"])] = r["hash"]
        prev, expected_seq = r["hash"], r["seq"] + 1
    for a in (await conn.execute(text("SELECT * FROM audit.audit_anchor ORDER BY shard_id, up_to_seq"))).mappings():
        if not verify_bytes(anchor_message(a["shard_id"], a["up_to_seq"], a["head_hash"]), a["signature"], keys):
            problems.append(Problem(a["shard_id"], a["up_to_seq"], "锚点签名无效"))
        actual = hashes.get((a["shard_id"], a["up_to_seq"]))
        if actual is None:
            problems.append(Problem(a["shard_id"], a["up_to_seq"], "锚定的记录已不存在（被删除）"))
        elif actual != a["head_hash"]:
            problems.append(Problem(a["shard_id"], a["up_to_seq"], "链头与锚点不一致（锚定后被改写）"))
    return problems


def _json(v: Any) -> dict[str, Any]:
    return json.loads(v) if isinstance(v, str) else dict(v)


def verify_spool_file(path: pathlib.Path) -> list[Problem]:
    """不依赖数据库，直接校验本地 spool 文件。"""
    problems: list[Problem] = []
    prev, expected = GENESIS, 1
    for line in path.read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        entry = body(r["shard_id"], r["seq"], r["occurred_at"], r["company_id"], r["kind"], r["record"])
        if r["seq"] != expected or r["prev_hash"] != prev or chain_hash(prev, entry) != r["hash"]:
            problems.append(Problem(r["shard_id"], r["seq"], "spool 链不完整"))
        prev, expected = r["hash"], r["seq"] + 1
    return problems
