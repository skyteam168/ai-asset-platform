"""H18：wf_run.ctx / wf_step.output 信封存储。"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from aip.control_plane.db import Database, DbScope
from aip.data_plane.workflow.engine import _hydrate_run, _hydrate_step, _seal_ctx, _seal_output
from aip.libs.crypto.envelope import is_envelope


def test_seal_ctx_and_output_roundtrip() -> None:
    cid = str(uuid.uuid4())
    plain, ref = _seal_ctx(cid, {"input": {"x": 1}, "step_a": {"y": "密"}})
    assert plain == "{}"
    assert is_envelope(json.loads(ref))
    row = {
        "company_id": cid,
        "ctx": {},
        "ctx_ref": json.loads(ref),
        "input_ref": None,
        "result_ref": None,
    }
    assert _hydrate_run(row)["ctx"]["step_a"]["y"] == "密"
    o_plain, o_ref = _seal_output(cid, {"decision": True})
    assert o_plain is None
    step = _hydrate_step({"output": None, "output_ref": json.loads(o_ref)}, cid)
    assert step is not None and step["output"] == {"decision": True}


@pytest.mark.infra
@pytest.mark.asyncio
async def test_wf_run_ctx_not_plaintext_in_db(db: Database) -> None:
    """迁移后列存在；密封写入后 ctx/output 列不含业务明文。"""
    async with db.tx(DbScope.system()) as conn:
        cols = (
            await conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='exec' AND table_name='wf_run' AND column_name='ctx_ref'"
                )
            )
        ).scalar_one_or_none()
        assert cols == "ctx_ref"
        step_col = (
            await conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='exec' AND table_name='wf_step' AND column_name='output_ref'"
                )
            )
        ).scalar_one_or_none()
        assert step_col == "output_ref"
