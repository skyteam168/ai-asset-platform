"""Generate docs/specs/*.schema.json (architecture.md 4.4, 6.1, 6.3, 9.1, 9.6). Source of truth for the specs."""

from __future__ import annotations

import json
import pathlib
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "specs"
D = "https://json-schema.org/draft/2020-12/schema"
B = "https://schemas.aip.internal/"

SEMVER = {"type": "string", "pattern": r"^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$"}
REF_AT = {"type": "string", "pattern": r"^[a-z0-9_.]+@\d+\.\d+\.\d+$"}
CAP = {"type": "string", "pattern": r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$"}
RISK = {"enum": ["read", "write", "danger"]}
CONFIRM = {"enum": ["auto", "user_confirm", "approval"]}


def obj(props: dict[str, Any], required: list[str] | None = None, extra: bool = True, **kw: Any) -> dict[str, Any]:
    o: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        o["required"] = required
    if not extra:
        o["additionalProperties"] = False
    o.update(kw)
    return o


def when(prop: str, value: Any, then: dict[str, Any], const: bool = True) -> dict[str, Any]:
    cond = {"const": value} if const else {"enum": value}
    return {"if": {"properties": {prop: cond}, "required": [prop]}, "then": then}


TOOL = obj(
    {
        "capability_id": CAP,
        "domain": {"type": "string"},
        "name": {"type": "string", "maxLength": 64},
        "title": {"type": "string"},
        "title_i18n": {"type": "object", "additionalProperties": {"type": "string"}},
        "description": {"type": "string", "minLength": 20, "maxLength": 2000},
        "description_lang": {"enum": ["zh", "en", "vi"], "default": "zh"},
        "usage_notes": {"type": "string"},
        "not_for": {"type": "array", "items": {"type": "string"}},
        "examples": {
            "type": "array",
            "items": obj({"question": {"type": "string"}, "args": {"type": "object"}}, ["question", "args"]),
        },
        "input_schema": {"type": "object"},
        "output_schema": {"type": "object"},
        "connector_ref": obj(
            {"connector": {"type": "string"}, "operation": {"type": "string"}, "package_version": SEMVER},
            ["connector", "operation"],
            extra=False,
        ),
        "output_limits": obj(
            {
                "max_items": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 200},
                "max_bytes": {"type": "integer", "minimum": 1024, "default": 65536},
                "pagination": {"enum": ["none", "cursor"], "default": "none"},
            }
        ),
        "risk_level": RISK,
        "idempotency": obj({"idempotent": {"type": "boolean"}, "key_rule": {"type": "string"}}, ["idempotent"]),
        "timeout": obj(
            {
                "connect_ms": {"type": "integer", "minimum": 100},
                "read_ms": {"type": "integer", "minimum": 100},
                "deadline_ms": {"type": "integer", "minimum": 100},
            },
            ["connect_ms", "read_ms", "deadline_ms"],
        ),
        "retry_class": {
            "type": "object",
            "additionalProperties": obj(
                {
                    "max_attempts": {"type": "integer", "minimum": 0, "maximum": 5},
                    "backoff_ms": {"type": "integer", "minimum": 0},
                }
            ),
        },
        "rate_limit": obj(
            {"per_second": {"type": "number", "exclusiveMinimum": 0}, "burst": {"type": "integer", "minimum": 1}}
        ),
        "breaker": obj(
            {
                "failure_ratio": {"type": "number", "minimum": 0, "maximum": 1},
                "min_calls": {"type": "integer"},
                "open_seconds": {"type": "integer"},
            }
        ),
        "reconcile": obj(
            {"operation": {"type": "string"}, "by": {"enum": ["idempotency_key", "business_no"]}}, ["operation", "by"]
        ),
        "verify": obj({"operation": {"type": "string"}, "assert": {"type": "string"}}),
        "recovery": {"enum": ["none", "retry", "compensate", "manual"], "default": "none"},
        "compensation_ref": REF_AT,
        "irreversible": {"type": "boolean", "default": False},
        "identity_mode": {"enum": ["service_account", "user_delegated", "hybrid"]},
        "scope_binding": {
            "type": "object",
            "additionalProperties": obj(
                {
                    "dimension": {"enum": ["company", "org"]},
                    "map": {"enum": ["by_id", "by_code"]},
                    "system_key": {"type": "string"},
                },
                ["dimension", "map"],
            ),
        },
        "data_classification": {"type": "integer", "minimum": 1, "maximum": 4},
        "required_scopes": {"type": "array", "items": {"type": "string"}},
        "pii_fields": {"type": "array", "items": {"type": "string"}},
        "confirm_policy": CONFIRM,
        "confirm_template": {"type": "string"},
        "app_identity_allowed": {"type": "boolean"},
        "probe": obj(
            {
                "args": {"type": "object"},
                "interval_s": {"type": "integer", "minimum": 300, "default": 3600},
                "assert": {"type": "string"},
            }
        ),
        "egress_class": {"enum": ["none", "internal", "external"], "default": "none"},
    },
    [
        "capability_id",
        "domain",
        "name",
        "description",
        "input_schema",
        "output_schema",
        "connector_ref",
        "risk_level",
        "timeout",
        "data_classification",
        "identity_mode",
        "confirm_policy",
    ],
    extra=False,
    allOf=[
        when("risk_level", ["write", "danger"], {"required": ["idempotency", "recovery", "reconcile"]}, const=False),
        when("recovery", "compensate", {"required": ["compensation_ref"]}),
        when("risk_level", "danger", {"properties": {"confirm_policy": {"enum": ["user_confirm", "approval"]}}}),
    ],
)

