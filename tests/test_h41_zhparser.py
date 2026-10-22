"""H41：zhparser opt-in FTS；默认 alpine 无扩展时 @pytest.mark.zhparser skip。"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from aip.control_plane.db import Database, DbScope
from aip.control_plane.registry.fts import match_in_memory, search_asset_ids, upsert_catalog_fts, zhparser_ready


def test_match_in_memory_fallback() -> None:
    items = [
        {"capability_id": "tool.a", "title": "查询工单", "description": ""},
        {"capability_id": "tool.b", "title": "请假", "description": ""},
    ]
    assert [i["capability_id"] for i in match_in_memory(items, "工单")] == ["tool.a"]


@pytest.mark.infra
@pytest.mark.zhparser
async def test_zhparser_chinese_fts_hits_work_order(db: Database) -> None:
    async with db.tx(DbScope.system()) as conn:
        if not await zhparser_ready(conn):
            pytest.skip("zhparser not installed; run tools/dev.py zhparser-up")
        company_id = uuid.uuid4()
        asset_id = uuid.uuid4()
        await upsert_catalog_fts(
            conn,
            asset_id=asset_id,
            company_id=company_id,
            search_text="查询工单状态 tool.ticket.query",
        )
        tok = (
            await conn.execute(text("SELECT to_tsvector('chinese', '查询工单状态')::text AS v"))
        ).mappings().first()
        assert tok and "工单" in (tok["v"] or "")
        hits = await search_asset_ids(conn, "工单")
        assert hits is not None
        assert asset_id in hits
