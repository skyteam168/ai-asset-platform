"""PDP 事实预计算（architecture.md 4.4）：OPA 只持有策略，组织闭包、授权命中、范围映射都在这里算好。"""

from __future__ import annotations

from typing import Any

from aip.data_plane.principal import Principal


def _release_in_scope(scope: dict[str, Any], p: Principal) -> bool:
    return (
        bool(scope.get("group"))
        or p.consumer_company_id in scope.get("company_ids", [])
        or p.consumer_id in scope.get("consumer_ids", [])
    )


def _scope_check(
    release: dict[str, Any], args: dict[str, Any], p: Principal, iam: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """scope_binding 映射（architecture.md 2.3）：
    - dimension=org：入参映射到的节点必须在用户数据范围内；
    - dimension=company：入参映射到公司节点，用户数据范围内至少有一个节点属于该公司。
    入参缺失视为越界：绑定了组织维度的参数不能省略。
    """
    out: dict[str, dict[str, Any]] = {}
    orgs = iam["orgs"]
    scope = set(p.data_scope_org_ids)
    for arg, binding in (release["spec"].get("scope_binding") or {}).items():
        value = args.get(arg)
        org_id: str | None = None
        if isinstance(value, str):
            if binding["map"] == "by_id":
                org_id = value if value in orgs else None
            else:
                org_id = iam["external_codes"].get(binding.get("system_key", ""), {}).get(value)
        if org_id is None:
            out[arg] = {"org_id": None, "in_scope": False}
        elif binding["dimension"] == "company":
            company = orgs[org_id]["company_id"]
            ok = orgs[org_id]["type"] == "company" and any(orgs.get(o, {}).get("company_id") == company for o in scope)
            out[arg] = {"org_id": org_id, "in_scope": ok}
        else:
            out[arg] = {"org_id": org_id, "in_scope": org_id in scope}
    return out


def build_input(
    *, principal: Principal, release: dict[str, Any], args: dict[str, Any], iam: dict[str, Any], env: str, exposure: str
) -> dict[str, Any]:
    asset_id = release["asset_id"]
    grants = [g for g in iam["grants"] if g["asset_id"] == asset_id and g["env"] == env]
    app_grants = [
        g
        for g in grants
        if (g["grantee_type"] == "consumer" and g["grantee_id"] == principal.consumer_id)
        or (g["grantee_type"] == "agent_profile" and g["grantee_id"] == principal.agent_profile_id)
    ]
    user_grants = [
        g
        for g in grants
        if g["grantee_type"] in ("org_unit", "user") and g["company_id"] == principal.consumer_company_id
    ]
    user_grant: bool | None = None
    if principal.identity_kind == "user" and user_grants:
        user_grant = any(
            (g["grantee_type"] == "user" and g["grantee_id"] == principal.user_id)
            or (g["grantee_type"] == "org_unit" and g["grantee_id"] in principal.org_ids)
            for g in user_grants
        )
    spec = release["spec"]
    if release.get("asset_type") == "workflow":
        # 启动 Workflow 本身不触达数据；每个 Tool 步骤都以 Run 的 Principal 单独经过 PDP（含确认）
        spec = {"risk_level": "read", "confirm_policy": "auto", "app_identity_allowed": True, **spec}
    return {
        "principal": {
            "identity_kind": principal.identity_kind,
            "user_id": principal.user_id,
            "company_id": principal.company_id,
            "clearance": principal.clearance,
            "roles": principal.roles,
            "client_id": principal.client_id,
            "agent_profile_id": principal.agent_profile_id,
        },
        "facts": {
            "release_in_scope": _release_in_scope(release["scope"], principal),
            "app_grant": bool(app_grants),
            "within_ceiling": any(g["within_ceiling"] for g in app_grants) if app_grants else True,
            "user_grant_required": bool(user_grants),
            "user_grant": user_grant,
            "scope_check": _scope_check(release, args, principal, iam),
        },
        "resource": {
            "capability_id": release["capability_id"],
            "version": release["version"],
            "classification": release["classification"],
            "risk": spec.get("risk_level", "danger"),
            "release_status": release["status"],
            "confirm_policy": spec.get("confirm_policy", "approval"),
            "app_identity_allowed": bool(spec.get("app_identity_allowed", False)),
            "transport_exception": False,
            "exposure": release["exposure"],
            "required_scopes": spec.get("required_scopes", []),
        },
        "context": {"env": env, "channel": release["channel"], "exposure": exposure},
    }
