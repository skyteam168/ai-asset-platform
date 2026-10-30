"""控制面 IAM 接口（docs/api/control-plane.openapi.yaml）。每个操作在操作者可见公司范围内的事务中执行。"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from aip.control_plane.db import Database, DbScope
from aip.control_plane.iam import (
    auth,
    business_roles,
    consumers,
    external_login,
    orgs,
    providers,
    snapshot,
    users,
    webauthn,
)
from aip.control_plane.iam import schemas as out
from aip.control_plane.iam import tables as t
from aip.control_plane.iam.authz import Actor
from aip.libs.common.config import get_settings
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.common.paging import Page
from aip.libs.tokens.assertion import verify_assertion
from aip.libs.tokens.keys import platform_key
from aip.libs.tokens.replay import MemoryReplayStore

V = "/api/v1"


def _db(request: Request) -> Database:
    db: Database = request.app.state.db
    return db


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise PlatformError(ErrorCode.UNAUTHENTICATED, "缺少会话令牌")
    return authorization[7:].strip()


async def current_actor(request: Request, authorization: Annotated[str | None, Header()] = None) -> Actor:
    db = _db(request)
    token = _bearer(authorization)
    async with db.tx(DbScope.system()) as conn:
        user_id = await auth.resolve_session(conn, token)
        return await users.load_actor(conn, user_id)


ActorDep = Annotated[Actor, Depends(current_actor)]
DbDep = Annotated[Database, Depends(_db)]


class LoginIn(BaseModel):
    login_name: str
    password: str
    totp: str | None = None


class OidcCallbackIn(BaseModel):
    """OIDC 回调骨架：stub 接受 id_token=stub:<sub>；真 IdP 联调后再接 code 交换。"""

    id_token: str = Field(min_length=1, max_length=8192)


class ExternalLoginIn(BaseModel):
    provider: str = Field(pattern=r"^[a-z][a-z0-9_]{1,31}$")
    external_id: str = Field(min_length=1, max_length=256)


class WebAuthnFinishIn(BaseModel):
    challenge_id: uuid.UUID
    credential_id: str = Field(min_length=1, max_length=512)
    public_key_cose_b64: str = Field(min_length=1, max_length=8192)
    nickname: str = ""


class WebAuthnAuthBeginIn(BaseModel):
    login_name: str


class WebAuthnAuthFinishIn(BaseModel):
    challenge_id: uuid.UUID
    credential_id: str = Field(min_length=1, max_length=512)
    sign_count: int | None = Field(default=None, ge=0, description="认证器计数；须严格大于已存值，防重放")


class OrgIn(BaseModel):
    type: Literal["company", "department", "team"]
    code: str = Field(min_length=1, max_length=64)
    name: str
    parent_id: uuid.UUID
    default_clearance: int | None = Field(default=None, ge=1, le=4)
    timezone: str | None = None
    locale: str | None = None


class MoveIn(BaseModel):
    new_parent_id: uuid.UUID


class UserIn(BaseModel):
    display_name: str
    home_org_id: uuid.UUID
    login_name: str | None = None
    password: str | None = None
    employee_no: str | None = None
    email: str | None = None
    login_enabled: bool = False


class ImportIn(BaseModel):
    format: Literal["csv"] = "csv"
    content: str
    dry_run: bool = True


class RoleIn(BaseModel):
    role: str
    scope_org_id: uuid.UUID
    expires_at: dt.datetime | None = None


class BusinessRoleIn(BaseModel):
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    name: str = Field(min_length=1, max_length=128)
    description: str = ""


class ClearanceIn(BaseModel):
    user_id: uuid.UUID
    requested_level: int = Field(ge=1, le=4)
    reason: str = Field(min_length=4)
    expires_at: dt.datetime


class DecisionIn(BaseModel):
    decision: Literal["approve", "reject"]
    comment: str = ""


class ConsumerIn(BaseModel):
    client_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,63}$")
    company_id: uuid.UUID
    clearance_cap: int = Field(ge=1, le=4)
    bind_policy: Literal["strict", "by_employee_no", "app_only"] = "strict"
    contact: str | None = None


class CredentialIn(BaseModel):
    kind: Literal["static_token"] = "static_token"
    days: int = Field(default=90, ge=1, le=365)
    ip_binding: list[str] | None = None


class BindingIn(BaseModel):
    external_subject: str = Field(min_length=1, max_length=256)
    user_id: uuid.UUID


class DebugIn(BaseModel):
    consumer_id: uuid.UUID
    assertion: str


def _user_out(row: dict[str, Any], clearance: int, *, totp_uri: str | None = None) -> out.User:
    keys = (
        "id",
        "login_name",
        "display_name",
        "employee_no",
        "email",
        "home_org_id",
        "login_enabled",
        "status",
        "timezone",
        "locale",
    )
    data = {k: (str(row[k]) if isinstance(row[k], uuid.UUID) else row[k]) for k in keys}
    data["effective_clearance"] = clearance
    data["totp_provisioning_uri"] = totp_uri
    return out.User.model_validate(data)


def router() -> APIRouter:
    r = APIRouter()

    @r.post(f"{V}/auth/login", tags=["auth"], operation_id="login", response_model=out.Session)
    async def login(body: LoginIn, db: DbDep) -> out.Session:
        async with db.tx(DbScope.system()) as conn:
            try:
                token, expires, user_id = await auth.login(conn, body.login_name, body.password, body.totp)
            except PlatformError:
                # 失败计数与锁定需要落库，因此先提交再抛错
                await conn.commit()
                raise
            u = await users.user_row(conn, user_id)
            clearance = await users.effective_clearance(conn, user_id)
        return out.Session(
            session_token=token, expires_at=expires.isoformat(), user=_user_out(u, clearance)
        )

    @r.post(f"{V}/auth/logout", tags=["auth"], operation_id="logout", status_code=204)
    async def logout(db: DbDep, authorization: Annotated[str | None, Header()] = None) -> Response:
        async with db.tx(DbScope.system()) as conn:
            await auth.logout(conn, _bearer(authorization))
        return Response(status_code=204)

    @r.get(
        f"{V}/auth/providers",
        tags=["auth"],
        operation_id="listAuthProviders",
        response_model=Page[out.AuthProviderInfo],
    )
    async def list_auth_providers() -> Page[out.AuthProviderInfo]:
        items = [out.AuthProviderInfo.model_validate(p) for p in providers.list_providers()]
        return Page(items=items)

    @r.post(
        f"{V}/auth/oidc/callback",
        tags=["auth"],
        operation_id="oidcCallback",
        response_model=out.Session,
    )
    async def oidc_callback(body: OidcCallbackIn, db: DbDep) -> out.Session:
        async with db.tx(DbScope.system()) as conn:
            token, expires, user_id = await auth.login_via_provider(
                conn, "oidc", {"id_token": body.id_token}
            )
            u = await users.user_row(conn, user_id)
            clearance = await users.effective_clearance(conn, user_id)
        return out.Session(
            session_token=token, expires_at=expires.isoformat(), user=_user_out(u, clearance)
        )

    @r.get(f"{V}/me", tags=["auth"], operation_id="me", response_model=out.Me)
    async def me(actor: ActorDep, db: DbDep) -> out.Me:
        assert actor.user_id is not None
        async with db.tx(actor.db_scope()) as conn:
            u = await users.user_row(conn, actor.user_id)
            base = _user_out(u, await users.effective_clearance(conn, actor.user_id))
        return out.Me(
            **base.model_dump(),
            roles=[out.RoleGrant(role=g.role, scope_org_id=str(g.scope_org_id)) for g in actor.roles],
        )

    @r.get(f"{V}/orgs", tags=["iam"], operation_id="listOrgs", response_model=Page[out.OrgUnit])
    async def list_orgs(actor: ActorDep, db: DbDep) -> Page[out.OrgUnit]:
        async with db.tx(actor.db_scope()) as conn:
            items = [out.OrgUnit.model_validate(o.as_dict()) for o in await orgs.list_orgs(conn)]
        return Page(items=items)

    @r.post(f"{V}/orgs", tags=["iam"], operation_id="createOrg", status_code=201, response_model=out.OrgUnit)
    async def create_org(body: OrgIn, actor: ActorDep, db: DbDep) -> out.OrgUnit:
        async with db.tx(actor.db_scope()) as conn:
            o = await orgs.create_org(
                conn,
                actor,
                type_=body.type,
                code=body.code,
                name=body.name,
                parent_id=body.parent_id,
                default_clearance=body.default_clearance,
                timezone=body.timezone,
                locale=body.locale,
            )
        return out.OrgUnit.model_validate(o.as_dict())

    @r.get(f"{V}/orgs/{{id}}", tags=["iam"], operation_id="getOrg", response_model=out.OrgUnit)
    async def get_org(id: uuid.UUID, actor: ActorDep, db: DbDep) -> out.OrgUnit:
        async with db.tx(actor.db_scope()) as conn:
            return out.OrgUnit.model_validate((await orgs.get_org(conn, id)).as_dict())

    @r.post(f"{V}/orgs/{{id}}/move", tags=["iam"], operation_id="moveOrg", response_model=out.OrgUnit)
    async def move_org(id: uuid.UUID, body: MoveIn, actor: ActorDep, db: DbDep) -> out.OrgUnit:
        async with db.tx(actor.db_scope()) as conn:
            return out.OrgUnit.model_validate((await orgs.move_org(conn, actor, id, body.new_parent_id)).as_dict())

    @r.post(f"{V}/orgs/{{id}}/disable", tags=["iam"], operation_id="disableOrg", response_model=out.OrgUnit)
    async def disable_org(id: uuid.UUID, actor: ActorDep, db: DbDep) -> out.OrgUnit:
        async with db.tx(actor.db_scope()) as conn:
            return out.OrgUnit.model_validate((await orgs.disable_org(conn, actor, id)).as_dict())

    @r.get(f"{V}/users", tags=["iam"], operation_id="listUsers", response_model=Page[out.User])
    async def list_users(actor: ActorDep, db: DbDep) -> Page[out.User]:
        async with db.tx(actor.db_scope()) as conn:
            rows = (await conn.execute(select(t.iam_user).order_by(t.iam_user.c.id).limit(200))).mappings().all()
            items = [_user_out(dict(u), await users.effective_clearance(conn, u["id"])) for u in rows]
        return Page(items=items)

    @r.post(f"{V}/users", tags=["iam"], operation_id="createUser", status_code=201, response_model=out.User)
    async def create_user(body: UserIn, actor: ActorDep, db: DbDep) -> out.User:
        async with db.tx(actor.db_scope()) as conn:
            created = await users.create_user(
                conn,
                actor,
                display_name=body.display_name,
                home_org_id=body.home_org_id,
                login_name=body.login_name,
                password=body.password,
                employee_no=body.employee_no,
                email=body.email,
                login_enabled=body.login_enabled,
            )
            return _user_out(
                await users.user_row(conn, created.id),
                await users.effective_clearance(conn, created.id),
                totp_uri=created.totp_provisioning_uri,
            )

    @r.post(f"{V}/users/import", tags=["iam"], operation_id="importUsers", response_model=out.ImportResult)
    async def import_users(body: ImportIn, actor: ActorDep, db: DbDep) -> out.ImportResult:
        async with db.tx(actor.db_scope()) as conn:
            res = await users.import_users_csv(conn, actor, body.content, body.dry_run)
        return out.ImportResult(
            batch_id=str(res.batch_id),
            dry_run=res.dry_run,
            created=res.created,
            updated=0,
            unchanged=res.unchanged,
            errors=[out.ImportErrorItem.model_validate(e) for e in res.errors],
        )

    @r.post(
        f"{V}/users/import/{{id}}/rollback",
        tags=["iam"],
        operation_id="rollbackImport",
        response_model=out.ImportRollback,
    )
    async def rollback_import(id: uuid.UUID, actor: ActorDep, db: DbDep) -> out.ImportRollback:
        async with db.tx(actor.db_scope()) as conn:
            result = await users.rollback_import(conn, actor, id)
        return out.ImportRollback(batch_id=str(id), **result)

    @r.get(f"{V}/users/{{id}}", tags=["iam"], operation_id="getUser", response_model=out.User)
    async def get_user(id: uuid.UUID, actor: ActorDep, db: DbDep) -> out.User:
        async with db.tx(actor.db_scope()) as conn:
            return _user_out(await users.user_row(conn, id), await users.effective_clearance(conn, id))

    @r.get(
        f"{V}/users/{{id}}/roles",
        tags=["iam"],
        operation_id="listUserRoles",
        response_model=Page[out.RoleAssignment],
    )
    async def list_user_roles(id: uuid.UUID, actor: ActorDep, db: DbDep) -> Page[out.RoleAssignment]:
        async with db.tx(actor.db_scope()) as conn:
            items = [out.RoleAssignment.model_validate(x) for x in await users.list_roles_for_user(conn, actor, id)]
        return Page(items=items)

    @r.post(
        f"{V}/users/{{id}}/roles",
        tags=["iam"],
        operation_id="assignRole",
        status_code=201,
        response_model=out.RoleAssignment,
    )
    async def assign_role(id: uuid.UUID, body: RoleIn, actor: ActorDep, db: DbDep) -> out.RoleAssignment:
        async with db.tx(actor.db_scope()) as conn:
            rid = await users.assign_role(conn, actor, id, body.role, body.scope_org_id, body.expires_at)
        return out.RoleAssignment(
            id=str(rid), user_id=str(id), role=body.role, scope_org_id=str(body.scope_org_id)
        )

    @r.get(
        f"{V}/users/{{id}}/external-logins",
        tags=["iam"],
        operation_id="listExternalLogins",
        response_model=Page[out.ExternalLogin],
    )
    async def list_external_logins(id: uuid.UUID, actor: ActorDep, db: DbDep) -> Page[out.ExternalLogin]:
        async with db.tx(actor.db_scope()) as conn:
            await users.user_row(conn, id)  # 404 / RLS
            items = [out.ExternalLogin.model_validate(x) for x in await external_login.list_for_user(conn, id)]
        return Page(items=items)

    @r.post(
        f"{V}/users/{{id}}/external-logins",
        tags=["iam"],
        operation_id="bindExternalLogin",
        status_code=201,
        response_model=out.ExternalLogin,
    )
    async def bind_external_login(
        id: uuid.UUID, body: ExternalLoginIn, actor: ActorDep, db: DbDep
    ) -> out.ExternalLogin:
        async with db.tx(actor.db_scope()) as conn:
            row = await external_login.bind(
                conn, actor, user_id=id, provider=body.provider, external_id=body.external_id
            )
        return out.ExternalLogin.model_validate(row)

    @r.delete(
        f"{V}/users/{{id}}/external-logins/{{provider}}/{{external_id}}",
        tags=["iam"],
        operation_id="unbindExternalLogin",
        status_code=204,
    )
    async def unbind_external_login(
        id: uuid.UUID, provider: str, external_id: str, actor: ActorDep, db: DbDep
    ) -> Response:
        async with db.tx(actor.db_scope()) as conn:
            await external_login.unbind(conn, actor, user_id=id, provider=provider, external_id=external_id)
        return Response(status_code=204)

    @r.get(
        f"{V}/users/{{id}}/webauthn/credentials",
        tags=["auth"],
        operation_id="listWebAuthnCredentials",
        response_model=Page[out.WebAuthnCredential],
    )
    async def list_webauthn(id: uuid.UUID, actor: ActorDep, db: DbDep) -> Page[out.WebAuthnCredential]:
        actor.require(actor.user_id == id or actor.has("platform_admin"))
        async with db.tx(actor.db_scope()) as conn:
            items = [out.WebAuthnCredential.model_validate(x) for x in await webauthn.list_credentials(conn, id)]
        return Page(items=items)

    @r.post(f"{V}/users/{{id}}/webauthn/register/begin", tags=["auth"], operation_id="webauthnRegisterBegin")
    async def webauthn_register_begin(id: uuid.UUID, actor: ActorDep, db: DbDep) -> dict[str, Any]:
        async with db.tx(actor.db_scope()) as conn:
            return await webauthn.begin_registration(conn, actor, id)

    @r.post(
        f"{V}/users/{{id}}/webauthn/register/finish",
        tags=["auth"],
        operation_id="webauthnRegisterFinish",
        status_code=201,
        response_model=out.WebAuthnCredential,
    )
    async def webauthn_register_finish(
        id: uuid.UUID, body: WebAuthnFinishIn, actor: ActorDep, db: DbDep
    ) -> out.WebAuthnCredential:
        async with db.tx(actor.db_scope()) as conn:
            row = await webauthn.finish_registration(
                conn,
                actor,
                id,
                challenge_id=body.challenge_id,
                credential_id=body.credential_id,
                public_key_cose_b64=body.public_key_cose_b64,
                nickname=body.nickname,
            )
        return out.WebAuthnCredential.model_validate(row)

    @r.post(f"{V}/auth/webauthn/authenticate/begin", tags=["auth"], operation_id="webauthnAuthenticateBegin")
    async def webauthn_auth_begin(body: WebAuthnAuthBeginIn, db: DbDep) -> dict[str, Any]:
        async with db.tx(DbScope.system()) as conn:
            return await webauthn.begin_authentication(conn, body.login_name)

    @r.post(f"{V}/auth/webauthn/authenticate/finish", tags=["auth"], operation_id="webauthnAuthenticateFinish")
    async def webauthn_auth_finish(body: WebAuthnAuthFinishIn, db: DbDep) -> dict[str, Any]:
        """stub：校验挑战后返回 user_id；完整登录仍须口令或后续与 session 衔接。"""
        async with db.tx(DbScope.system()) as conn:
            uid = await webauthn.stub_verify_authentication(
                conn,
                challenge_id=body.challenge_id,
                credential_id=body.credential_id,
                sign_count=body.sign_count,
            )
        return {"user_id": str(uid), "verified": True}

    @r.get(
        f"{V}/companies/{{id}}/business-roles",
        tags=["iam"],
        operation_id="listBusinessRoles",
        response_model=Page[out.BusinessRole],
    )
    async def list_business_roles(id: uuid.UUID, actor: ActorDep, db: DbDep) -> Page[out.BusinessRole]:
        async with db.tx(actor.db_scope()) as conn:
            items = [out.BusinessRole.model_validate(x) for x in await business_roles.list_roles(conn, id)]
        return Page(items=items)

    @r.post(
        f"{V}/companies/{{id}}/business-roles",
        tags=["iam"],
        operation_id="createBusinessRole",
        status_code=201,
        response_model=out.BusinessRole,
    )
    async def create_business_role(
        id: uuid.UUID, body: BusinessRoleIn, actor: ActorDep, db: DbDep
    ) -> out.BusinessRole:
        async with db.tx(actor.db_scope()) as conn:
            return out.BusinessRole.model_validate(
                await business_roles.create(conn, actor, id, body.code, body.name, body.description)
            )

    @r.delete(
        f"{V}/companies/{{id}}/business-roles/{{code}}",
        tags=["iam"],
        operation_id="deleteBusinessRole",
        status_code=204,
    )
    async def delete_business_role(
        id: uuid.UUID, code: str, actor: ActorDep, db: DbDep, response: Response
    ) -> None:
        async with db.tx(actor.db_scope()) as conn:
            await business_roles.delete(conn, actor, id, code)
        response.status_code = 204

    @r.get(
        f"{V}/clearance-requests",
        tags=["iam"],
        operation_id="listClearanceRequests",
        response_model=Page[out.ClearanceRequest],
    )
    async def list_clearance_requests(
        actor: ActorDep, db: DbDep, status: str | None = "pending"
    ) -> Page[out.ClearanceRequest]:
        async with db.tx(actor.db_scope()) as conn:
            items = [
                out.ClearanceRequest.model_validate(x)
                for x in await users.list_clearance_requests(conn, actor, status=status)
            ]
        return Page(items=items)

    @r.post(
        f"{V}/clearance-requests",
        tags=["iam"],
        operation_id="requestClearance",
        status_code=201,
        response_model=out.ClearanceRequest,
    )
    async def request_clearance(body: ClearanceIn, actor: ActorDep, db: DbDep) -> out.ClearanceRequest:
        async with db.tx(actor.db_scope()) as conn:
            rid = await users.request_clearance(
                conn, actor, body.user_id, body.requested_level, body.reason, body.expires_at
            )
        return out.ClearanceRequest.model_validate(
            {"id": str(rid), "status": "pending", **body.model_dump(mode="json")}
        )

    @r.post(
        f"{V}/clearance-requests/{{id}}/decide",
        tags=["iam"],
        operation_id="decideClearance",
        response_model=out.ClearanceDecision,
    )
    async def decide_clearance(
        id: uuid.UUID, body: DecisionIn, actor: ActorDep, db: DbDep
    ) -> out.ClearanceDecision:
        async with db.tx(actor.db_scope()) as conn:
            status = await users.decide_clearance(conn, actor, id, body.decision == "approve", body.comment)
        return out.ClearanceDecision(id=str(id), status=status)

    @r.get(f"{V}/consumers", tags=["iam"], operation_id="listConsumers", response_model=Page[out.Consumer])
    async def list_consumers(actor: ActorDep, db: DbDep) -> Page[out.Consumer]:
        async with db.tx(actor.db_scope()) as conn:
            rows = (await conn.execute(select(t.consumer).order_by(t.consumer.c.client_id))).mappings().all()
        items = [
            out.Consumer.model_validate({k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in row.items()})
            for row in rows
        ]
        return Page(items=items)

    @r.put(f"{V}/consumers/{{id}}/quota", tags=["iam"], operation_id="setConsumerQuota")
    async def set_consumer_quota(id: uuid.UUID, body: dict[str, Any], actor: ActorDep, db: DbDep) -> dict[str, Any]:
        """消费方每日配额：{"daily_calls": N, "daily_writes": N}；空对象表示不限。"""
        async with db.tx(actor.db_scope()) as conn:
            return {"consumer_id": str(id), "quota": await consumers.set_quota(conn, actor, id, body)}

    @r.post(
        f"{V}/consumers",
        tags=["iam"],
        operation_id="createConsumer",
        status_code=201,
        response_model=out.Consumer,
    )
    async def create_consumer(body: ConsumerIn, actor: ActorDep, db: DbDep) -> out.Consumer:
        async with db.tx(actor.db_scope()) as conn:
            cid = await consumers.create_consumer(
                conn,
                actor,
                client_id=body.client_id,
                company_id=body.company_id,
                clearance_cap=body.clearance_cap,
                bind_policy=body.bind_policy,
                contact=body.contact,
            )
        return out.Consumer.model_validate(
            {"id": str(cid), **body.model_dump(mode="json"), "status": "active"}
        )

    @r.get(
        f"{V}/consumers/{{id}}/credentials",
        tags=["iam"],
        operation_id="listCredentials",
        response_model=Page[out.CredentialMeta],
    )
    async def list_credentials(id: uuid.UUID, actor: ActorDep, db: DbDep) -> Page[out.CredentialMeta]:
        async with db.tx(actor.db_scope()) as conn:
            items = [out.CredentialMeta.model_validate(x) for x in await consumers.list_credentials(conn, actor, id)]
        return Page(items=items)

    @r.post(
        f"{V}/consumers/{{id}}/credentials",
        tags=["iam"],
        operation_id="issueCredential",
        status_code=201,
        response_model=out.CredentialIssued,
    )
    async def issue_credential(
        id: uuid.UUID, body: CredentialIn, actor: ActorDep, db: DbDep
    ) -> out.CredentialIssued:
        async with db.tx(actor.db_scope()) as conn:
            cred_id, token, expires = await consumers.issue_static_token(conn, actor, id, body.days, body.ip_binding)
        return out.CredentialIssued(
            id=str(cred_id), kind=body.kind, token=token, expires_at=expires.isoformat(), status="active"
        )
    @r.post(f"{V}/credentials/{{id}}/revoke", tags=["iam"], operation_id="revokeCredential", status_code=204)
    async def revoke_credential(id: uuid.UUID, actor: ActorDep, db: DbDep) -> Response:
        async with db.tx(actor.db_scope()) as conn:
            await consumers.revoke_credential(conn, actor, id)
        return Response(status_code=204)

    @r.post(f"{V}/consumers/{{id}}/jwks", tags=["iam"], operation_id="addConsumerJwk", status_code=201)
    async def add_jwk(id: uuid.UUID, jwk: dict[str, Any], actor: ActorDep, db: DbDep) -> dict[str, Any]:
        async with db.tx(actor.db_scope()) as conn:
            await consumers.add_jwk(conn, actor, id, jwk)
        return jwk

    @r.get(
        f"{V}/consumers/{{id}}/bindings",
        tags=["iam"],
        operation_id="listBindings",
        response_model=Page[out.UserBinding],
    )
    async def list_bindings(id: uuid.UUID, actor: ActorDep, db: DbDep) -> Page[out.UserBinding]:
        async with db.tx(actor.db_scope()) as conn:
            rows = (
                (await conn.execute(select(t.user_binding).where(t.user_binding.c.consumer_id == id).limit(200)))
                .mappings()
                .all()
            )
        items = [
            out.UserBinding.model_validate(
                {k: (str(v) if isinstance(v, uuid.UUID | dt.datetime) else v) for k, v in row.items()}
            )
            for row in rows
        ]
        return Page(items=items)

    @r.post(
        f"{V}/consumers/{{id}}/bindings",
        tags=["iam"],
        operation_id="createBinding",
        status_code=201,
        response_model=out.UserBinding,
    )
    async def create_binding(id: uuid.UUID, body: BindingIn, actor: ActorDep, db: DbDep) -> out.UserBinding:
        async with db.tx(actor.db_scope()) as conn:
            bid = await consumers.create_binding(conn, actor, id, body.external_subject, body.user_id)
        return out.UserBinding.model_validate(
            {"id": str(bid), "consumer_id": str(id), **body.model_dump(mode="json")}
        )

    @r.post(
        f"{V}/assertion-debug",
        tags=["iam"],
        operation_id="debugAssertion",
        response_model=out.AssertionDebugResult,
    )
    async def debug_assertion(body: DebugIn, actor: ActorDep, db: DbDep) -> out.AssertionDebugResult:
        """断言调试器：验签、校验声明、查绑定；不消耗 jti、不记录断言内容（architecture.md 3.1）。"""
        s = get_settings()
        async with db.tx(actor.db_scope()) as conn:
            c = await consumers.consumer_row(conn, body.consumer_id)
            actor.require(actor.manages_org(c["company_id"]))
            keys = {
                k["kid"]: k["jwk"]
                for k in (
                    await conn.execute(
                        select(t.consumer_jwk).where(
                            (t.consumer_jwk.c.consumer_id == body.consumer_id) & (t.consumer_jwk.c.status == "active")
                        )
                    )
                ).mappings()
            }
            result: dict[str, Any] = {
                "signature_valid": False,
                "claims_valid": False,
                "binding": None,
                "errors": [],
                "principal": None,
            }
            try:
                v = await verify_assertion(
                    body.assertion,
                    client_id=c["client_id"],
                    jwks=keys,
                    audiences=[s.token_endpoint_aud, s.mcp_resource, s.issuer],
                    replay=MemoryReplayStore(),
                    max_ttl_s=s.assertion_max_ttl_s,
                )
            except PlatformError as e:
                result["errors"].append(f"{e.code}: {e.detail}")
                return out.AssertionDebugResult.model_validate(result)
            result.update(signature_valid=True, claims_valid=True)
            row = (
                await conn.execute(
                    select(t.user_binding.c.user_id).where(
                        (t.user_binding.c.consumer_id == body.consumer_id)
                        & (t.user_binding.c.external_subject == v.subject)
                    )
                )
            ).first()
            if row is None:
                result["errors"].append(f"USER_NOT_BOUND（bind_policy={c['bind_policy']}）")
                return out.AssertionDebugResult.model_validate(result)
            result["binding"] = str(row[0])
            u = await users.user_row(conn, row[0])
            result["principal"] = {
                "identity_kind": "user",
                "user_id": str(row[0]),
                "company_id": str(u["company_id"]),
                "clearance": min(await users.effective_clearance(conn, row[0]), int(c["clearance_cap"])),
                "client_id": c["client_id"],
                "status": u["status"],
            }
        return out.AssertionDebugResult.model_validate(result)

    @r.get(f"{V}/snapshots/iam", tags=["snapshot"], operation_id="pullIamSnapshot", response_model=None)
    async def pull_iam(
        request: Request,
        db: DbDep,
        after_seq: int = 0,
        wait_s: float = 0,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | dict[str, Any]:
        from aip.libs.serviceauth import require_service_auth

        require_service_auth(authorization)

        async def fetch(conn: AsyncConnection) -> dict[str, Any] | None:
            await snapshot.publish(conn, platform_key())
            return await snapshot.latest(conn, after_seq)

        snap = await request.app.state.snapshot_feed.serve(fetch, wait_s)
        return snap if snap is not None else Response(status_code=204)

    return r
