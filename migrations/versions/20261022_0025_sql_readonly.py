"""H39：sql_readonly — scheme postgres/mysql、kind、sql_query_template（ADR-0026）

Revision ID: 0025
Revises: 0024
Create Date: 2026-10-22
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE conn.system_connection DROP CONSTRAINT IF EXISTS system_connection_scheme_check")
    op.execute(
        "ALTER TABLE conn.system_connection ADD CONSTRAINT system_connection_scheme_check "
        "CHECK (scheme IN ('https','http','postgres','mysql'))"
    )
    # http <=> tls_mode IS NULL；其余 scheme 必须有 tls_mode
    op.execute("ALTER TABLE conn.system_connection DROP CONSTRAINT IF EXISTS system_connection_check")
    op.execute(
        "ALTER TABLE conn.system_connection ADD CONSTRAINT system_connection_http_tls_check "
        "CHECK ("
        " (scheme = 'http' AND tls_mode IS NULL)"
        " OR (scheme IN ('https','postgres','mysql') AND tls_mode IS NOT NULL)"
        ")"
    )
    # pinned 仅 https
    op.execute(
        "ALTER TABLE conn.system_connection DROP CONSTRAINT IF EXISTS system_connection_pinned_https_check"
    )
    op.execute(
        "ALTER TABLE conn.system_connection ADD CONSTRAINT system_connection_pinned_https_check "
        "CHECK (tls_mode IS DISTINCT FROM 'pinned' OR scheme = 'https')"
    )

    op.execute("ALTER TABLE conn.connector_instance DROP CONSTRAINT IF EXISTS connector_instance_kind_check")
    op.execute(
        "ALTER TABLE conn.connector_instance ADD CONSTRAINT connector_instance_kind_check "
        "CHECK (kind IN ('declarative','kb','mock','upstream_mcp','sql_readonly'))"
    )

    op.execute(
        """
        CREATE TABLE conn.sql_query_template (
          id uuid PRIMARY KEY,
          company_id uuid NOT NULL,
          connector_instance_id uuid NOT NULL REFERENCES conn.connector_instance(id),
          template_key text NOT NULL,
          engine text NOT NULL CHECK (engine IN ('postgres','mysql')),
          statement text NOT NULL,
          param_schema jsonb NOT NULL DEFAULT '{}',
          max_rows integer NOT NULL DEFAULT 200
            CHECK (max_rows >= 1 AND max_rows <= 1000),
          statement_timeout_ms integer NOT NULL DEFAULT 5000
            CHECK (statement_timeout_ms >= 100 AND statement_timeout_ms <= 60000),
          explain_cost_cap numeric,
          output_field_whitelist jsonb NOT NULL DEFAULT '[]',
          status text NOT NULL DEFAULT 'draft'
            CHECK (status IN ('draft','active','disabled')),
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (connector_instance_id, template_key)
        )
        """
    )
    op.execute(
        "ALTER TABLE conn.sql_query_template ENABLE ROW LEVEL SECURITY"
    )
    op.execute(
        """
        CREATE POLICY sql_query_template_rls ON conn.sql_query_template
          USING (app_bypass_rls() OR company_id = ANY (app_company_ids()))
          WITH CHECK (app_bypass_rls() OR company_id = ANY (app_company_ids()))
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON conn.sql_query_template TO aip_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS conn.sql_query_template CASCADE")
    op.execute("ALTER TABLE conn.connector_instance DROP CONSTRAINT IF EXISTS connector_instance_kind_check")
    op.execute(
        "ALTER TABLE conn.connector_instance ADD CONSTRAINT connector_instance_kind_check "
        "CHECK (kind IN ('declarative','kb','mock','upstream_mcp'))"
    )
    op.execute("ALTER TABLE conn.system_connection DROP CONSTRAINT IF EXISTS system_connection_pinned_https_check")
    op.execute("ALTER TABLE conn.system_connection DROP CONSTRAINT IF EXISTS system_connection_http_tls_check")
    op.execute(
        "ALTER TABLE conn.system_connection ADD CONSTRAINT system_connection_check "
        "CHECK ((scheme = 'http') = (tls_mode IS NULL))"
    )
    op.execute("ALTER TABLE conn.system_connection DROP CONSTRAINT IF EXISTS system_connection_scheme_check")
    op.execute(
        "ALTER TABLE conn.system_connection ADD CONSTRAINT system_connection_scheme_check "
        "CHECK (scheme IN ('https','http'))"
    )
