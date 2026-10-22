"""控制面目录中文 FTS（H41；依赖 zhparser，缺扩展时全部降级）。"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def zhparser_ready(conn: AsyncConnection) -> bool:
    """库内是否已启用 chinese 配置与 catalog_fts 表。"""
    row = (
        await conn.execute(
            text(
                """
                SELECT EXISTS (
                  SELECT 1 FROM pg_extension WHERE extname = 'zhparser'
                ) AND EXISTS (
                  SELECT 1 FROM pg_ts_config WHERE cfgname = 'chinese'
                ) AND EXISTS (
                  SELECT 1 FROM information_schema.tables
                  WHERE table_schema = 'registry' AND table_name = 'catalog_fts'
                ) AS ok
                """
            )
        )
    ).mappings().first()
    return bool(row and row["ok"])


async def upsert_catalog_fts(
    conn: AsyncConnection,
    *,
    asset_id: uuid.UUID,
    company_id: uuid.UUID,
    search_text: str,
) -> None:
    if not await zhparser_ready(conn):
        return
    await conn.execute(
        text(
            """
            INSERT INTO registry.catalog_fts (asset_id, company_id, search_text, updated_at)
            VALUES (:aid, :cid, :st, now())
            ON CONFLICT (asset_id) DO UPDATE SET
              company_id = EXCLUDED.company_id,
              search_text = EXCLUDED.search_text,
              updated_at = now()
            """
        ),
        {"aid": asset_id, "cid": company_id, "st": search_text},
    )


async def search_asset_ids(conn: AsyncConnection, query: str, *, limit: int = 200) -> list[uuid.UUID] | None:
    """有 zhparser 时返回命中 asset_id；无扩展返回 None（调用方自行降级）。"""
    q = (query or "").strip()
    if not q:
        return []
    if not await zhparser_ready(conn):
        return None
    rows = (
        await conn.execute(
            text(
                """
                SELECT asset_id FROM registry.catalog_fts
                WHERE tsv @@ plainto_tsquery('chinese', :q)
                ORDER BY ts_rank(tsv, plainto_tsquery('chinese', :q)) DESC
                LIMIT :lim
                """
            ),
            {"q": q, "lim": limit},
        )
    ).mappings().all()
    return [uuid.UUID(str(r["asset_id"])) for r in rows]


def match_in_memory(items: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """无 FTS 时的子串降级（运维目录用）。"""
    q = (query or "").strip().lower()
    if not q:
        return items
    out: list[dict[str, Any]] = []
    for it in items:
        blob = " ".join(
            str(it.get(k) or "")
            for k in ("name", "capability_id", "description", "title", "search_text")
        ).lower()
        if q in blob:
            out.append(it)
    return out
