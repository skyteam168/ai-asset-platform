"""控制面 RBAC（architecture.md 2.5、4.3）：角色 + 生效节点，管理操作按子树做范围检查。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from aip.control_plane.db import DbScope
from aip.libs.common.errors import ErrorCode, PlatformError

# 总部角色：跨公司生效。资产开发者为所有公司维护资产与 Connector（architecture.md 2.5）
GLOBAL_ROLES = frozenset({"platform_admin", "asset_developer", "data_owner", "auditor"})


@dataclass(frozen=True)
class RoleGrant:
    role: str
    scope_org_id: uuid.UUID
    scope_company_id: uuid.UUID


@dataclass(frozen=True)
class Actor:
    """控制台操作者。system 只用于引导与后台任务。"""

    user_id: uuid.UUID | None
    company_id: uuid.UUID | None
    roles: tuple[RoleGrant, ...] = ()
    system: bool = False
    # 每个角色生效节点的后代集合（含自身），由 load_actor 预先展开
    subtrees: dict[uuid.UUID, frozenset[uuid.UUID]] = field(default_factory=dict)

    @staticmethod
    def system_actor() -> Actor:
        return Actor(user_id=None, company_id=None, system=True)

    def has(self, role: str) -> bool:
        return self.system or any(r.role == role for r in self.roles)

    @property
    def is_global(self) -> bool:
        return self.system or any(r.role in GLOBAL_ROLES for r in self.roles)

    def db_scope(self) -> DbScope:
        if self.is_global:
            return DbScope(bypass=True)
        ids = {r.scope_company_id for r in self.roles}
        if self.company_id:
            ids.add(self.company_id)
        return DbScope(company_ids=frozenset(ids))

    def manages_org(self, org_id: uuid.UUID) -> bool:
        """platform_admin 管全部；company_admin 只管其生效节点子树。"""
        if self.has("platform_admin"):
            return True
        return any(
            r.role == "company_admin" and org_id in self.subtrees.get(r.scope_org_id, frozenset()) for r in self.roles
        )

    def require(self, ok: bool, detail: str = "无权执行该操作") -> None:
        if not ok:
            raise PlatformError(ErrorCode.PERMISSION_DENIED, detail)
