"""控制面目录向量检索（G5.3 / H58；依赖 pgvector，缺扩展时降级）。

默认用确定性伪 embedding（字符 n-gram 哈希 → L2 归一化），证明写入与近邻通路；
生产可替换为 LLM Gateway 真 embedding，维度保持 EMBED_DIM。
"""

from __future__ import annotations

import hashlib
import math
import re
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

EMBED_DIM = 32
_TOKEN = re.compile(r"[\w\u4e00-\u9fff]+", re.UNICODE)


def text_to_pseudo_embedding(raw: str, *, dim: int = EMBED_DIM) -> list[float]:
    """确定性伪向量：便于无外部模型时测通路；非生产语义质量。"""
    tokens = _TOKEN.findall((raw or "").lower())
    if not tokens:
        tokens = ["_"]
    vec = [0.0] * dim
    for tok in tokens:
        for i in range(len(tok) - 1 or 1):
            gram = tok[i : i + 2] if len(tok) > 1 else tok
            h = hashlib.sha256(gram.encode("utf-8")).digest()
            idx = int.from_bytes(h[:2], "big") % dim
            sign = 1.0 if h[2] % 2 == 0 else -1.0
            vec[idx] += sign
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def _vec_literal(vals: list[float]) -> str:
    return "[" + ",".join(f"{x:.8f}" for x in vals) + "]"


async def pgvector_ready(conn: AsyncConnection) -> bool:
    row = (
        await conn.execute(
            text(
                """
                SELECT EXISTS (
                  SELECT 1 FROM pg_extension WHERE extname = 'vector'
                ) AND EXISTS (
                  SELECT 1 FROM information_schema.tables
                  WHERE table_schema = 'registry' AND table_name = 'catalog_embedding'
                ) AS ok
                """
            )
        )
    ).mappings().first()
    return bool(row and row["ok"])


async def upsert_catalog_embedding(
    conn: AsyncConnection,
    *,
    asset_id: uuid.UUID,
    company_id: uuid.UUID,
    search_text: str,
    embedding: list[float] | None = None,
) -> None:
    if not await pgvector_ready(conn):
        return
    vec = embedding if embedding is not None else text_to_pseudo_embedding(search_text)
    if len(vec) != EMBED_DIM:
        raise ValueError(f"embedding dim must be {EMBED_DIM}")
    await conn.execute(
        text(
            """
            INSERT INTO registry.catalog_embedding (asset_id, company_id, embedding, updated_at)
            VALUES (:aid, :cid, CAST(:emb AS vector), now())
            ON CONFLICT (asset_id) DO UPDATE SET
              company_id = EXCLUDED.company_id,
              embedding = EXCLUDED.embedding,
              updated_at = now()
            """
        ),
        {"aid": asset_id, "cid": company_id, "emb": _vec_literal(vec)},
    )


async def search_asset_ids_by_vector(
    conn: AsyncConnection,
    query: str,
    *,
    limit: int = 200,
    embedding: list[float] | None = None,
) -> list[uuid.UUID] | None:
    """有 pgvector 时按余弦距离近邻；无扩展返回 None。"""
    q = (query or "").strip()
    if not q and embedding is None:
        return []
    if not await pgvector_ready(conn):
        return None
    vec = embedding if embedding is not None else text_to_pseudo_embedding(q)
    if len(vec) != EMBED_DIM:
        raise ValueError(f"embedding dim must be {EMBED_DIM}")
    rows = (
        await conn.execute(
            text(
                """
                SELECT asset_id FROM registry.catalog_embedding
                ORDER BY embedding <=> CAST(:emb AS vector)
                LIMIT :lim
                """
            ),
            {"emb": _vec_literal(vec), "lim": limit},
        )
    ).mappings().all()
    return [uuid.UUID(str(r["asset_id"])) for r in rows]


def merge_search_ids(
    fts_ids: list[uuid.UUID] | None,
    vector_ids: list[uuid.UUID] | None,
) -> list[uuid.UUID] | None:
    """双通道合并：任一通道可用则返回并集（FTS 优先序）；皆不可用返回 None。"""
    if fts_ids is None and vector_ids is None:
        return None
    seen: set[uuid.UUID] = set()
    out: list[uuid.UUID] = []
    for src in (fts_ids or [], vector_ids or []):
        for i in src:
            if i not in seen:
                seen.add(i)
                out.append(i)
    return out
