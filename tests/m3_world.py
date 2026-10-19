"""M3 Runtime 测试：直接构造并签名 IAM / 发布快照（不经数据库），PDP 使用真实 OPA。"""

from __future__ import annotations

import copy
import pathlib
from typing import Any

from aip.connectors.credentials import MemorySecretStore
from aip.control_plane.registry.snapshot import policy_bundle
from aip.data_plane.app import create_app
from aip.data_plane.principal import Principal
from aip.data_plane.runtime import ExecutionStore, MemoryExecutionStore
from aip.libs.tokens.keys import SigningKey
from aip.libs.tokens.replay import MemoryReplayStore
from aip.libs.tokens.signed import sign_payload

KEY = SigningKey.generate()
# 用真实 UUID，执行记录可以写入 exec.tool_execution
VN, VN_MFG, KH, GROUP = (f"0192e3a0-0000-7000-8000-00000000000{c}" for c in "1234")
CONSUMER, USER = "0192e3a0-0000-7000-8000-0000000000c1", "0192e3a0-0000-7000-8000-0000000000a1"
STATIC_TOKEN_HASH = "0" * 64


def tool(cap: str, connector: str, op: str, **over: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "capability_id": cap,
        "domain": cap.split(".")[0],
        "name": cap.split(".")[-1],
        "description": "测试用工具：调用 Connector 的指定操作并返回结构化结果。",
        "input_schema": {
            "type": "object",
            "properties": {"factory_id": {"type": "string"}, "status": {"type": "string"}},
            "required": ["factory_id"],
        },
        "output_schema": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "wo_no": {"type": "string"},
                    "qty": {"type": "integer"},
                    "status": {"type": "string"},
                    "phone": {"type": "string"},
                },
            },
        },
        "connector_ref": {"connector": connector, "operation": op},
        "risk_level": "read",
        "timeout": {"connect_ms": 1000, "read_ms": 2000, "deadline_ms": 1500},
        "data_classification": 2,
        "identity_mode": "service_account",
        "confirm_policy": "auto",
        "scope_binding": {"factory_id": {"dimension": "company", "map": "by_code", "system_key": "mes"}},
    }
    spec.update(over)
    return spec


def write_tool(cap: str, connector: str, op: str, **over: Any) -> dict[str, Any]:
    return tool(
        cap,
        connector,
        op,
        risk_level="write",
        idempotency={"idempotent": True},
        recovery="manual",
        reconcile={"operation": "query", "by": "idempotency_key"},
        output_schema={"type": "object"},
        **over,
    )


def connection(base_url: str, env: str = "test", **over: Any) -> dict[str, Any]:
    scheme = "https" if base_url.startswith("https") else "http"
    c = {
        "id": f"conn-{abs(hash(base_url)) % 10_000}",
        "base_url": base_url,
        "scheme": scheme,
        "env": env,
        "tls_mode": None if scheme == "http" else "system_ca",
        "pins": [],
        "status": "active",
        "transport_exception_reason": "测试" if scheme == "http" else None,
        "transport_exception_expires_at": "2099-01-01T00:00:00+00:00" if scheme == "http" else None,
        "connect_timeout_s": 1.0,
        "read_timeout_s": 2.0,
        "credential_ref": None,
    }
    c.update(over)
    return c


def connector(
    name: str, kind: str, config: dict[str, Any], conn: dict[str, Any] | None = None, **over: Any
) -> dict[str, Any]:
    return {
        "name": name,
        "kind": kind,
        "config": config,
        "connection": conn,
        "concurrency": 8,
        "rate_per_s": 1000,
        "burst": 1000,
        **over,
    }


