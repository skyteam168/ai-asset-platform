from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from aip.control_plane.db import Database
from iam_seed import Seed, app_url, create_test_database, seed, truncate

_created = False


@pytest.fixture
async def db() -> AsyncIterator[Database]:
    """独立测试库 aip_test：首次使用时重建并迁移，每个测试前清空 iam 表。应用连接用非超级用户 aip_app。"""
    global _created
    if not _created:
        await create_test_database()
        _created = True
    await truncate()
    database = Database(app_url())
    yield database
    await database.dispose()


@pytest.fixture
async def seeded(db: Database) -> Seed:
    return await seed(db)
