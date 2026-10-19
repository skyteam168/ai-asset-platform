"""baseline：扩展、schema、RLS 会话设置辅助函数、审计仅追加角色

Revision ID: 0001
Revises:
Create Date: 2026-10-19
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMAS = ("iam", "registry", "grants", "conn", "exec", "audit")


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS ltree")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    for s in SCHEMAS:
        op.execute(f"CREATE SCHEMA IF NOT EXISTS {s}")
    # RLS 读取的会话设置（architecture.md 2.6）：每个事务 SET LOCAL app.company_ids = '<uuid>,<uuid>'
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.app_company_ids() RETURNS uuid[]
        LANGUAGE sql STABLE AS $$
          SELECT COALESCE(string_to_array(NULLIF(current_setting('app.company_ids', true), ''), ',')::uuid[], '{}')
        $$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.app_bypass_rls() RETURNS boolean
        LANGUAGE sql STABLE AS $$ SELECT COALESCE(current_setting('app.bypass_rls', true), '') = 'on' $$
        """
    )
    # 应用连接使用的非 owner 角色，RLS 对它生效；审计 schema 只授予 INSERT / SELECT
    op.execute(
        """
        DO $$ BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aip_app') THEN
            CREATE ROLE aip_app NOLOGIN;
          END IF;
        END $$
        """
    )
    for s in SCHEMAS:
        op.execute(f"GRANT USAGE ON SCHEMA {s} TO aip_app")
        if s != "audit":
            op.execute(
                f"ALTER DEFAULT PRIVILEGES IN SCHEMA {s} GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO aip_app"
            )
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA audit GRANT SELECT, INSERT ON TABLES TO aip_app")


def downgrade() -> None:
    for s in reversed(SCHEMAS):
        op.execute(f"DROP SCHEMA IF EXISTS {s} CASCADE")
    op.execute("DROP FUNCTION IF EXISTS public.app_bypass_rls()")
    op.execute("DROP FUNCTION IF EXISTS public.app_company_ids()")