def iam_payload(clearance: int = 2) -> dict[str, Any]:
    return {
        "format": 1,
        "seq": 1,
        "orgs": {
            GROUP: {"code": "GROUP", "type": "group", "parent_id": None, "company_id": GROUP},
            VN: {"code": "VN01", "type": "company", "parent_id": GROUP, "company_id": VN},
            VN_MFG: {"code": "VN01-MFG", "type": "department", "parent_id": VN, "company_id": VN},
            KH: {"code": "KH01", "type": "company", "parent_id": GROUP, "company_id": KH},
        },
        "closure": {GROUP: [GROUP, VN, VN_MFG, KH], VN: [VN, VN_MFG], VN_MFG: [VN_MFG], KH: [KH]},
        "users": {
            USER: {
                "company_id": VN,
                "home_org_id": VN_MFG,
                "status": "active",
                "clearance": clearance,
                "org_ids": [GROUP, VN, VN_MFG],
                "data_scope_org_ids": [VN_MFG],
                "roles": [],
            }
        },
        "employee_index": {},
        "consumers": {
            CONSUMER: {
                "client_id": "vn-ai",
                "company_id": VN,
                "clearance_cap": 3,
                "bind_policy": "strict",
                "status": "active",
                "jwks": {},
                "agent_profiles": {},
                "static_tokens": {STATIC_TOKEN_HASH: {"expires_at": "2099-01-01T00:00:00+00:00", "ip_binding": []}},
                "bindings": {"emp-1": USER},
            }
        },
        "external_codes": {"mes": {"F001": VN, "F900": KH}},
        "grants": [],
    }


class World:
    def __init__(
        self,
        tmp: pathlib.Path,
        tools: list[dict[str, Any]],
        connectors: list[dict[str, Any]],
        *,
        secrets: dict[str, dict[str, str]] | None = None,
        store: ExecutionStore | None = None,
        clearance: int = 2,
        resolver: Any = None,
    ) -> None:
        self.store = store or MemoryExecutionStore()
        self.app = create_app(
            key=KEY,
            replay=MemoryReplayStore(),
            data_dir=tmp,
            secrets=MemorySecretStore(secrets or {}),
            exec_store=self.store,
            resolver=resolver,
        )
        iam = iam_payload(clearance)
        releases = []
        for i, spec in enumerate(tools):
            asset = f"asset-{i}"
            iam["grants"].append(
                {
                    "id": f"g{i}",
                    "asset_id": asset,
                    "grantee_type": "consumer",
                    "grantee_id": CONSUMER,
                    "env": "test",
                    "company_id": VN,
                    "within_ceiling": True,
                    "expires_at": "2099-01-01T00:00:00+00:00",
                }
            )
            releases.append(
                {
                    "release_id": f"r{i}",
                    "asset_id": asset,
                    "asset_type": "tool",
                    "name": spec["capability_id"],
                    "capability_id": spec["capability_id"],
                    "version": "1.0.0",
                    "content_hash": "x",
                    "env": "test",
                    "channel": "stable",
                    "scope": {"group": True, "company_ids": [], "consumer_ids": []},
                    "exposure": ["rest", "mcp"],
                    "status": "active",
                    "classification": spec["data_classification"],
                    "spec": spec,
                }
            )
        rel = {
            "format": 1,
            "seq": 1,
            "releases": releases,
            "policy": policy_bundle(),
            "connectors": connectors,
            "network_allowlist": ["127.0.0.0/8", "10.0.0.0/8"],
        }
        for payload, cache in ((iam, self.app.state.iam), (rel, self.app.state.releases)):
            digest, sig = sign_payload(copy.deepcopy(payload), KEY)
            cache.load({"seq": 1, "sha256": digest, "signature": sig, "payload": payload})

    async def ready(self) -> None:
        await self.app.state.policy.sync_policy()

    @property
    def runtime(self) -> Any:
        return self.app.state.runtime

    def principal(self) -> Principal:
        r = self.app.state.auth.resolver
        return r.for_user("vn-ai", USER, auth_mode="token_exchange")

    def connector(self, name: str) -> Any:
        return self.runtime.registry.get(name).connector
