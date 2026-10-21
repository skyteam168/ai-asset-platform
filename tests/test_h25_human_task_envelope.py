"""H25：human_task.data 信封存储。"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from aip.control_plane.db import Database, DbScope
from aip.data_plane.workflow.engine import _hydrate_task, _seal_data
from aip.libs.crypto.envelope import is_envelope


def test_seal_data_roundtrip() -> None:
    cid = str(uuid.uuid4())
    plain, ref = _seal_data(cid, {"input_schema": {"type": "object"}, "submitted": {"x": "密"}})
    assert plain == "{}"
    assert is_envelope(json.loads(ref))
    row = {
        "id": str(uuid.uuid4()),
        "company_id": cid,
        "data": {},
        "data_ref": json.loads(ref),
    }
    hydrated = _hydrate_task(row)
    assert hydrated is not None
    assert hydrated["data"]["submitted"]["x"] == "密"


def test_hydrate_task_plaintext_legacy() -> None:
    row = {
        "id": str(uuid.uuid4()),
        "company_id": str(uuid.uuid4()),
        "data": {"input_schema": {"type": "object"}},
        "data_ref": None,
    }
    hydrated = _hydrate_task(row)
    assert hydrated is not None
    assert hydrated["data"]["input_schema"]["type"] == "object"


@pytest.mark.infra
@pytest.mark.asyncio
async def test_human_task_data_ref_column(db: Database) -> None:
    async with db.tx(DbScope.system()) as conn:
        col = (
            await conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='exec' AND table_name='human_task' AND column_name='data_ref'"
                )
            )
        ).scalar_one_or_none()
        assert col == "data_ref"
