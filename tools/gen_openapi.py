"""Generate docs/api/{control-plane,data-plane}.openapi.yaml — the API contract (architecture.md 3, 4, 8, 9, 10).

The FastAPI apps must only expose paths declared here (checked by tests/test_contract.py).
Actions on a resource use a sub-path verb, e.g. POST /connections/{id}/test.
"""

from __future__ import annotations

import pathlib
from typing import Any

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "api"
V = "/api/v1"


def ref(name: str) -> dict[str, str]:
    return {"$ref": f"#/components/schemas/{name}"}


def body(schema: dict[str, Any]) -> dict[str, Any]:
    return {"required": True, "content": {"application/json": {"schema": schema}}}


def ok(schema: dict[str, Any] | None, code: str = "200", desc: str = "OK") -> dict[str, Any]:
    r: dict[str, Any] = {"description": desc}
    if schema is not None:
        r["content"] = {"application/json": {"schema": schema}}
    return {code: r, "default": {"$ref": "#/components/responses/Problem"}}


def page(item: str) -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["items", "has_more"],
        "properties": {
            "items": {"type": "array", "items": ref(item)},
            "next_cursor": {"type": ["string", "null"]},
            "has_more": {"type": "boolean"},
        },
    }


def op(
    op_id: str,
    tag: str,
    summary: str,
    *,
    req: dict[str, Any] | None = None,
    res: dict[str, Any] | None = None,
    code: str = "200",
    params: list[str] | None = None,
    security: list[dict[str, list[str]]] | None = None,
    idem: bool = False,
) -> dict[str, Any]:
    o: dict[str, Any] = {"operationId": op_id, "tags": [tag], "summary": summary, "responses": ok(res, code)}
    ps = [{"$ref": f"#/components/parameters/{p}"} for p in (params or [])]
    if idem:
        ps.append({"$ref": "#/components/parameters/IdempotencyKey"})
    if ps:
        o["parameters"] = ps
    if req is not None:
        o["requestBody"] = body(req)
    if security is not None:
        o["security"] = security
    return o


S = {"type": "string"}
SN = {"type": ["string", "null"]}
DT = {"type": "string", "format": "date-time"}
ID = {"type": "string", "description": "UUIDv7"}
B = {"type": "boolean"}
INT = {"type": "integer"}
CLEAR = {"type": "integer", "minimum": 1, "maximum": 4}


