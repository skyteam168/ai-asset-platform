"""PDP 客户端（OPA sidecar）与 PolicyService.decide()（architecture.md 4.4、8.1、13）。

任何异常（OPA 不可达、策略未加载、返回格式不对、快照缺失）都按拒绝处理，并写审计。
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

from aip.data_plane.facts import build_input
from aip.data_plane.iam_cache import IamCache
from aip.data_plane.principal import Principal
from aip.data_plane.release_cache import ReleaseCache
from aip.libs.audit.spool import AuditSpool
from aip.libs.common.errors import PlatformError
from aip.libs.tokens.keys import canonical_json

log = logging.getLogger("aip.pdp")
DECISION_PATH = "/v1/data/aip/authz/decision"


@dataclass(frozen=True)
class Decision:
    result: Literal["allow", "deny", "require_confirmation"]
    reasons: list[str] = field(default_factory=list)
    kind: str | None = None
    policy_revision: str | None = None


class OpaClient:
    def __init__(self, base_url: str, timeout_s: float = 2.0) -> None:
        # sidecar 在本机：保持长连接，避免每次决策都建连
        self._http = httpx.AsyncClient(
            base_url=base_url, timeout=timeout_s, limits=httpx.Limits(max_keepalive_connections=32, max_connections=64)
        )
        self.revision: str | None = None

    async def load_bundle(self, bundle: dict[str, Any]) -> None:
        """把发布快照中的策略模块推送到 sidecar（bundle 已随快照一起验签）。"""
        if bundle["revision"] == self.revision:
            return
        for path, source in bundle["modules"].items():
            r = await self._http.put(
                f"/v1/policies/{path}", content=source.encode(), headers={"Content-Type": "text/plain"}
            )
            r.raise_for_status()
        self.revision = bundle["revision"]

    async def evaluate(self, input_: dict[str, Any]) -> Decision:
        r = await self._http.post(DECISION_PATH, json={"input": input_})
        r.raise_for_status()
        out = r.json().get("result")
        if not isinstance(out, dict) or out.get("result") not in ("allow", "deny", "require_confirmation"):
            return Decision("deny", ["POLICY_RESULT_INVALID"], policy_revision=self.revision)
        return Decision(out["result"], list(out.get("reasons", [])), out.get("kind"), self.revision)

    async def aclose(self) -> None:
        await self._http.aclose()


class PolicyService:
    def __init__(self, iam: IamCache, releases: ReleaseCache, opa: OpaClient, audit: AuditSpool, env: str) -> None:
        self.iam, self.releases, self.opa, self.audit, self.env = iam, releases, opa, audit, env

    async def sync_policy(self) -> None:
        await self.opa.load_bundle(self.releases.policy)

    async def visible(self, principal: Principal, release: dict[str, Any], *, exposure: str) -> bool:
        """tools/list 与目录的可见性：同一套策略，但不检查入参数据范围（此时还没有参数），不写审计。"""
        try:
            input_ = build_input(
                principal=principal, release=release, args={}, iam=self.iam.data, env=self.env, exposure=exposure
            )
            input_["facts"]["scope_check"] = {}
            if self.opa.revision != self.releases.policy["revision"]:
                await self.sync_policy()
            return (await self.opa.evaluate(input_)).result != "deny"
        except (PlatformError, httpx.HTTPError, KeyError, ValueError, TypeError):
            return False

    async def decide(
        self,
        principal: Principal | None,
        capability_id: str,
        args: dict[str, Any],
        *,
        exposure: str = "rest",
        channel: str = "stable",
        version: str | None = None,
    ) -> Decision:
        """所有调用必须经过这里；默认拒绝，决策与原因写入审计。"""
        release: dict[str, Any] | None = None
        try:
            if principal is None:
                decision = Decision("deny", ["UNAUTHENTICATED"])
            else:
                release = self.releases.resolve(capability_id, self.env, channel=channel, version=version)
                input_ = build_input(
                    principal=principal, release=release, args=args, iam=self.iam.data, env=self.env, exposure=exposure
                )
                if self.opa.revision != self.releases.policy["revision"]:
                    await self.sync_policy()
                decision = await self.opa.evaluate(input_)
        except PlatformError as e:
            decision = Decision("deny", [str(e.code)], policy_revision=self.opa.revision)
        except (httpx.HTTPError, KeyError, ValueError, TypeError) as e:
            log.error("pdp failure, fail closed: %s", type(e).__name__)
            decision = Decision("deny", ["POLICY_UNAVAILABLE"], policy_revision=self.opa.revision)
        await self.audit.append_async(
            "pdp.decision",
            company_id=principal.consumer_company_id if principal else None,
            record={
                "capability_id": capability_id,
                "version": release["version"] if release else version,
                "exposure": exposure,
                "client_id": principal.client_id if principal else None,
                "end_user_id": principal.user_id if principal else None,
                "identity_kind": principal.identity_kind if principal else None,
                "auth_mode": principal.auth_mode if principal else None,
                "iam_snapshot_seq": self.iam.seq,
                "release_snapshot_seq": self.releases.seq,
                "decision": decision.result,
                "reasons": decision.reasons,
                "confirmation_kind": decision.kind,
                "policy_revision": decision.policy_revision,
                # 参数只存摘要，不存原值（architecture.md 8.3）
                "args_sha256": hashlib.sha256(canonical_json(args)).hexdigest(),
            },
        )
        return decision