NODE_TYPES = ["tool", "llm", "condition", "parallel", "loop", "human_task", "sub_workflow", "wait", "end"]
NODE = obj(
    {
        "id": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"},
        "type": {"enum": NODE_TYPES},
        "next": {"type": "string"},
    },
    ["id", "type"],
    allOf=[
        when(
            "type",
            "tool",
            {
                "required": ["ref"],
                "properties": {"ref": REF_AT, "compensation": REF_AT, "irreversible": {"type": "boolean"}},
            },
        ),
        when(
            "type",
            "llm",
            {
                "required": ["model_profile", "prompt", "output_schema"],
                "properties": {"model_profile": REF_AT, "prompt": REF_AT, "output_schema": {"type": "string"}},
            },
        ),
        when("type", "condition", {"required": ["expr", "then", "else"]}),
        when(
            "type",
            "loop",
            {
                "required": ["max_iterations", "body"],
                "properties": {"max_iterations": {"type": "integer", "minimum": 1, "maximum": 100}},
            },
        ),
        when(
            "type",
            "human_task",
            {
                "required": ["kind", "assignee"],
                "properties": {"kind": {"enum": ["confirm", "approve", "input"]}, "timeout_s": {"type": "integer"}},
            },
        ),
        when("type", "sub_workflow", {"required": ["ref"], "properties": {"ref": REF_AT}}),
        when("type", "parallel", {"required": ["branches"]}),
        when("type", "wait", {"required": ["seconds"]}),
    ],
)
WORKFLOW = obj(
    {
        "name": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"},
        "version": SEMVER,
        "input_schema": {"type": "object"},
        "output_schema": {"type": "object"},
        "trigger": {"enum": ["api", "schedule", "event"], "default": "api"},
        "budget": obj(
            {
                "max_steps": {"type": "integer", "minimum": 1, "maximum": 500},
                "max_llm_calls": {"type": "integer", "minimum": 0, "maximum": 50},
                "max_tokens": {"type": "integer", "minimum": 1},
                "timeout_s": {"type": "integer", "minimum": 1, "maximum": 604800},
            },
            ["max_steps", "max_llm_calls", "timeout_s"],
        ),
        "nodes": {"type": "array", "minItems": 1, "items": {"$ref": "#/$defs/node"}},
    },
    ["name", "version", "budget", "nodes"],
    extra=False,
)
WORKFLOW["$defs"] = {"node": NODE}

NULLSTR = {"type": ["string", "null"]}
POLICY = obj(
    {
        "principal": obj(
            {
                "identity_kind": {"enum": ["user", "app"]},
                "user_id": NULLSTR,
                "company_id": {"type": "string"},
                "clearance": {"type": "integer", "minimum": 1, "maximum": 4},
                "roles": {"type": "array", "items": {"type": "string"}},
                "client_id": NULLSTR,
                "agent_profile_id": NULLSTR,
            },
            ["identity_kind", "company_id", "clearance", "client_id"],
        ),
        "facts": obj(
            {
                "app_grant": {"type": "boolean"},
                "user_grant_required": {"type": "boolean"},
                "user_grant": {"type": ["boolean", "null"]},
                "within_ceiling": {"type": "boolean"},
                "scope_check": {
                    "type": "object",
                    "additionalProperties": obj({"org_id": NULLSTR, "in_scope": {"type": "boolean"}}, ["in_scope"]),
                },
            },
            ["app_grant", "within_ceiling", "scope_check"],
        ),
        "resource": obj(
            {
                "capability_id": CAP,
                "version": SEMVER,
                "classification": {"type": "integer", "minimum": 1, "maximum": 4},
                "risk": RISK,
                "release_status": {"enum": ["canary", "active", "deprecated", "suspended", "retired"]},
                "confirm_policy": CONFIRM,
                "app_identity_allowed": {"type": "boolean"},
                "transport_exception": {"type": "boolean"},
                "required_scopes": {"type": "array", "items": {"type": "string"}},
            },
            ["capability_id", "version", "classification", "risk", "release_status", "confirm_policy"],
        ),
        "context": obj(
            {
                "env": {"enum": ["test", "staging", "prod"]},
                "channel": {"enum": ["beta", "stable"]},
                "exposure": {"enum": ["rest", "mcp", "a2a", "internal"]},
                "time_utc": {"type": "string", "format": "date-time"},
            },
            ["env", "channel", "exposure"],
        ),
    },
    ["principal", "facts", "resource", "context"],
    extra=False,
)