def o(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    d: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        d["required"] = required
    return d


COMMON_SCHEMAS: dict[str, Any] = {
    "Problem": o(
        {
            "type": S,
            "title": S,
            "status": INT,
            "code": S,
            "detail": S,
            "instance": S,
            "trace_id": S,
            "errors": {"type": "array", "items": o({"field": S, "code": S, "message": S})},
        },
        ["type", "title", "status", "code"],
    ),
    "Health": o(
        {
            "status": {"enum": ["ok", "degraded"]},
            "service": S,
            "version": S,
            "checks": {"type": "object", "additionalProperties": S},
        },
        ["status", "service"],
    ),
}
COMMON_COMPONENTS: dict[str, Any] = {
    "responses": {
        "Problem": {
            "description": "RFC 9457 Problem Details",
            "content": {"application/problem+json": {"schema": ref("Problem")}},
        }
    },
    "parameters": {
        "Id": {"name": "id", "in": "path", "required": True, "schema": S},
        "Limit": {
            "name": "limit",
            "in": "query",
            "schema": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
        },
        "Cursor": {"name": "cursor", "in": "query", "schema": S},
        "IdempotencyKey": {
            "name": "Idempotency-Key",
            "in": "header",
            "schema": {"type": "string", "minLength": 1, "maxLength": 128},
        },
        "AfterSeq": {"name": "after_seq", "in": "query", "schema": {"type": "integer", "minimum": 0, "default": 0}},
        "WaitS": {
            "name": "wait_s",
            "in": "query",
            "description": "长轮询等待秒数（0 到 25）",
            "schema": {"type": "number", "minimum": 0, "maximum": 25, "default": 0},
        },
    },
    "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}},
}
PUBLIC: list[dict[str, list[str]]] = []
LIST = ["Limit", "Cursor"]


def control_plane() -> dict[str, Any]:
    sch = dict(COMMON_SCHEMAS)
    sch.update(
        {
            "LoginRequest": o(
                {"login_name": S, "password": {"type": "string", "format": "password"}, "totp": SN},
                ["login_name", "password"],
            ),
            "Session": o(
                {"session_token": S, "expires_at": DT, "user": ref("User")}, ["session_token", "expires_at", "user"]
            ),
            "OrgUnit": o(
                {
                    "id": ID,
                    "type": {"enum": ["group", "company", "department", "team"]},
                    "code": S,
                    "name": S,
                    "parent_id": SN,
                    "company_id": S,
                    "default_clearance": {"type": ["integer", "null"], "minimum": 1, "maximum": 4},
                    "timezone": SN,
                    "locale": SN,
                    "status": {"enum": ["active", "disabled"]},
                    "valid_from": DT,
                    "valid_to": {"type": ["string", "null"], "format": "date-time"},
                },
                ["id", "type", "code", "name", "company_id", "status"],
            ),
            "OrgUnitCreate": o(
                {
                    "type": {"enum": ["company", "department", "team"]},
                    "code": S,
                    "name": S,
                    "parent_id": S,
                    "default_clearance": {"type": ["integer", "null"], "minimum": 1, "maximum": 4},
                    "timezone": SN,
                    "locale": SN,
                },
                ["type", "code", "name", "parent_id"],
            ),
            "OrgUnitMove": o({"new_parent_id": S}, ["new_parent_id"]),
            "User": o(
                {
                    "id": ID,
                    "login_name": SN,
                    "display_name": S,
                    "employee_no": SN,
                    "email": SN,
                    "home_org_id": S,
                    "login_enabled": B,
                    "status": {"enum": ["active", "disabled", "left"]},
                    "effective_clearance": CLEAR,
                    "totp_provisioning_uri": {"type": ["string", "null"], "description": "仅创建可登录用户时返回一次"},
                    "timezone": SN,
                    "locale": SN,
                },
                ["id", "display_name", "home_org_id", "login_enabled", "status"],
            ),
            "UserCreate": o(
                {
                    "login_name": SN,
                    "display_name": S,
                    "employee_no": SN,
                    "email": SN,
                    "home_org_id": S,
                    "login_enabled": B,
                    "password": SN,
                },
                ["display_name", "home_org_id"],
            ),
            "ImportRequest": o({"format": {"enum": ["csv"]}, "content": S, "dry_run": B}, ["format", "content"]),
            "ImportResult": o(
                {
                    "batch_id": S,
                    "dry_run": B,
                    "created": INT,
                    "updated": INT,
                    "unchanged": INT,
                    "errors": {"type": "array", "items": o({"line": INT, "message": S})},
                }
            ),
            "RoleAssignment": o(
                {
                    "id": ID,
                    "user_id": S,
                    "role": {
                        "enum": [
                            "platform_admin",
                            "asset_developer",
                            "data_owner",
                            "business_owner",
                            "company_admin",
                            "auditor",
                        ]
                    },
                    "scope_org_id": S,
                    "expires_at": {"type": ["string", "null"], "format": "date-time"},
                },
                ["user_id", "role", "scope_org_id"],
            ),
            "ClearanceRequest": o(
                {
                    "id": ID,
                    "user_id": S,
                    "requested_level": CLEAR,
                    "reason": S,
                    "expires_at": DT,
                    "status": {"enum": ["pending", "approved", "rejected"]},
                },
                ["user_id", "requested_level", "reason", "expires_at"],
            ),
            "Decision": o({"decision": {"enum": ["approve", "reject"]}, "comment": S}, ["decision"]),
            "Note": o({"note": {"type": "string", "maxLength": 1000}}),
            "Quota": o(
                {"daily_calls": {"type": "integer", "minimum": 1}, "daily_writes": {"type": "integer", "minimum": 1}}
            ),
            "RiskRule": o(
                {
                    "code": S,
                    "name": S,
                    "description": S,
                    "source": {"enum": ["audit", "domain"]},
                    "params": {"type": "object"},
                    "action": {"enum": ["alert", "suspend_asset"]},
                    "severity": {"enum": ["low", "medium", "high"]},
                    "enabled": B,
                },
                ["code", "params", "action"],
            ),
            "RiskRulePatch": o(
                {
                    "params": {"type": "object"},
                    "enabled": B,
                    "action": {"enum": ["alert", "suspend_asset"]},
                    "severity": {"enum": ["low", "medium", "high"]},
                }
            ),
            "RiskEvent": o(
                {
                    "id": S,
                    "rule_code": S,
                    "rule_name": S,
                    "company_id": SN,
                    "group_key": {"type": "object"},
                    "observed": {"type": "number"},
                    "threshold": {"type": "number"},
                    "severity": S,
                    "action": S,
                    "action_result": {"type": "object"},
                    "status": {"enum": ["open", "acknowledged", "resume_requested", "resolved"]},
                },
                ["id", "rule_code", "status"],
            ),
            "TodoDecision": o(
                {"decision": {"enum": ["approve", "reject"]}, "comment": S, "data": {"type": "object"}}, ["decision"]
            ),
            "Consumer": o(
                {
                    "id": ID,
                    "client_id": S,
                    "company_id": S,
                    "clearance_cap": CLEAR,
                    "bind_policy": {"enum": ["strict", "by_employee_no", "app_only"]},
                    "status": S,
                    "contact": S,
                },
                ["client_id", "company_id", "clearance_cap", "bind_policy"],
            ),
            "CredentialCreate": o(
                {
                    "kind": {"enum": ["static_token", "private_key_jwt"]},
                    "expires_at": DT,
                    "ip_binding": {"type": "array", "items": S},
                },
                ["kind"],
            ),
            "CredentialIssued": o(
                {
                    "id": ID,
                    "kind": S,
                    "token": {"type": ["string", "null"], "description": "仅创建时返回一次"},
                    "expires_at": DT,
                }
            ),
            "Jwk": o({"kid": S, "kty": S, "crv": S, "x": S, "y": S, "n": S, "e": S, "alg": S}, ["kid", "kty"]),
            "UserBinding": o(
                {"id": ID, "consumer_id": S, "external_subject": S, "user_id": S, "expires_at": DT},
                ["consumer_id", "external_subject", "user_id"],
            ),
            "AssertionDebugRequest": o({"consumer_id": S, "assertion": S}, ["consumer_id", "assertion"]),
            "AssertionDebugResult": o(
                {
                    "signature_valid": B,
                    "claims_valid": B,
                    "binding": SN,
                    "errors": {"type": "array", "items": S},
                    "principal": {"type": ["object", "null"]},
                }
            ),
            "Asset": o(
                {
                    "id": ID,
                    "type": {
                        "enum": ["tool", "skill", "workflow", "bundle", "prompt", "model_profile", "connector_package"]
                    },
                    "name": S,
                    "owner_org_id": S,
                    "owner_user_id": S,
                    "classification": CLEAR,
                },
                ["type", "name", "owner_org_id", "classification"],
            ),
            "AssetVersion": o(
                {
                    "id": ID,
                    "asset_id": S,
                    "semver": S,
                    "content_hash": S,
                    "spec": {"type": "object"},
                    "state": {
                        "enum": [
                            "draft",
                            "testing",
                            "in_review",
                            "approved",
                            "canary",
                            "active",
                            "deprecated",
                            "retired",
                            "suspended",
                        ]
                    },
                },
                ["semver", "spec"],
            ),
            "Release": o(
                {
                    "id": ID,
                    "version_id": S,
                    "env": {"enum": ["test", "staging", "prod"]},
                    "scope": {"type": "object"},
                    "channel": {"enum": ["beta", "stable"]},
                    "exposure": {"type": "array", "items": {"enum": ["rest", "mcp", "a2a"]}},
                    "status": S,
                },
                ["version_id", "env", "scope", "channel"],
            ),
            "GrantCeiling": o(
                {
                    "id": ID,
                    "company_id": S,
                    "asset_set": {"type": "object"},
                    "max_classification": CLEAR,
                    "quota": {"type": "object"},
                },
                ["company_id", "asset_set", "max_classification"],
            ),
            "Grant": o(
                {
                    "id": ID,
                    "asset_id": S,
                    "grantee_type": {"enum": ["consumer", "agent_profile", "org_unit", "user"]},
                    "grantee_id": S,
                    "env": S,
                    "expires_at": DT,
                    "ceiling_id": S,
                },
                ["asset_id", "grantee_type", "grantee_id", "env", "expires_at"],
            ),
            "SystemConnection": {"$ref": "../specs/system-connection.schema.json"},
            "ConnectionTestResult": o(
                {
                    "connection_id": S,
                    "resolved_ip": SN,
                    "allowlisted": B,
                    "tls_version": SN,
                    "chain": {
                        "type": "array",
                        "items": o({"subject": S, "issuer": S, "not_before": DT, "not_after": DT}),
                    },
                    "fingerprint_cert_sha256": SN,
                    "fingerprint_spki_sha256": SN,
                    "hostname_match": {"type": ["boolean", "null"]},
                    "verdict": {"enum": ["pass", "fail"]},
                    "error_code": SN,
                    "detail": SN,
                },
                ["connection_id", "verdict"],
            ),
            "ConfigDraft": o(
                {
                    "tls_mode": {"enum": ["custom_ca", "pinned"]},
                    "pins": {"type": "array", "items": {"type": "object"}},
                    "ca_pem": SN,
                    "approval_id": S,
                }
            ),
            "Approval": o(
                {
                    "id": ID,
                    "kind": S,
                    "subject_ref": S,
                    "status": S,
                    "steps": {"type": "array", "items": {"type": "object"}},
                }
            ),
            "AuditRecord": o(
                {
                    "id": ID,
                    "occurred_at": DT,
                    "actor": {"type": "object"},
                    "action": S,
                    "target": S,
                    "decision": SN,
                    "reason": SN,
                    "trace_id": SN,
                    "shard_id": S,
                    "seq": INT,
                    "hash": S,
                }
            ),
            "Snapshot": o(
                {
                    "kind": {"enum": ["iam", "release"]},
                    "seq": INT,
                    "full": B,
                    "sha256": S,
                    "signature": S,
                    "payload_url": S,
                },
                ["kind", "seq", "full", "sha256", "signature"],
            ),
        }
    )
    A = "/api/v1"
    paths: dict[str, Any] = {
        "/healthz": {"get": op("cpHealth", "ops", "存活检查", res=ref("Health"), security=PUBLIC)},
        f"{A}/auth/login": {
            "post": op(
                "login", "auth", "本地登录（密码 + TOTP）", req=ref("LoginRequest"), res=ref("Session"), security=PUBLIC
            )
        },
        f"{A}/auth/logout": {"post": op("logout", "auth", "注销会话", code="204")},
        f"{A}/me": {"get": op("me", "auth", "当前用户", res=ref("User"))},
        f"{A}/orgs": {
            "get": op("listOrgs", "iam", "组织列表（按生效节点范围）", res=page("OrgUnit"), params=LIST),
            "post": op(
                "createOrg", "iam", "新建组织节点", req=ref("OrgUnitCreate"), res=ref("OrgUnit"), code="201", idem=True
            ),
        },
        f"{A}/orgs/{{id}}": {
            "get": op("getOrg", "iam", "组织详情", res=ref("OrgUnit"), params=["Id"]),
            "patch": op("updateOrg", "iam", "修改组织", req=ref("OrgUnitCreate"), res=ref("OrgUnit"), params=["Id"]),
        },
        f"{A}/orgs/{{id}}/move": {
            "post": op("moveOrg", "iam", "迁移组织节点", req=ref("OrgUnitMove"), res=ref("OrgUnit"), params=["Id"])
        },
        f"{A}/orgs/{{id}}/disable": {
            "post": op("disableOrg", "iam", "停用组织节点", res=ref("OrgUnit"), params=["Id"])
        },
        f"{A}/users": {
            "get": op("listUsers", "iam", "用户列表", res=page("User"), params=LIST),
            "post": op("createUser", "iam", "新建用户", req=ref("UserCreate"), res=ref("User"), code="201", idem=True),
        },
        f"{A}/users/import/{{id}}/rollback": {
            "post": op(
                "rollbackImport",
                "iam",
                "按批次回滚导入（未使用的删除，已使用的停用）",
                res={"type": "object"},
                params=["Id"],
            )
        },
        f"{A}/users/import": {
            "post": op(
                "importUsers", "iam", "批量导入用户（支持 dry_run）", req=ref("ImportRequest"), res=ref("ImportResult")
            )
        },
        f"{A}/users/{{id}}": {
            "get": op("getUser", "iam", "用户详情", res=ref("User"), params=["Id"]),
            "patch": op("updateUser", "iam", "修改用户", req=ref("UserCreate"), res=ref("User"), params=["Id"]),
        },
        f"{A}/users/{{id}}/roles": {
            "post": op(
                "assignRole",
                "iam",
                "分配角色",
                req=ref("RoleAssignment"),
                res=ref("RoleAssignment"),
                code="201",
                params=["Id"],
            )
        },
        f"{A}/clearance-requests": {
            "post": op(
                "requestClearance",
                "iam",
                "申请提升密级",
                req=ref("ClearanceRequest"),
                res=ref("ClearanceRequest"),
                code="201",
            )
        },
        f"{A}/clearance-requests/{{id}}/decide": {
            "post": op(
                "decideClearance",
                "iam",
                "审批密级申请",
                req=ref("Decision"),
                res=ref("ClearanceRequest"),
                params=["Id"],
            )
        },
        f"{A}/consumers": {
            "get": op("listConsumers", "iam", "消费方列表", res=page("Consumer"), params=LIST),
            "post": op(
                "createConsumer", "iam", "注册消费方", req=ref("Consumer"), res=ref("Consumer"), code="201", idem=True
            ),
        },
        f"{A}/consumers/{{id}}/credentials": {
            "post": op(
                "issueCredential",
                "iam",
                "签发消费方凭据",
                req=ref("CredentialCreate"),
                res=ref("CredentialIssued"),
                code="201",
                params=["Id"],
            )
        },
        f"{A}/credentials/{{id}}/revoke": {
            "post": op("revokeCredential", "iam", "吊销凭据", code="204", params=["Id"])
        },
        f"{A}/consumers/{{id}}/jwks": {
            "post": op(
                "addConsumerJwk",
                "iam",
                "登记用户断言验签公钥",
                req=ref("Jwk"),
                res=ref("Jwk"),
                code="201",
                params=["Id"],
            )
        },
        f"{A}/consumers/{{id}}/bindings": {
            "get": op("listBindings", "iam", "用户绑定列表", res=page("UserBinding"), params=["Id", *LIST]),
            "post": op(
                "createBinding",
                "iam",
                "新建用户绑定",
                req=ref("UserBinding"),
                res=ref("UserBinding"),
                code="201",
                params=["Id"],
            ),
        },
        f"{A}/assertion-debug": {
            "post": op(
                "debugAssertion",
                "iam",
                "断言调试器（不落日志）",
                req=ref("AssertionDebugRequest"),
                res=ref("AssertionDebugResult"),
            )
        },
        f"{A}/assets": {
            "get": op("listAssets", "registry", "资产列表", res=page("Asset"), params=LIST),
            "post": op(
                "createAsset", "registry", "新建资产", req=ref("Asset"), res=ref("Asset"), code="201", idem=True
            ),
        },
        f"{A}/versions/{{id}}/submit": {
            "post": op("submitVersion", "release", "提交门禁", res=ref("AssetVersion"), params=["Id"])
        },
        f"{A}/versions/{{id}}/test-cases": {
            "post": op(
                "addTestCase",
                "release",
                "添加门禁用例（功能 / 权限）",
                req={"type": "object"},
                res={"type": "object"},
                code="201",
                params=["Id"],
            )
        },
        f"{A}/versions/{{id}}/acceptance": {
            "put": op(
                "setAcceptance",
                "release",
                "设置验收单模板（G5）",
                req={"type": "object"},
                res={"type": "object"},
                params=["Id"],
            )
        },
        f"{A}/versions/{{id}}/run-tests": {
            "post": op("runTests", "release", "Test Lab 执行 G1 / G2", res={"type": "object"}, params=["Id"])
        },
        f"{A}/versions/{{id}}/gates": {
            "get": op("gateHistory", "release", "门禁记录", res={"type": "object"}, params=["Id"])
        },
        f"{A}/versions/{{id}}/canary": {
            "post": op(
                "startCanary",
                "release",
                "G7 灰度发布",
                req={"type": "object"},
                res={"type": "object"},
                code="201",
                params=["Id"],
            )
        },
        f"{A}/versions/{{id}}/promote": {
            "post": op(
                "promoteVersion",
                "release",
                "G7 指标达标后全量（G8）",
                req={"type": "object"},
                res={"type": "object"},
                params=["Id"],
            )
        },
        f"{A}/approval-delegates": {
            "post": op(
                "addApprovalDelegate",
                "approval",
                "设置审批代理人",
                req={"type": "object"},
                res={"type": "object"},
                code="201",
            )
        },
        f"{A}/access-reviews": {
            "post": op(
                "openAccessReview",
                "grant",
                "发起授权复核活动",
                req={"type": "object"},
                res={"type": "object"},
                code="201",
            )
        },
        f"{A}/access-reviews/{{id}}/close": {
            "post": op(
                "closeAccessReview", "grant", "关闭复核（未确认的授权撤销）", res={"type": "object"}, params=["Id"]
            )
        },
        f"{A}/assets/{{id}}/versions": {
            "get": op("listVersions", "registry", "资产的版本列表", res={"type": "object"}, params=["Id"]),
            "post": op(
                "createVersion",
                "registry",
                "新建资产版本",
                req=ref("AssetVersion"),
                res=ref("AssetVersion"),
                code="201",
                params=["Id"],
            ),
        },
        f"{A}/versions/{{id}}": {
            "get": op("getVersion", "registry", "版本详情（含用例与验收单）", res={"type": "object"}, params=["Id"])
        },
        f"{A}/grant-ceilings": {
            "get": op("listCeilings", "grant", "授权上限列表", res={"type": "object"}),
            "post": op(
                "createCeiling", "grant", "设置授权上限", req=ref("GrantCeiling"), res=ref("GrantCeiling"), code="201"
            ),
        },
        f"{A}/usage-requests": {
            "get": op("listUsageRequests", "grant", "使用申请列表", res={"type": "object"}),
            "post": op(
                "requestUsage",
                "grant",
                "使用申请（上限内，由本公司管理员审批）",
                req={"type": "object"},
                res={"type": "object"},
                code="201",
            ),
        },
        f"{A}/audit-logs/verify": {"post": op("verifyAudit", "audit", "校验审计哈希链与锚点", res={"type": "object"})},
        f"{A}/releases": {
            "post": op(
                "createRelease", "release", "发布", req=ref("Release"), res=ref("Release"), code="201", idem=True
            )
        },
        f"{A}/releases/{{id}}/rollback": {
            "post": op("rollbackRelease", "release", "一键回滚", res=ref("Release"), params=["Id"])
        },
        f"{A}/grants": {
            "get": op("listGrants", "grant", "授权列表", res=page("Grant"), params=LIST),
            "post": op("createGrant", "grant", "下放授权", req=ref("Grant"), res=ref("Grant"), code="201", idem=True),
        },
        f"{A}/connections": {
            "get": op("listConnections", "connection", "连接列表", res=page("SystemConnection"), params=LIST),
            "post": op(
                "createConnection",
                "connection",
                "新建系统连接",
                req=ref("SystemConnection"),
                res=ref("SystemConnection"),
                code="201",
            ),
        },
        f"{A}/connections/{{id}}/approve-exception": {
            "post": op(
                "approveTransportException",
                "connection",
                "审批传输例外（生产需两名数据 Owner）",
                res={"type": "object"},
                params=["Id"],
            )
        },
        f"{A}/connector-instances": {
            "post": op(
                "createConnectorInstance",
                "connection",
                "登记 Connector 实例",
                req={"type": "object"},
                res={"type": "object"},
                code="201",
            )
        },
        f"{A}/connections/{{id}}/test": {
            "post": op(
                "testConnection", "connection", "连接测试（真实握手）", res=ref("ConnectionTestResult"), params=["Id"]
            )
        },
        f"{A}/connections/{{id}}/trust-draft": {
            "post": op(
                "draftTrust",
                "connection",
                "从连接测试生成 pinned / custom_ca 草稿",
                req=ref("ConfigDraft"),
                res=ref("ConfigDraft"),
                params=["Id"],
            )
        },
        f"{A}/approvals": {"get": op("listApprovals", "approval", "我的审批待办", res=page("Approval"), params=LIST)},
        f"{A}/approvals/{{id}}/decide": {
            "post": op("decideApproval", "approval", "审批", req=ref("Decision"), res=ref("Approval"), params=["Id"])
        },
        f"{A}/risk-rules": {"get": op("listRiskRules", "risk", "风险规则", res=page("RiskRule"))},
        f"{A}/risk-rules/{{code}}": {
            "put": {
                **op(
                    "updateRiskRule",
                    "risk",
                    "调整风险规则（平台管理员）",
                    req=ref("RiskRulePatch"),
                    res=ref("RiskRule"),
                ),
                "parameters": [{"name": "code", "in": "path", "required": True, "schema": S}],
            }
        },
        f"{A}/risk-events": {
            "get": {
                **op("listRiskEvents", "risk", "风险事件（按公司隔离）", res=page("RiskEvent")),
                "parameters": [
                    {
                        "name": "status",
                        "in": "query",
                        "schema": {"enum": ["open", "acknowledged", "resume_requested", "resolved"]},
                    },
                    {"name": "limit", "in": "query", "schema": {"type": "integer", "minimum": 1, "maximum": 500}},
                ],
            }
        },
        f"{A}/risk-events/{{id}}/acknowledge": {
            "post": op(
                "acknowledgeRiskEvent",
                "risk",
                "确认风险事件（告警类关闭，熔断类只标记已确认）",
                req=ref("Note"),
                res=ref("RiskEvent"),
                params=["Id"],
            )
        },
        f"{A}/risk-events/{{id}}/request-resume": {
            "post": op(
                "requestRiskResume",
                "risk",
                "熔断恢复：写明修复情况，由另一位数据 Owner 复核（审批 kind=resume）",
                req=ref("Note"),
                res={"type": "object"},
                params=["Id"],
            )
        },
        f"{A}/risk-scan": {
            "post": op("runRiskScan", "risk", "手动触发一次风险扫描（平台管理员）", res={"type": "object"})
        },
        f"{A}/usage": {
            "get": {
                **op(
                    "usageReport",
                    "audit",
                    "计量：按日、公司、消费方、能力汇总调用、失败与写操作",
                    res={"type": "object"},
                ),
                "parameters": [
                    {"name": "days", "in": "query", "schema": {"type": "integer", "minimum": 1, "maximum": 90}}
                ],
            }
        },
        f"{A}/consumers/{{id}}/quota": {
            "put": op(
                "setConsumerQuota",
                "iam",
                "消费方每日配额（daily_calls、daily_writes；空对象表示不限）",
                req=ref("Quota"),
                res={"type": "object"},
                params=["Id"],
            )
        },
        f"{A}/consumers/{{id}}/webhooks": {
            "get": op("listWebhooks", "webhook", "消费方的 webhook 端点", res={"type": "object"}, params=["Id"]),
            "post": op(
                "createWebhook",
                "webhook",
                "登记 webhook 订阅（返回一次性明文密钥）",
                req=o({"events": {"type": "array", "items": S}, "url": S}, ["events", "url"]),
                res={"type": "object"},
                code="201",
                params=["Id"],
            ),
        },
        f"{A}/webhooks/{{id}}/rotate-secret": {
            "post": op(
                "rotateWebhookSecret",
                "webhook",
                "轮换 webhook 密钥（旧密钥立即失效，新密钥只返回一次）",
                res={"type": "object"},
                params=["Id"],
            )
        },
        f"{A}/webhooks/{{id}}/status": {
            "put": op(
                "setWebhookStatus",
                "webhook",
                "启用或停用 webhook 端点",
                req=o({"status": {"enum": ["active", "disabled"]}}, ["status"]),
                res={"type": "object"},
                params=["Id"],
            )
        },
        f"{A}/webhooks/{{id}}/deliveries": {
            "get": op(
                "listWebhookDeliveries",
                "webhook",
                "webhook 投递记录",
                res={"type": "object"},
                params=["Id"],
            )
        },
        f"{A}/webhooks/process": {
            "post": op(
                "processWebhooks",
                "webhook",
                "手动触发到期 webhook 投递",
                res={"type": "object"},
            )
        },
        f"{A}/console/catalog": {
            "get": op(
                "consoleCatalog",
                "catalog",
                "控制台资产目录（最新发布快照中的 Tool 与套件）",
                res={"type": "object"},
            )
        },
        f"{A}/versions/{{id}}/skill-package": {
            "post": {
                **op(
                    "uploadSkillPackage",
                    "skill",
                    "上传 Skill zip 包（签名入库，draft 阶段）",
                    res={"type": "object"},
                    code="201",
                    params=["Id"],
                ),
                "requestBody": {
                    "required": True,
                    "content": {
                        "multipart/form-data": {
                            "schema": o({"file": {"type": "string", "format": "binary"}}, ["file"]),
                        }
                    },
                },
            }
        },
        f"{A}/skills": {
            "get": {
                **op("listSkills", "skill", "Skill 目录（含撤回标记）", res={"type": "object"}),
                "parameters": [
                    {"name": "q", "in": "query", "schema": S},
                    {"name": "channel", "in": "query", "schema": {"enum": ["stable", "beta"]}},
                    {"name": "include_retracted", "in": "query", "schema": {"type": "boolean"}},
                ],
            }
        },
        f"{A}/skills/{{name}}/versions/{{version}}/manifest": {
            "get": {
                **op("getSkillManifest", "skill", "Skill 文件清单与签名", res={"type": "object"}),
                "parameters": [
                    {"name": "name", "in": "path", "required": True, "schema": S},
                    {"name": "version", "in": "path", "required": True, "schema": S},
                ],
            }
        },
        f"{A}/skills/{{name}}/versions/{{version}}/download": {
            "get": {
                **op("downloadSkill", "skill", "下载 Skill zip", res={"type": "string", "format": "binary"}),
                "parameters": [
                    {"name": "name", "in": "path", "required": True, "schema": S},
                    {"name": "version", "in": "path", "required": True, "schema": S},
                ],
            }
        },
        f"{A}/skills/install-check": {
            "post": op(
                "skillInstallCheck",
                "skill",
                "安装前检查 Tool 授权与撤回状态",
                req=o(
                    {
                        "consumer_id": S,
                        "name": S,
                        "version": S,
                        "env": {"enum": ["test", "staging", "prod"]},
                    },
                    ["consumer_id", "name", "version"],
                ),
                res={"type": "object"},
            )
        },
        f"{A}/grant-ceilings/{{id}}/quota": {
            "put": op(
                "setCeilingQuota",
                "grant",
                "授权上限内全公司共享的每日调用额度（daily_calls）",
                req=ref("Quota"),
                res={"type": "object"},
                params=["Id"],
            )
        },
        f"{A}/todo": {
            "get": op(
                "listTodo",
                "approval",
                "我的待办：可处理的调用确认（审批 / 本人确认）与 Workflow 人工任务",
                res={
                    "type": "object",
                    "properties": {
                        "confirmations": {"type": "array", "items": {"type": "object"}},
                        "tasks": {"type": "array", "items": {"type": "object"}},
                    },
                },
            )
        },
        f"{A}/todo/confirmations/{{id}}/decide": {
            "post": op(
                "decideConfirmation",
                "approval",
                "处理调用确认（approval 需具备 approver_role 且不是被代表用户；user_confirm 只能本人）",
                req=ref("TodoDecision"),
                res={"type": "object"},
                params=["Id"],
            )
        },
        f"{A}/todo/tasks/{{id}}/decide": {
            "post": op(
                "decideTask",
                "approval",
                "处理 Workflow 人工任务（确认、审批、补录）",
                req=ref("TodoDecision"),
                res={"type": "object"},
                params=["Id"],
            )
        },
        f"{A}/runs/{{id}}": {
            "get": op("getRunConsole", "approval", "控制台查看 Workflow Run", res={"type": "object"}, params=["Id"])
        },
        f"{A}/audit-logs": {
            "get": {
                **op("searchAudit", "audit", "审计检索（按时间倒序；分公司管理员只看本公司）", res=page("AuditRecord")),
                "parameters": [
                    {"name": "kind", "in": "query", "schema": S},
                    {"name": "trace_id", "in": "query", "schema": S},
                    {"name": "since", "in": "query", "schema": {"type": "string", "format": "date-time"}},
                    {"name": "until", "in": "query", "schema": {"type": "string", "format": "date-time"}},
                    {
                        "name": "limit",
                        "in": "query",
                        "schema": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
                    },
                ],
            }
        },
        f"{A}/internal/events": {
            "post": op(
                "ingestDataPlaneEvents",
                "snapshot",
                "数据面事件回传（服务凭据）",
                req={"type": "object"},
                res={"type": "object"},
            )
        },
        f"{A}/snapshots/iam": {
            "get": op(
                "pullIamSnapshot", "snapshot", "数据面拉取 IAM 快照", res=ref("Snapshot"), params=["AfterSeq", "WaitS"]
            )
        },
        f"{A}/snapshots/release": {
            "get": op(
                "pullReleaseSnapshot",
                "snapshot",
                "数据面拉取发布快照",
                res=ref("Snapshot"),
                params=["AfterSeq", "WaitS"],
            )
        },
    }
    return doc("AIP Control Plane API", "控制面：IAM、资产、发布、授权、连接、审批、审计", paths, sch)


def data_plane() -> dict[str, Any]:
    sch = dict(COMMON_SCHEMAS)
    sch.update(
        {
            "TokenRequest": {
                "type": "object",
                "required": ["grant_type"],
                "properties": {
                    "grant_type": {"enum": ["client_credentials", "urn:ietf:params:oauth:grant-type:jwt-bearer"]},
                    "assertion": S,
                    "client_id": S,
                    "client_assertion_type": S,
                    "client_assertion": S,
                    "resource": S,
                    "scope": S,
                },
            },
            "TokenResponse": o(
                {"access_token": S, "token_type": {"const": "Bearer"}, "expires_in": INT, "scope": S},
                ["access_token", "token_type", "expires_in"],
            ),
            "ProtectedResourceMetadata": o(
                {
                    "resource": S,
                    "authorization_servers": {"type": "array", "items": S},
                    "bearer_methods_supported": {"type": "array", "items": S},
                },
                ["resource", "authorization_servers"],
            ),
            "AuthorizationServerMetadata": o(
                {
                    "issuer": S,
                    "token_endpoint": S,
                    "jwks_uri": S,
                    "grant_types_supported": {"type": "array", "items": S},
                },
                ["issuer", "token_endpoint"],
            ),
            "Jwks": o({"keys": {"type": "array", "items": {"type": "object"}}}, ["keys"]),
            "CatalogEntry": o(
                {
                    "capability_id": S,
                    "version": S,
                    "title": S,
                    "description": S,
                    "risk_level": S,
                    "input_schema": {"type": "object"},
                    "output_schema": {"type": "object"},
                    "bundles": {"type": "array", "items": S, "description": "所属套件 name 列表"},
                },
                ["capability_id", "version"],
            ),
            "InvokeRequest": o(
                {"args": {"type": "object"}, "version": SN, "channel": {"enum": ["beta", "stable"]}}, ["args"]
            ),
            "ExecutionResult": o(
                {
                    "execution_id": S,
                    "status": {
                        "enum": [
                            "succeeded",
                            "failed",
                            "unknown",
                            "pending_confirm",
                            "running",
                            "cancelled",
                            "compensated",
                        ]
                    },
                    "data": {},
                    "page": o({"next_cursor": SN, "truncated": B}),
                    "error": {"type": ["object", "null"], "properties": {"code": S, "message": S, "retryable": B}},
                    "next_action": {
                        "type": ["object", "null"],
                        "properties": {
                            "type": {
                                "enum": [
                                    "refresh_auth",
                                    "fix_args",
                                    "request_permission",
                                    "confirm",
                                    "wait",
                                    "reconcile",
                                    "ask_human",
                                ]
                            },
                            "message": S,
                            "confirmation_id": SN,
                            "retry_after_s": {"type": ["integer", "null"]},
                        },
                    },
                    "trace_id": S,
                    "run_id": {"type": "string", "description": "Workflow 能力：与 execution_id 相同"},
                },
                ["execution_id", "status", "trace_id"],
            ),
            "Run": o(
                {
                    "run_id": S,
                    "status": {
                        "enum": ["running", "waiting", "succeeded", "failed", "compensated", "rejected", "cancelled"]
                    },
                    "result": {},
                    "error": {"type": ["object", "null"]},
                    "tainted": B,
                    "pending_tasks": {"type": "array", "items": {"type": "object"}},
                    "steps": {"type": "array", "items": {"type": "object"}},
                    "trace_id": S,
                },
                ["run_id", "status"],
            ),
            "ConfirmRequest": o(
                {
                    "assertion": {
                        "type": "string",
                        "description": "被代表用户的新断言（jti 未用过、iat 不早于确认创建）",
                    },
                    "comment": S,
                    "args": {"type": "object", "description": "可选：声明所确认的参数，必须与冻结内容一致"},
                }
            ),
            "InternalDecision": o(
                {
                    "decision": {"enum": ["approve", "reject"]},
                    "user_id": S,
                    "comment": S,
                    "data": {"type": "object"},
                },
                ["decision", "user_id"],
            ),
        }
    )
    paths: dict[str, Any] = {
        "/healthz": {"get": op("dpHealth", "ops", "存活检查", res=ref("Health"), security=PUBLIC)},
        "/readyz": {"get": op("dpReady", "ops", "就绪检查（快照与策略已加载）", res=ref("Health"), security=PUBLIC)},
        "/.well-known/oauth-protected-resource": {
            "get": op(
                "protectedResourceMetadata", "oauth", "RFC 9728", res=ref("ProtectedResourceMetadata"), security=PUBLIC
            )
        },
        "/.well-known/oauth-authorization-server": {
            "get": op(
                "authorizationServerMetadata",
                "oauth",
                "RFC 8414",
                res=ref("AuthorizationServerMetadata"),
                security=PUBLIC,
            )
        },
        "/.well-known/jwks.json": {"get": op("jwks", "oauth", "平台令牌验签公钥", res=ref("Jwks"), security=PUBLIC)},
        "/oauth/token": {
            "post": {
                **op(
                    "token",
                    "oauth",
                    "client_credentials 与 RFC 7523 jwt-bearer 令牌交换",
                    res=ref("TokenResponse"),
                    security=PUBLIC,
                ),
                "requestBody": {
                    "required": True,
                    "content": {"application/x-www-form-urlencoded": {"schema": ref("TokenRequest")}},
                },
            }
        },
        f"{V}/catalog": {
            "get": op("catalog", "catalog", "按 Principal 过滤的能力目录", res=page("CatalogEntry"), params=LIST)
        },
        f"{V}/invoke/{{capability_id}}": {
            "post": {
                **op(
                    "invoke",
                    "invoke",
                    "调用 Tool（统一执行链）",
                    req=ref("InvokeRequest"),
                    res=ref("ExecutionResult"),
                    idem=True,
                ),
                "parameters": [
                    {"name": "capability_id", "in": "path", "required": True, "schema": S},
                    {"$ref": "#/components/parameters/IdempotencyKey"},
                    {"name": "X-User-Assertion", "in": "header", "schema": S},
                ],
            }
        },
        f"{V}/executions/{{id}}": {
            "get": op("getExecution", "invoke", "查询执行结果", res=ref("ExecutionResult"), params=["Id"])
        },
        f"{V}/runs/{{id}}": {"get": op("getRun", "invoke", "查询 Workflow Run", res=ref("Run"), params=["Id"])},
        f"{V}/internal/todo": {
            "post": op(
                "internalTodo",
                "internal",
                "内部（服务凭据）：待处理的确认与人工任务，按公司过滤",
                req=o({"company_ids": {"type": ["array", "null"], "items": S}}),
                res={"type": "object"},
            )
        },
        f"{V}/internal/confirmations/{{id}}": {
            "get": op(
                "internalGetConfirmation",
                "internal",
                "内部（服务凭据）：确认摘要",
                res={"type": "object"},
                params=["Id"],
            )
        },
        f"{V}/internal/confirmations/{{id}}/decide": {
            "post": op(
                "internalDecideConfirmation",
                "internal",
                "内部（服务凭据）：控制台审批后执行或拒绝",
                req=ref("InternalDecision"),
                res=ref("ExecutionResult"),
                params=["Id"],
            )
        },
        f"{V}/internal/tasks/{{id}}": {
            "get": op(
                "internalGetTask", "internal", "内部（服务凭据）：人工任务", res={"type": "object"}, params=["Id"]
            )
        },
        f"{V}/internal/tasks/{{id}}/decide": {
            "post": op(
                "internalDecideTask",
                "internal",
                "内部（服务凭据）：处理人工任务",
                req=ref("InternalDecision"),
                res={"type": "object"},
                params=["Id"],
            )
        },
        f"{V}/internal/runs/{{id}}": {
            "get": op(
                "internalGetRun", "internal", "内部（服务凭据）：Run 全量视图", res={"type": "object"}, params=["Id"]
            )
        },
        f"{V}/confirmations/{{id}}/confirm": {
            "post": op(
                "confirm",
                "confirmation",
                "确认（需同一用户的新断言）",
                req=ref("ConfirmRequest"),
                res=ref("ExecutionResult"),
                params=["Id"],
            )
        },
        f"{V}/confirmations/{{id}}/reject": {
            "post": op(
                "rejectConfirmation",
                "confirmation",
                "拒绝确认",
                req=ref("ConfirmRequest"),
                res=ref("ExecutionResult"),
                params=["Id"],
            )
        },
        "/mcp": {
            "post": {
                **op(
                    "mcp",
                    "mcp",
                    "MCP Streamable HTTP（JSON-RPC，协议 2025-11-25 与 2026-07-28）",
                    res={"type": "object", "description": "JSON-RPC 响应"},
                ),
                "requestBody": {"required": True, "content": {"application/json": {"schema": {"type": "object"}}}},
            }
        },
        "/mcp/bundles/{name}": {
            "post": {
                **op("mcpBundle", "mcp", "按套件暴露的 MCP 端点", res={"type": "object"}),
                "parameters": [{"name": "name", "in": "path", "required": True, "schema": S}],
                "requestBody": {"required": True, "content": {"application/json": {"schema": {"type": "object"}}}},
            }
        },
        "/mcp/discovery": {
            "post": {
                **op(
                    "mcpDiscovery",
                    "mcp",
                    "发现模式 MCP：仅 search/get/invoke_capability（danger 不进入）",
                    res={"type": "object"},
                ),
                "requestBody": {"required": True, "content": {"application/json": {"schema": {"type": "object"}}}},
            }
        },
    }
    return doc("AIP Data Plane API", "数据面：OAuth 令牌、目录、统一调用、确认、MCP", paths, sch)


def doc(title: str, desc: str, paths: dict[str, Any], schemas: dict[str, Any]) -> dict[str, Any]:
    used = yaml.safe_dump(paths)
    params = {k: v for k, v in COMMON_COMPONENTS["parameters"].items() if f"#/components/parameters/{k}" in used}
    comps = {**COMMON_COMPONENTS, "parameters": params, "schemas": schemas}
    return {
        "openapi": "3.1.0",
        "info": {
            "title": title,
            "version": "0.1.0",
            "description": desc + "。约定见 conventions.md，错误码见 error-codes.md。",
        },
        "servers": [{"url": "https://api.example-group.internal"}],
        "security": [{"bearer": []}],
        "tags": sorted({t for p in paths.values() for m in p.values() for t in m["tags"]}),
        "paths": paths,
        "components": comps,
    }


class _NoAliasDumper(yaml.SafeDumper):
    def ignore_aliases(self, data: Any) -> bool:
        return True


def main() -> None:
    for name, d in (("control-plane", control_plane()), ("data-plane", data_plane())):
        d["tags"] = [{"name": t} for t in d["tags"]]
        (OUT / f"{name}.openapi.yaml").write_text(
            yaml.dump(d, Dumper=_NoAliasDumper, allow_unicode=True, sort_keys=False, width=120), encoding="utf-8"
        )
    print("wrote 2 OpenAPI documents")


if __name__ == "__main__":
    main()
