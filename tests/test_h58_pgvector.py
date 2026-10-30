"""G5.3 / H58：pgvector opt-in；默认 alpine 无扩展时 @pytest.mark.pgvector skip。"""

from __future__ import annotations

import uuid

import pytest

from aip.control_plane.db import Database, DbScope
from aip.control_plane.registry.vector import (
    EMBED_DIM,
    merge_search_ids,
    pgvector_ready,
    search_asset_ids_by_vector,
    text_to_pseudo_embedding,
    upsert_catalog_embedding,
)


def test_pseudo_embedding_similarity() -> None:
    a = text_to_pseudo_embedding("查询工单")
    b = text_to_pseudo_embedding("查询工单状态")
    c = text_to_pseudo_embedding("完全无关主题 xyz")
    assert len(a) == EMBED_DIM
    dot_ab = sum(x * y for x, y in zip(a, b, strict=True))
    dot_ac = sum(x * y for x, y in zip(a, c, strict=True))
    assert dot_ab > dot_ac


def test_merge_search_ids() -> None:
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    assert merge_search_ids(None, None) is None
    assert merge_search_ids([a, b], [b, c]) == [a, b, c]


@pytest.mark.infra
@pytest.mark.pgvector
async def test_pgvector_nearest_neighbor(db: Database) -> None:
    async with db.tx(DbScope.system()) as conn:
        if not await pgvector_ready(conn):
            pytest.skip("pgvector not installed; run tools/dev.py pgvector-up")
        company_id = uuid.uuid4()
        target = uuid.uuid4()
        noise = uuid.uuid4()
        await upsert_catalog_embedding(
            conn,
            asset_id=target,
            company_id=company_id,
            search_text="查询工单状态 tool.ticket.query",
        )
        await upsert_catalog_embedding(
            conn,
            asset_id=noise,
            company_id=company_id,
            search_text="天气播报 unrelated weather",
        )
        hits = await search_asset_ids_by_vector(conn, "查询工单", limit=5)
        assert hits is not None
        assert hits[0] == target
