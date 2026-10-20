"""PrincipalResolver（architecture.md 3.2）：认证结果 + IAM 快照 → 统一 Principal。

令牌只证明“你是谁”；组织、角色、密级、数据范围全部来自快照，因此变更随快照生效。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from aip.data_plane.iam_cache import IamCache
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.tokens.assertion import VerifiedAssertion

AuthMode = Literal["token_exchange", "header_assertion", "app_token", "console_session"]


@dataclass(frozen=True)
class Principal:
    identity_kind: Literal["user", "app"]
    user_id: str | None
    company_id: str
    org_ids: list[str]
    roles: list[str]
    clearance: int
    data_scope_org_ids: list[str]
    client_id: str
    consumer_id: str
    consumer_company_id: str
    agent_profile_id: str | None
    auth_mode: AuthMode
    iam_snapshot_seq: int
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("extra")
        return d


class PrincipalResolver:
    def __init__(self, cache: IamCache) -> None:
        self.cache = cache
        # 按工号匹配成功（且尚无绑定）时回调，用于把绑定回写控制面（architecture.md 3.1）
        self.on_employee_match: Callable[[str, dict[str, Any]], None] | None = None

    def consumer(self, client_id: str) -> tuple[str, dict[str, Any]]:
        found = self.cache.consumer_by_client_id(client_id)
        if found is None or found[1]["status"] != "active":
            raise PlatformError(ErrorCode.UNAUTHENTICATED, "消费方不存在或已停用")
        return found

    def map_subject(self, consumer_id: str, consumer: dict[str, Any], subject: str) -> str:
        """按 bind_policy 把消费方侧员工标识映射为内部 user_id（architecture.md 3.1）。"""
        policy = consumer["bind_policy"]
        if policy == "app_only":
            raise PlatformError(ErrorCode.PERMISSION_DENIED, "该消费方只允许应用身份调用")
        user_id = consumer["bindings"].get(subject)
        if user_id is None and policy == "by_employee_no":
            user_id = self.cache.data["employee_index"].get(consumer["company_id"], {}).get(subject)
            if user_id is not None and self.on_employee_match is not None:
                self.on_employee_match(
                    "binding.employee_no_matched",
                    {"consumer_id": consumer_id, "external_subject": subject, "user_id": str(user_id)},
                )
        if user_id is None:
            raise PlatformError(ErrorCode.USER_NOT_BOUND, "该员工未绑定平台用户")
        return str(user_id)

    def user_still_bound(self, consumer: dict[str, Any], user_id: str) -> bool:
        if user_id in consumer["bindings"].values():
            return True
        if consumer["bind_policy"] == "by_employee_no":
            return user_id in self.cache.data["employee_index"].get(consumer["company_id"], {}).values()
        return False

    def for_user(
        self,
        client_id: str,
        user_id: str,
        *,
        auth_mode: AuthMode,
        agent_profile: str | None = None,
        check_binding: bool = True,
    ) -> Principal:
        consumer_id, c = self.consumer(client_id)
        if check_binding and not self.user_still_bound(c, user_id):
            raise PlatformError(ErrorCode.USER_NOT_BOUND, "绑定已失效")
        u = self.cache.data["users"].get(user_id)
        if u is None or u["status"] != "active":
            raise PlatformError(ErrorCode.UNAUTHENTICATED, "用户不存在或已停用")
        return Principal(
            identity_kind="user",
            user_id=user_id,
            company_id=u["company_id"],
            org_ids=u["org_ids"],
            roles=sorted({r["role"] for r in u["roles"]}),
            clearance=min(int(u["clearance"]), int(c["clearance_cap"])),
            data_scope_org_ids=u["data_scope_org_ids"],
            client_id=client_id,
            consumer_id=consumer_id,
            consumer_company_id=c["company_id"],
            agent_profile_id=self._profile(c, agent_profile),
            auth_mode=auth_mode,
            iam_snapshot_seq=int(self.cache.seq or 0),
        )

    def for_app(self, client_id: str, *, auth_mode: AuthMode, agent_profile: str | None = None) -> Principal:
        """应用身份：数据范围限于消费方所属公司子树，有效密级为 clearance_cap（architecture.md 3.1）。"""
        consumer_id, c = self.consumer(client_id)
        company = c["company_id"]
        return Principal(
            identity_kind="app",
            user_id=None,
            company_id=company,
            org_ids=sorted(a for a, ds in self.cache.data["closure"].items() if company in ds),
            roles=[],
            clearance=int(c["clearance_cap"]),
            data_scope_org_ids=self.cache.data["closure"].get(company, []),
            client_id=client_id,
            consumer_id=consumer_id,
            consumer_company_id=company,
            agent_profile_id=self._profile(c, agent_profile),
            auth_mode=auth_mode,
            iam_snapshot_seq=int(self.cache.seq or 0),
        )

    def for_assertion(self, verified: VerifiedAssertion, auth_mode: AuthMode) -> Principal:
        consumer_id, c = self.consumer(verified.client_id)
        user_id = self.map_subject(consumer_id, c, verified.subject)
        return self.for_user(
            verified.client_id, user_id, auth_mode=auth_mode, agent_profile=verified.act_agent, check_binding=False
        )

    def static_token(self, digest: str, now: dt.datetime) -> str:
        """静态 API Token → client_id。"""
        found = self.cache.consumer_by_token_hash(digest)
        if found is None:
            raise PlatformError(ErrorCode.UNAUTHENTICATED, "凭据无效")
        _, c, tok = found
        if dt.datetime.fromisoformat(tok["expires_at"]) <= now or c["status"] != "active":
            raise PlatformError(ErrorCode.UNAUTHENTICATED, "凭据无效或已过期")
        return str(c["client_id"])

    @staticmethod
    def _profile(c: dict[str, Any], name: str | None) -> str | None:
        if name is None:
            return None
        pid = c["agent_profiles"].get(name)
        if pid is None:
            raise PlatformError(ErrorCode.PERMISSION_DENIED, "未登记的 Agent Profile")
        return str(pid)
