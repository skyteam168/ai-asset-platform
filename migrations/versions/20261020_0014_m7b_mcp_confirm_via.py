"""M7b：confirmation.decided_via 允许 mcp（交互式确认通道）

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-20
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE exec.confirmation DROP CONSTRAINT IF EXISTS confirmation_decided_via_check")
    op.execute(
        "ALTER TABLE exec.confirmation ADD CONSTRAINT confirmation_decided_via_check "
        "CHECK (decided_via IN ('callback','console','workflow','mcp'))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE exec.confirmation DROP CONSTRAINT IF EXISTS confirmation_decided_via_check")
    op.execute(
        "ALTER TABLE exec.confirmation ADD CONSTRAINT confirmation_decided_via_check "
        "CHECK (decided_via IN ('callback','console','workflow'))"
    )