DECL = obj(
    {
        "connector": {"type": "string"},
        "operation": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"},
        "mode": {"enum": ["read", "write"], "default": "read"},
        "request": obj(
            {
                "method": {"enum": ["GET", "POST", "PUT", "PATCH", "DELETE"]},
                "path": {"type": "string", "pattern": "^/"},
                "query": {"type": "object", "additionalProperties": {"type": "string"}},
                "headers": {"type": "object", "additionalProperties": {"type": "string"}},
                "body": {},
            },
            ["method", "path"],
            extra=False,
        ),
        "response": obj(
            {
                "success_when": {"type": "string"},
                "data": {"type": "string"},
                "fields": {"type": "object", "additionalProperties": {"type": "string"}},
                "next_cursor": {"type": "string"},
            },
            ["data"],
            extra=False,
        ),
        "errors": {
            "type": "array",
            "items": obj(
                {"when": {"type": "string"}, "map": {"type": "string", "pattern": "^[A-Z][A-Z_]+$"}}, ["when", "map"]
            ),
        },
    },
    ["connector", "operation", "request", "response"],
    extra=False,
)

CONN = obj(
    {
        "system_key": {"type": "string"},
        "env": {"enum": ["sandbox", "test", "staging", "prod"]},
        "base_url": {"type": "string", "pattern": "^https?://"},
        "scheme": {"enum": ["https", "http"]},
        "tls_mode": {"enum": ["system_ca", "custom_ca", "pinned", "insecure", None]},
        "tls_profile_id": NULLSTR,
        "tls_check_hostname": {"type": "boolean", "default": True},
        "pins": {
            "type": "array",
            "items": obj(
                {"kind": {"enum": ["cert", "spki"]}, "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"}},
                ["kind", "sha256"],
            ),
        },
        "pin_enforce_expiry": {"type": "boolean", "default": False},
        "block_on_fingerprint_change": {"type": "boolean", "default": False},
        "client_cert_ref": NULLSTR,
        "transport_exception_reason": NULLSTR,
        "transport_exception_expires_at": {"type": ["string", "null"], "format": "date-time"},
        "transport_approved_by": {"type": "array", "items": {"type": "string"}},
    },
    ["system_key", "env", "base_url", "scheme"],
    allOf=[
        when(
            "scheme",
            "http",
            {
                "required": ["transport_exception_reason", "transport_exception_expires_at"],
                "properties": {"tls_mode": {"const": None}, "base_url": {"pattern": "^http://"}},
            },
        ),
        when(
            "scheme",
            "https",
            {
                "required": ["tls_mode"],
                "properties": {
                    "tls_mode": {"enum": ["system_ca", "custom_ca", "pinned", "insecure"]},
                    "base_url": {"pattern": "^https://"},
                },
            },
        ),
        when("tls_mode", "pinned", {"required": ["pins"], "properties": {"pins": {"minItems": 1}}}),
        when("tls_mode", "custom_ca", {"required": ["tls_profile_id"]}),
        when("tls_mode", "insecure", {"required": ["transport_exception_reason", "transport_exception_expires_at"]}),
    ],
)

SPECS = {
    "tool-spec": ("Tool Spec（architecture.md 6.1）", TOOL),
    "workflow-dsl": ("Workflow DSL（architecture.md 6.3）", WORKFLOW),
    "policy-input": ("PDP 输入（architecture.md 4.4）", POLICY),
    "declarative-connector": ("声明式 REST Connector 映射（architecture.md 9.1）", DECL),
    "system-connection": ("system_connection（architecture.md 9.6、11.3）", CONN),
}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, (title, body) in SPECS.items():
        doc = {"$schema": D, "$id": f"{B}{name}.schema.json", "title": title, **body}
        (OUT / f"{name}.schema.json").write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(SPECS)} schemas")


if __name__ == "__main__":
    main()
