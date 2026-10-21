"""M7d：上游 MCP 来源表；connector_instance.kind 增加 upstream_mcp

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-21
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE conn.connector_instance DROP CONSTRAINT IF EXISTS connector_instance_kind_check")
    op.execute(
        "ALTER TABLE conn.connector_instance ADD CONSTRAINT connector_instance_kind_check "
        "CHECK (kind IN ('declarative','kb','mock','upstream_mcp'))"
    )
    op.execute(
        """
        CREATE TABLE conn.upstream_mcp_source (
          id uuid PRIMARY KEY,
          company_id uuid NOT NULL,
          name text NOT NULL,
          system_connection_id uuid NOT NULL REFERENCES conn.system_connection(id),
          mcp_path text NOT NULL DEFAULT '/mcp',
          tools_manifest_hash text NOT NULL,
          pinned_tools jsonb NOT NULL DEFAULT '[]',
          status text NOT NULL DEFAULT 'active'
            CHECK (status IN ('active','disabled')),
          created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (company_id, name)
        )
        """
    )
    op.execute("ALTER TABLE conn.upstream_mcp_source ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY upstream_mcp_source_rls ON conn.upstream_mcp_source "
        "USING (app_bypass_rls() OR company_id = ANY (app_company_ids())) "
        "WITH CHECK (app_bypass_rls() OR company_id = ANY (app_company_ids()))"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON conn.upstream_mcp_source TO aip_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS conn.upstream_mcp_source")
    op.execute("ALTER TABLE conn.connector_instance DROP CONSTRAINT IF EXISTS connector_instance_kind_check")
    op.execute(
        "ALTER TABLE conn.connector_instance ADD CONSTRAINT connector_instance_kind_check "
        "CHECK (kind IN ('declarative','kb','mock'))"
    )
