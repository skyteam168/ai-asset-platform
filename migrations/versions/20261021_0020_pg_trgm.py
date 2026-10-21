"""H17：启用 pg_trgm（控制面目录 FTS / 相似度检索地基；zhparser 仍为可选镜像）

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-21
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # alpine 官方镜像含 contrib；生产 zhparser 见 docs/ops/search-index.md
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")


def downgrade() -> None:
    # 扩展可能被他处依赖，降级不 DROP EXTENSION
    pass
