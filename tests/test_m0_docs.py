"""M0 / P0a：文档契约（错误码登记、JSON Schema、OpenAPI、ADR）。"""

from __future__ import annotations

import json
import pathlib
import re

import jsonschema
import pytest
import yaml
from openapi_spec_validator import validate
from openapi_spec_validator.readers import read_from_filename

from aip.libs.common.errors import ERROR_META, ErrorCode

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPECS = ROOT / "docs" / "specs"


def schema(name: str) -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(json.loads((SPECS / f"{name}.schema.json").read_text(encoding="utf-8")))


def test_error_codes_match_registry() -> None:
    md = (ROOT / "docs/api/error-codes.md").read_text(encoding="utf-8")
    rows = re.findall(r"^\| ([A-Z_]+) \| (\d{3}) \| (true|false) \|", md, re.M)
    registry = {code: (int(status), retry == "true") for code, status, retry in rows}
    code_side = {str(c): ERROR_META[c] for c in ErrorCode}
    assert registry == code_side


@pytest.mark.parametrize(
    "name", ["tool-spec", "workflow-dsl", "policy-input", "declarative-connector", "system-connection"]
)
def test_json_schemas_are_valid(name: str) -> None:
    jsonschema.Draft202012Validator.check_schema(
        json.loads((SPECS / f"{name}.schema.json").read_text(encoding="utf-8"))
    )


READ_TOOL = {
    "capability_id": "production.mes.query_work_orders",
    "domain": "production",
    "name": "query_work_orders",
    "description": "查询指定工厂的生产工单，按状态过滤，返回工单号、计划数量和状态。",
    "input_schema": {"type": "object"},
    "output_schema": {"type": "object"},
    "connector_ref": {"connector": "mes_vn01", "operation": "query_work_orders"},
    "risk_level": "read",
    "timeout": {"connect_ms": 2000, "read_ms": 5000, "deadline_ms": 8000},
    "data_classification": 2,
    "identity_mode": "service_account",
    "confirm_policy": "auto",
    "scope_binding": {"factory_id": {"dimension": "company", "map": "by_code", "system_key": "mes"}},
}


def test_tool_spec_accepts_read_tool_and_rejects_unsafe_write() -> None:
    v = schema("tool-spec")
    assert not list(v.iter_errors(READ_TOOL))
    write = {**READ_TOOL, "risk_level": "write"}
    assert list(v.iter_errors(write)), "write 类缺 idempotency / recovery / reconcile 必须被拒"
    danger_auto = {
        **write,
        "risk_level": "danger",
        "idempotency": {"idempotent": True},
        "recovery": "manual",
        "reconcile": {"operation": "q", "by": "business_no"},
    }
    assert list(v.iter_errors(danger_auto)), "danger 不允许 confirm_policy=auto"
    assert list(v.iter_errors({**READ_TOOL, "unknown_field": 1}))


EXP = "2027-01-01T00:00:00Z"


@pytest.mark.parametrize(
    ("conn", "valid"),
    [
        (
            {
                "system_key": "mes",
                "env": "prod",
                "base_url": "http://10.1.2.3",
                "scheme": "http",
                "transport_exception_reason": "仅支持 HTTP",
                "transport_exception_expires_at": EXP,
            },
            True,
        ),
        ({"system_key": "mes", "env": "prod", "base_url": "http://10.1.2.3", "scheme": "http"}, False),
        (
            {
                "system_key": "mes",
                "env": "prod",
                "base_url": "https://10.1.2.3",
                "scheme": "https",
                "tls_mode": "pinned",
                "pins": [{"kind": "spki", "sha256": "a" * 64}],
            },
            True,
        ),
        (
            {
                "system_key": "mes",
                "env": "prod",
                "base_url": "https://10.1.2.3",
                "scheme": "https",
                "tls_mode": "pinned",
            },
            False,
        ),
        (
            {
                "system_key": "mes",
                "env": "prod",
                "base_url": "https://10.1.2.3",
                "scheme": "https",
                "tls_mode": "insecure",
                "transport_exception_reason": "无法获取 CA",
                "transport_exception_expires_at": EXP,
            },
            True,
        ),
        (
            {
                "system_key": "mes",
                "env": "prod",
                "base_url": "https://10.1.2.3",
                "scheme": "https",
                "tls_mode": "insecure",
            },
            False,
        ),
        (
            {
                "system_key": "mes",
                "env": "prod",
                "base_url": "http://10.1.2.3",
                "scheme": "https",
                "tls_mode": "system_ca",
            },
            False,
        ),
    ],
)
def test_system_connection_schema(conn: dict[str, object], valid: bool) -> None:
    assert (not list(schema("system-connection").iter_errors(conn))) is valid


def test_workflow_dsl_example_from_architecture() -> None:
    dsl = {
        "name": "purchase_order_review",
        "version": "1.0.0",
        "budget": {"max_steps": 30, "max_llm_calls": 3, "timeout_s": 600},
        "nodes": [
            {"id": "n1", "type": "tool", "ref": "finance.erp.get_po@1.2.0", "next": "n2"},
            {
                "id": "n2",
                "type": "llm",
                "model_profile": "mp_extract_v1@1.0.0",
                "prompt": "po_risk_extract@3.0.0",
                "output_schema": "RiskFlags",
                "next": "n3",
            },
            {"id": "n3", "type": "condition", "expr": "n2.risk_level == 'high'", "then": "n4", "else": "n5"},
            {
                "id": "n4",
                "type": "human_task",
                "kind": "approve",
                "assignee": "role:purchase_manager@org_of(input.company)",
                "next": "n5",
            },
            {
                "id": "n5",
                "type": "tool",
                "ref": "finance.erp.approve_po@1.0.1",
                "compensation": "finance.erp.revoke_approval@1.0.0",
                "next": "end",
            },
        ],
    }
    v = schema("workflow-dsl")
    assert not list(v.iter_errors(dsl))
    dsl["nodes"][0]["ref"] = "finance.erp.get_po"  # 未锁版本
    assert list(v.iter_errors(dsl))


@pytest.mark.parametrize("name", ["control-plane", "data-plane"])
def test_openapi_valid_and_operation_ids_unique(name: str) -> None:
    spec, url = read_from_filename(str(ROOT / f"docs/api/{name}.openapi.yaml"))
    validate(spec, base_uri=url)
    ids = [m["operationId"] for p in spec["paths"].values() for m in p.values()]
    assert len(ids) == len(set(ids))
    assert "&id" not in (ROOT / f"docs/api/{name}.openapi.yaml").read_text(encoding="utf-8"), "禁止 YAML 别名"


def test_openapi_has_tls_and_oauth_endpoints() -> None:
    cp = yaml.safe_load((ROOT / "docs/api/control-plane.openapi.yaml").read_text(encoding="utf-8"))
    dp = yaml.safe_load((ROOT / "docs/api/data-plane.openapi.yaml").read_text(encoding="utf-8"))
    assert "/api/v1/connections/{id}/test" in cp["paths"]
    for p in ["/oauth/token", "/.well-known/oauth-protected-resource", "/mcp", "/api/v1/confirmations/{id}/confirm"]:
        assert p in dp["paths"]


def test_adrs_complete() -> None:
    adrs = sorted(p.name for p in (ROOT / "docs/adr").glob("0*.md"))
    assert adrs == ["0000-template.md", *[f"{i:04d}.md" for i in range(1, 26)]]
