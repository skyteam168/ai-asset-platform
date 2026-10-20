"""分公司可见其授权上限内的总部资产（只读元数据）

跨公司可见性只通过授权上限、Release 开放范围与 Grant 表达（architecture.md 2.6）；不在上限内的资产对分公司不可见。

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-19
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "CREATE POLICY ceiling_visibility ON registry.asset FOR SELECT USING (EXISTS ("
        "SELECT 1 FROM grants.grant_ceiling c WHERE c.status = 'active' AND registry.asset.id = ANY (c.asset_ids) "
        "AND c.company_id = ANY (app_company_ids())))"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS ceiling_visibility ON registry.asset")
