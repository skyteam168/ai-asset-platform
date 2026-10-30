"""G5.3 / H58：pgvector + catalog_embedding（无扩展时 no-op）

Revision ID: 0029
Revises: 0028
Create Date: 2026-10-30
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def upgrade() -> None:
    # dim=32 与 aip.control_plane.registry.vector.EMBED_DIM 一致
    op.execute(
        """
        DO $vec$
        BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'vector') THEN
            RAISE NOTICE 'pgvector not available; skip G5.3 catalog_embedding';
            RETURN;
          END IF;
          CREATE EXTENSION IF NOT EXISTS vector;
          CREATE TABLE IF NOT EXISTS registry.catalog_embedding (
            asset_id uuid PRIMARY KEY,
            company_id uuid NOT NULL,
            embedding vector(32) NOT NULL,
            updated_at timestamptz NOT NULL DEFAULT now()
          );
          CREATE INDEX IF NOT EXISTS catalog_embedding_hnsw_idx
            ON registry.catalog_embedding USING hnsw (embedding vector_cosine_ops);
          ALTER TABLE registry.catalog_embedding ENABLE ROW LEVEL SECURITY;
          DROP POLICY IF EXISTS catalog_embedding_tenant ON registry.catalog_embedding;
          CREATE POLICY catalog_embedding_tenant ON registry.catalog_embedding
            USING (app_bypass_rls() OR company_id = ANY (app_company_ids()));
          GRANT SELECT, INSERT, UPDATE, DELETE ON registry.catalog_embedding TO aip_app;
        END
        $vec$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $vec$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'registry' AND table_name = 'catalog_embedding'
          ) THEN
            DROP TABLE registry.catalog_embedding;
          END IF;
        END
        $vec$;
        """
    )
