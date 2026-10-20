"""Test Lab 执行器（architecture.md 7.2）：在临时沙箱世界里用真实 Runtime 与真实策略执行用例。

- 组织、外部编码取自当前 IAM 快照（真实数据），消费方、用户、授权按用例的 principal 画像合成；
- 只装载 sandbox / test 环境的连接（及 mock），Runtime 以 sandbox 模式运行，生产写接口不可达；
- 权限用例与功能用例走同一条执行链，所以“该拒绝的确实被拒绝”。

principal 画像：{"identity": "user" | "app", "company": "VN01", "scope": "company" | "none" | "<org code>",
"clearance": 1-4, "granted": true | false}；expect：{"status": ..., "error_code": ..., "equals": ...}
"""

from __future__ import annotations

import copy
import pathlib
import tempfile
import uuid
from dataclasses import dataclass, field
from typing import Any

import jsonschema

from aip.connectors.credentials import SecretStore
from aip.control_plane.registry.snapshot import policy_bundle
from aip.data_plane.iam_cache import IamCache
from aip.data_plane.pdp import OpaClient, PolicyService
from aip.data_plane.principal import PrincipalResolver
from aip.data_plane.release_cache import ReleaseCache
from aip.data_plane.runtime import ConnectorRegistry, MemoryExecutionStore, ToolRuntime
from aip.libs.audit.spool import AuditSpool
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.tokens.keys import SigningKey
from aip.libs.tokens.signed import sign_payload

TEST_CONSUMER = "0192e3a0-7e57-7000-8000-00000000c0de"
TEST_ASSET = "0192e3a0-7e57-7000-8000-00000000a55e"
SANDBOX_ENVS = ("sandbox", "test")


@dataclass
class CaseResult:
    name: str
    kind: str
    critical: bool
    passed: bool
    status: str
    error_code: str | None
    detail: str = ""
    contract_ok: bool | None = None


@dataclass
class LabReport:
    cases: list[CaseResult] = field(default_factory=list)

    @property
    def g1_passed(self) -> bool:
        functional = [c for c in self.cases if c.kind == "functional" and c.contract_ok is not None]
        return bool(functional) and all(c.contract_ok for c in functional)

    @property
    def g2_passed(self) -> bool:
        critical = [c for c in self.cases if c.critical]
        has_denial = any(c.kind == "permission" for c in self.cases)
        return bool(critical) and has_denial and all(c.passed for c in critical)

    def evidence(self) -> dict[str, Any]:
        return {
            "cases": [c.__dict__ for c in self.cases],
            "total": len(self.cases),
            "passed_count": sum(c.passed for c in self.cases),
        }


def _org_by_code(iam: dict[str, Any], code: str) -> str:
    for oid, o in iam["orgs"].items():
        if o["code"] == code:
            return str(oid)
    raise PlatformError(ErrorCode.INVALID_ARGUMENT, f"用例引用了不存在的组织编码 {code}")


async def run_cases(
    *,
    spec: dict[str, Any],
    classification: int,
    cases: list[dict[str, Any]],
    iam_payload: dict[str, Any],
    connectors: list[dict[str, Any]],
    allowlist: list[str],
    opa_url: str,
    secrets: SecretStore,
    resolver: Any = None,
) -> LabReport:
    key = SigningKey.generate()
    work = pathlib.Path(tempfile.mkdtemp(prefix="aip-testlab-"))
    sandbox_connectors = [
        c for c in connectors if c["kind"] == "mock" or (c["connection"] and c["connection"]["env"] in SANDBOX_ENVS)
    ]
    release = {
        "release_id": "testlab",
        "asset_id": TEST_ASSET,
        "asset_type": "tool",
        "name": spec["capability_id"],
        "capability_id": spec["capability_id"],
        "version": "0.0.0-testlab",
        "content_hash": "testlab",
        "env": "test",
        "channel": "stable",
        "scope": {"group": True, "company_ids": [], "consumer_ids": []},
        "exposure": ["rest", "mcp"],
        "status": "canary",
        "classification": classification,
        "spec": spec,
    }
    rel_payload = {
        "format": 1,
        "seq": 1,
        "releases": [release],
        "policy": policy_bundle(),
        "connectors": sandbox_connectors,
        "network_allowlist": allowlist,
    }
    report = LabReport()
    opa = OpaClient(opa_url)
    try:
        for case in cases:
            report.cases.append(
                await _run_one(case, spec, classification, iam_payload, rel_payload, key, work, opa, secrets, resolver)
            )
    finally:
        await opa.aclose()
    return report


