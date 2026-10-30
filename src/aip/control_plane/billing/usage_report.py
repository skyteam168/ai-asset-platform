"""从 exec.usage_counter 聚合调用量（architecture.md 计量；G5.2）。"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from aip.libs.common.errors import ErrorCode, PlatformError

GroupBy = Literal["consumer", "company", "asset"]

_ASSET_KEY = re.compile(
    r"^asset:([0-9a-fA-F-]{36}):version:([0-9a-fA-F-]{36})$"
)
_CONSUMER_KEY = re.compile(r"^consumer:([0-9a-fA-F-]{36})$")
_CONSUMER_WRITE_KEY = re.compile(r"^consumer_write:([0-9a-fA-F-]{36})$")


def parse_day(raw: str, *, field: str) -> dt.date:
    try:
        return dt.date.fromisoformat(raw.strip())
    except ValueError as e:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, f"{field} 须为 YYYY-MM-DD") from e


async def aggregate_usage_counter(
    conn: AsyncConnection,
    *,
    day_from: dt.date,
    day_to: dt.date,
    group_by: GroupBy,
    company_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """按日窗聚合 usage_counter。

    - company：按 company_id 汇总全部键
    - consumer：仅 consumer:{uuid} 与 consumer_write:{uuid}，dim=消费方 id；写操作单独计 writes
    - asset：仅 asset:{id}:version:{vid}，dim=asset_id（跨版本求和）
    """
    if day_to < day_from:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "to 不得早于 from")
    if (day_to - day_from).days > 366:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "查询窗不得超过 366 天")

    params: dict[str, Any] = {"from": day_from, "to": day_to}
    scope = ""
    if company_ids is not None:
        scope = " AND company_id = ANY(CAST(:companies AS uuid[]))"
        params["companies"] = company_ids

    rows = (
        await conn.execute(
            text(
                "SELECT day, key, company_id::text AS company_id, count "
                "FROM exec.usage_counter "
                "WHERE day >= :from AND day <= :to" + scope
            ),
            params,
        )
    ).mappings().all()

    if group_by == "company":
        totals: dict[str, int] = {}
        for r in rows:
            cid = str(r["company_id"] or "")
            totals[cid] = totals.get(cid, 0) + int(r["count"])
        return [
            {"group_by": "company", "dim": k, "company_id": k, "calls": v, "writes": 0}
            for k, v in sorted(totals.items(), key=lambda x: (-x[1], x[0]))
        ]

    if group_by == "consumer":
        calls: dict[tuple[str, str], int] = {}
        writes: dict[tuple[str, str], int] = {}
        for r in rows:
            key = str(r["key"] or "")
            cid = str(r["company_id"] or "")
            m = _CONSUMER_KEY.match(key)
            if m:
                dim = m.group(1)
                calls[(cid, dim)] = calls.get((cid, dim), 0) + int(r["count"])
                continue
            mw = _CONSUMER_WRITE_KEY.match(key)
            if mw:
                dim = mw.group(1)
                writes[(cid, dim)] = writes.get((cid, dim), 0) + int(r["count"])
        dims = set(calls) | set(writes)
        out: list[dict[str, Any]] = []
        for pair in sorted(dims, key=lambda x: (-(calls.get(x, 0) + writes.get(x, 0)), x[0], x[1])):
            cid, dim = pair
            out.append(
                {
                    "group_by": "consumer",
                    "dim": dim,
                    "company_id": cid,
                    "calls": calls.get(pair, 0),
                    "writes": writes.get(pair, 0),
                }
            )
        return out

    if group_by == "asset":
        totals_a: dict[tuple[str, str], int] = {}
        for r in rows:
            key = str(r["key"] or "")
            m = _ASSET_KEY.match(key)
            if not m:
                continue
            cid = str(r["company_id"] or "")
            aid = m.group(1)
            totals_a[(cid, aid)] = totals_a.get((cid, aid), 0) + int(r["count"])
        return [
            {
                "group_by": "asset",
                "dim": aid,
                "company_id": cid,
                "calls": n,
                "writes": 0,
            }
            for (cid, aid), n in sorted(totals_a.items(), key=lambda x: (-x[1], x[0][0], x[0][1]))
        ]

    raise PlatformError(ErrorCode.INVALID_ARGUMENT, "group_by 须为 consumer|company|asset")
