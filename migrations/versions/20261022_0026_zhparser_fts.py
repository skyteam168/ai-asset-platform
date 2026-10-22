"""H41：zhparser + chinese 全文配置 + catalog_fts（无扩展时 no-op）

Revision ID: 0026
Revises: 0025
Create Date: 2026-10-22
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # alpine 默认无 zhparser：整段跳过，保持 CI / 默认栈可用
    op.execute(
        """
        DO $zh$
        BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'zhparser') THEN
            RAISE NOTICE 'zhparser not available; skip H41 FTS';
            RETURN;
          END IF;
          CREATE EXTENSION IF NOT EXISTS zhparser;
          IF NOT EXISTS (SELECT 1 FROM pg_ts_config WHERE cfgname = 'chinese') THEN
            CREATE TEXT SEARCH CONFIGURATION chinese (PARSER = zhparser);
            ALTER TEXT SEARCH CONFIGURATION chinese
              ADD MAPPING FOR n,v,a,i,e,l,j,o,x WITH simple;
          END IF;
          CREATE TABLE IF NOT EXISTS registry.catalog_fts (
            asset_id uuid PRIMARY KEY,
            company_id uuid NOT NULL,
            search_text text NOT NULL DEFAULT '',
            tsv tsvector GENERATED ALWAYS AS (to_tsvector('chinese', coalesce(search_text, ''))) STORED,
            updated_at timestamptz NOT NULL DEFAULT now()
          );
          CREATE INDEX IF NOT EXISTS catalog_fts_tsv_idx ON registry.catalog_fts USING gin (tsv);
          ALTER TABLE registry.catalog_fts ENABLE ROW LEVEL SECURITY;
          DROP POLICY IF EXISTS catalog_fts_tenant ON registry.catalog_fts;
          CREATE POLICY catalog_fts_tenant ON registry.catalog_fts
            USING (app_bypass_rls() OR company_id = ANY (app_company_ids()));
          GRANT SELECT, INSERT, UPDATE, DELETE ON registry.catalog_fts TO aip_app;
        END
        $zh$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $zh$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'registry' AND table_name = 'catalog_fts'
          ) THEN
            DROP TABLE registry.catalog_fts;
          END IF;
        END
        $zh$;
        """
    )
