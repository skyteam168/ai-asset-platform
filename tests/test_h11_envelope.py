"""H11：公司作用域信封加密（local 后端；Transit 可选）。"""

from __future__ import annotations

import inspect
import uuid

import pytest

from aip.data_plane.runtime import PgExecutionStore, ToolRuntime
from aip.libs.common.config import get_settings
from aip.libs.crypto.envelope import decrypt_json, encrypt_json, is_envelope


def test_roundtrip_local() -> None:
    assert get_settings().envelope_backend == "local"
    cid = str(uuid.uuid4())
    payload = {"factory_id": "F1", "note": "机密"}
    ref = encrypt_json(cid, payload)
    assert is_envelope(ref)
    assert decrypt_json(cid, ref) == payload


def test_wrong_company_rejected() -> None:
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    ref = encrypt_json(a, {"x": 1})
    with pytest.raises(ValueError, match="公司"):
        decrypt_json(b, ref)


def test_pg_params_clear_plaintext_result() -> None:
    cid = str(uuid.uuid4())
    rec = {
        "id": str(uuid.uuid4()),
        "company_id": cid,
        "capability_id": "ops.h11.demo",
        "version": "1.0.0",
        "consumer_id": str(uuid.uuid4()),
        "end_user_id": None,
        "agent_profile_id": None,
        "idempotency_key": None,
        "args_sha256": "a" * 64,
        "args": {"factory_id": "F1"},
        "status": "succeeded",
        "result": {"wo": 1, "secret": "plain"},
        "error_code": None,
        "retry_count": 0,
        "reconcile_status": None,
        "transport": "https",
        "tls_verified": True,
        "trace_id": "t",
        "confirmation_id": None,
    }
    params = PgExecutionStore._params(rec)
    assert params["result"] is None
    assert params["result_ref"] is not None
    assert "plain" not in (params["result_ref"] or "")
    hydrated = PgExecutionStore._hydrate(
        {
            **{k: rec.get(k) for k in PgExecutionStore.COLS if k not in ("result", "input_ref", "result_ref")},
            "result": None,
            "input_ref": encrypt_json(cid, rec["args"]),
            "result_ref": encrypt_json(cid, rec["result"]),
        }
    )
    assert hydrated["result"] == rec["result"]
    assert hydrated["args"] == rec["args"]


def test_audit_excludes_payload_fields() -> None:
    src = inspect.getsource(ToolRuntime._audit)
    assert '"result"' not in src and '"args"' not in src
    assert "args_sha256" in src