async def _run_one(
    case: dict[str, Any],
    spec: dict[str, Any],
    classification: int,
    iam_payload: dict[str, Any],
    rel_payload: dict[str, Any],
    key: SigningKey,
    work: pathlib.Path,
    opa: OpaClient,
    secrets: SecretStore,
    resolver: Any,
) -> CaseResult:
    prof = case.get("principal") or {}
    iam = copy.deepcopy(iam_payload)
    company = _org_by_code(iam, prof.get("company", "")) if prof.get("company") else None
    if company is None:  # 默认：第一个公司节点
        company = next(oid for oid, o in iam["orgs"].items() if o["type"] == "company")
    scope_name = prof.get("scope", "company")
    scope = (
        []
        if scope_name == "none"
        else iam["closure"].get(company if scope_name == "company" else _org_by_code(iam, scope_name), [])
    )
    user_id = str(uuid.uuid4())
    iam["users"][user_id] = {
        "company_id": company,
        "home_org_id": company,
        "status": "active",
        "clearance": int(prof.get("clearance", classification)),
        "org_ids": [a for a, ds in iam["closure"].items() if company in ds],
        "data_scope_org_ids": scope,
        "roles": prof.get("roles", []),
    }
    iam["consumers"] = {
        TEST_CONSUMER: {
            "client_id": "testlab",
            "company_id": company,
            "clearance_cap": 4,
            "bind_policy": "strict",
            "status": "active",
            "jwks": {},
            "static_tokens": {},
            "bindings": {"tester": user_id},
            "agent_profiles": {},
        }
    }
    iam["grants"] = (
        []
        if prof.get("granted") is False
        else [
            {
                "id": "g",
                "asset_id": TEST_ASSET,
                "grantee_type": "consumer",
                "grantee_id": TEST_CONSUMER,
                "env": "test",
                "company_id": company,
                "within_ceiling": True,
                "expires_at": "2099-01-01T00:00:00+00:00",
            }
        ]
    )
    iam["seq"] = 1
    iam_cache, rel_cache = IamCache([key]), ReleaseCache([key])
    for payload, cache in ((iam, iam_cache), (copy.deepcopy(rel_payload), rel_cache)):
        digest, sig = sign_payload(payload, key)
        cache.load({"seq": 1, "sha256": digest, "signature": sig, "payload": payload}, persist=False)
    audit = AuditSpool(work / "audit", "testlab")
    policy = PolicyService(iam_cache, rel_cache, opa, audit, env="test")
    runtime = ToolRuntime(
        iam=iam_cache,
        releases=rel_cache,
        policy=policy,
        registry=ConnectorRegistry(rel_cache, secrets, resolver),
        store=MemoryExecutionStore(),
        audit=audit,
        mode="sandbox",
    )
    resolver_ = PrincipalResolver(iam_cache)
    principal = (
        resolver_.for_app("testlab", auth_mode="app_token")
        if prof.get("identity") == "app"
        else resolver_.for_user("testlab", user_id, auth_mode="token_exchange")
    )
    res = await runtime.invoke(
        principal,
        spec["capability_id"],
        case["args"],
        idempotency_key=f"testlab-{uuid.uuid4().hex}" if spec["risk_level"] != "read" else None,
    )
    expect = case["expect"]
    code = (res.get("error") or {}).get("code")
    passed = res["status"] == expect.get("status", "succeeded")
    detail = ""
    if passed and expect.get("error_code") and code != expect["error_code"]:
        passed, detail = False, f"期望错误码 {expect['error_code']}，实际 {code}"
    if passed and "equals" in expect and res["data"] != expect["equals"]:
        passed, detail = False, "返回内容与期望不一致"
    contract_ok = None
    if case.get("kind") == "functional" and res["status"] == "succeeded":
        try:
            jsonschema.validate(res["data"], spec.get("output_schema") or {})
            contract_ok = True
        except jsonschema.ValidationError as e:
            contract_ok, passed, detail = False, False, f"输出不符合 output_schema：{e.message}"
    elif case.get("kind") == "functional":
        contract_ok = False if expect.get("status", "succeeded") == "succeeded" else None
    if not passed and not detail:
        detail = f"期望 {expect.get('status', 'succeeded')}，实际 {res['status']}（{code}）"
    return CaseResult(
        case["name"],
        case.get("kind", "functional"),
        bool(case.get("critical", True)),
        passed,
        res["status"],
        code,
        detail,
        contract_ok,
    )
