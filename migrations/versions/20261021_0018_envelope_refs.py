"""H11：tool_execution / wf_run 信封加密引用列（architecture.md 12.6）

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-21
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE exec.tool_execution ADD COLUMN IF NOT EXISTS input_ref jsonb")
    op.execute("ALTER TABLE exec.tool_execution ADD COLUMN IF NOT EXISTS result_ref jsonb")
    op.execute("ALTER TABLE exec.wf_run ADD COLUMN IF NOT EXISTS input_ref jsonb")
    op.execute("ALTER TABLE exec.wf_run ADD COLUMN IF NOT EXISTS result_ref jsonb")


def downgrade() -> None:
    op.execute("ALTER TABLE exec.wf_run DROP COLUMN IF EXISTS result_ref")
    op.execute("ALTER TABLE exec.wf_run DROP COLUMN IF EXISTS input_ref")
    op.execute("ALTER TABLE exec.tool_execution DROP COLUMN IF EXISTS result_ref")
    op.execute("ALTER TABLE exec.tool_execution DROP COLUMN IF EXISTS input_ref")
