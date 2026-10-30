"""公司 LLM 白名单 / 日预算 + 平台提供方与 model_profile 快捷创建。"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from typing import Any

import httpx
from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from aip.connectors.credentials import OpenBaoSecretStore, SecretStore
from aip.control_plane.iam.api import ActorDep, DbDep
from aip.control_plane.iam.orgs import get_org
from aip.control_plane.llm import provider as llm_provider
from aip.control_plane.llm import schemas as out
from aip.control_plane.registry import service as registry
from aip.control_plane.registry import tables as rt
from aip.data_plane.llm import allowlist as al
from aip.data_plane.llm import budget as bd
from aip.libs.common.config import get_settings
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.common.paging import Page

V = "/api/v1"
_SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")


def _company(org: Any) -> None:
    if org.type != "company":
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "id 须为公司")


def _secrets(request: Request) -> SecretStore:
    s: SecretStore | None = getattr(request.app.state, "secrets", None)
    if s is None:
        settings = get_settings()
        s = OpenBaoSecretStore(settings.openbao_addr, settings.openbao_token)
        request.app.state.secrets = s
    return s


class AllowlistIn(BaseModel):
    names: list[str] = Field(default_factory=list)


class BudgetIn(BaseModel):
    daily_tokens: int = Field(ge=0)


class ProviderIn(BaseModel):
    base_url: str = Field(min_length=8, max_length=500)
    api_key: str | None = Field(default=None, max_length=2048)
    models_path: str = Field(default="/models", max_length=128)


class CreateProfileIn(BaseModel):
    model_id: str = Field(min_length=1, max_length=200)
    name: str | None = Field(default=None, max_length=64)
    version: str = Field(default="1.0.0", max_length=32)
    owner_org_id: uuid.UUID
    temperature: float = Field(default=0, ge=0, le=2)
    max_tokens: int = Field(default=1024, ge=1, le=128000)
    add_to_allowlist_company_id: uuid.UUID | None = None


def router() -> APIRouter:
    r = APIRouter(tags=["llm"])

    @r.get(
        f"{V}/llm/provider",
        operation_id="getLlmProvider",
        response_model=out.LlmProvider,
    )
    async def get_provider(actor: ActorDep, request: Request) -> out.LlmProvider:
        actor.require(actor.has("platform_admin"), "只有平台管理员可以查看 LLM 提供方")
        meta = llm_provider.load_meta()
        store = _secrets(request)
        keyed = await llm_provider.has_api_key(store)
        hint = None
        if keyed:
            key = await llm_provider.resolve_api_key(store)
            if key and len(key) >= 8:
                hint = f"{key[:3]}***{key[-4:]}"
            elif key:
                hint = "***"
        synced = list(meta.get("synced_models") or [])
        return out.LlmProvider(
            base_url=str(meta.get("base_url") or ""),
            credential_ref=str(meta.get("credential_ref") or llm_provider.DEFAULT_CREDENTIAL_REF),
            models_path=str(meta.get("models_path") or "/models"),
            has_api_key=keyed,
            api_key_hint=hint,
            synced_at=meta.get("synced_at") if isinstance(meta.get("synced_at"), str) else None,
            synced_count=len(synced),
        )

    @r.put(
        f"{V}/llm/provider",
        operation_id="putLlmProvider",
        response_model=out.LlmProvider,
    )
    async def put_provider(body: ProviderIn, actor: ActorDep, request: Request) -> out.LlmProvider:
        actor.require(actor.has("platform_admin"), "只有平台管理员可以配置 LLM 提供方")
        base = body.base_url.strip()
        if not (base.startswith("https://") or base.startswith("http://")):
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, "base_url 须为 http(s) URL")
        # 生产建议 https；本地 http 仅开发
        meta = llm_provider.save_meta(
            base_url=base,
            models_path=body.models_path.strip() or "/models",
        )
        store = _secrets(request)
        if body.api_key and body.api_key.strip():
            await llm_provider.save_api_key(store, body.api_key)
        keyed = await llm_provider.has_api_key(store)
        hint = None
        if keyed:
            key = await llm_provider.resolve_api_key(store)
            if key and len(key) >= 8:
                hint = f"{key[:3]}***{key[-4:]}"
            elif key:
                hint = "***"
        synced = list(meta.get("synced_models") or [])
        return out.LlmProvider(
            base_url=str(meta.get("base_url") or ""),
            credential_ref=str(meta.get("credential_ref") or llm_provider.DEFAULT_CREDENTIAL_REF),
            models_path=str(meta.get("models_path") or "/models"),
            has_api_key=keyed,
            api_key_hint=hint,
            synced_at=meta.get("synced_at") if isinstance(meta.get("synced_at"), str) else None,
            synced_count=len(synced),
        )

    @r.post(
        f"{V}/llm/provider/sync-models",
        operation_id="syncLlmModels",
        response_model=Page[out.LlmUpstreamModel],
    )
    async def sync_models(actor: ActorDep, request: Request) -> Page[out.LlmUpstreamModel]:
        actor.require(actor.has("platform_admin"), "只有平台管理员可以同步 LLM 模型")
        store = _secrets(request)
        transport: httpx.AsyncBaseTransport | None = getattr(request.app.state, "llm_transport", None)
        items = await llm_provider.sync_upstream_models(store, transport=transport)
        return Page(items=[out.LlmUpstreamModel.model_validate(i) for i in items])

    @r.get(
        f"{V}/llm/provider/synced-models",
        operation_id="listSyncedLlmModels",
        response_model=Page[out.LlmUpstreamModel],
    )
    async def list_synced(actor: ActorDep) -> Page[out.LlmUpstreamModel]:
        actor.require(actor.has("platform_admin"), "只有平台管理员可以查看同步模型")
        meta = llm_provider.load_meta()
        items = list(meta.get("synced_models") or [])
        return Page(items=[out.LlmUpstreamModel.model_validate(i) for i in items])

    @r.get(
        f"{V}/llm/model-profiles",
        operation_id="listLlmModelProfiles",
        response_model=Page[out.LlmModelProfileRow],
    )
    async def list_profiles(actor: ActorDep, db: DbDep) -> Page[out.LlmModelProfileRow]:
        actor.require(
            actor.has("platform_admin") or actor.has("asset_developer"),
            "需要平台管理员或资产开发者",
        )
        async with db.tx(actor.db_scope()) as conn:
            rows = (
                await conn.execute(
                    select(
                        rt.asset.c.id,
                        rt.asset.c.name,
                        rt.asset.c.company_id,
                        rt.asset_version.c.semver,
                        rt.asset_version.c.state,
                        rt.asset_version.c.spec,
                    )
                    .select_from(
                        rt.asset.outerjoin(
                            rt.asset_version,
                            rt.asset_version.c.asset_id == rt.asset.c.id,
                        )
                    )
                    .where(rt.asset.c.type == "model_profile")
                    .order_by(rt.asset.c.name, rt.asset_version.c.semver.desc())
                    .limit(500)
                )
            ).mappings().all()
        # 每个资产只取最新一条版本
        seen: set[str] = set()
        items: list[out.LlmModelProfileRow] = []
        for row in rows:
            aid = str(row["id"])
            if aid in seen:
                continue
            seen.add(aid)
            spec = row["spec"] if isinstance(row["spec"], dict) else {}
            items.append(
                out.LlmModelProfileRow(
                    asset_id=aid,
                    name=str(row["name"]),
                    version=str(row["semver"]) if row["semver"] else None,
                    model=str(spec["model"]) if spec.get("model") else None,
                    base_url=str(spec["base_url"]) if spec.get("base_url") else None,
                    status=str(row["state"]) if row["state"] else None,
                    company_id=str(row["company_id"]) if row["company_id"] else None,
                )
            )
        return Page(items=items)

    @r.post(
        f"{V}/llm/model-profiles",
        operation_id="createLlmModelProfile",
        status_code=201,
        response_model=out.LlmModelProfileCreated,
    )
    async def create_profile(
        body: CreateProfileIn, actor: ActorDep, db: DbDep, request: Request
    ) -> out.LlmModelProfileCreated:
        actor.require(
            actor.has("platform_admin") or actor.has("asset_developer"),
            "需要平台管理员或资产开发者",
        )
        if not _SEMVER.match(body.version.strip()):
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, "version 须为 semver")
        meta = llm_provider.load_meta()
        base = str(meta.get("base_url") or "").strip()
        if not base:
            raise PlatformError(ErrorCode.PRECONDITION_FAILED, "请先配置 LLM 提供方 base_url")
        name = (body.name or llm_provider.profile_name_from_model(body.model_id)).strip()
        if not re.match(r"^[a-z][a-z0-9_.]{0,63}$", name):
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, f"资产名不合法：{name}")
        # OpenAI 兼容：chat 路径相对 compatible-mode/v1 → /chat/completions
        api_path = "/chat/completions" if base.rstrip("/").endswith("/v1") else "/v1/chat/completions"
        cred = str(meta.get("credential_ref") or llm_provider.DEFAULT_CREDENTIAL_REF)
        spec = {
            "model": body.model_id.strip(),
            "base_url": base,
            "temperature": body.temperature,
            "max_tokens": body.max_tokens,
            "api_path": api_path,
            "credential_ref": cred,
            "deployment": "openai-compat",
        }
        async with db.tx(actor.db_scope()) as conn:
            existing = (
                await conn.execute(select(rt.asset.c.id).where(rt.asset.c.name == name))
            ).first()
            if existing:
                aid = existing[0]
                # 同名已存在：尝试新建版本
                vid = await registry.create_version(conn, actor, aid, body.version.strip(), spec)
            else:
                aid = await registry.create_asset(
                    conn,
                    actor,
                    type_="model_profile",
                    name=name,
                    owner_org_id=body.owner_org_id,
                    classification=2,
                )
                vid = await registry.create_version(conn, actor, aid, body.version.strip(), spec)
            if body.add_to_allowlist_company_id is not None:
                cid = body.add_to_allowlist_company_id
                _company(await get_org(conn, cid))
                names = await al.list_names_on_conn(conn, cid)
                if name not in names:
                    await al.set_names_on_conn(conn, cid, [*names, name])
        return out.LlmModelProfileCreated(
            asset_id=str(aid),
            version_id=str(vid),
            name=name,
            version=body.version.strip(),
            ref=f"{name}@{body.version.strip()}",
            spec=spec,
        )

    @r.get(
        f"{V}/companies/{{id}}/llm-allowlist",
        operation_id="getLlmAllowlist",
        response_model=out.LlmAllowlist,
    )
    async def get_allowlist(id: uuid.UUID, actor: ActorDep, db: DbDep) -> out.LlmAllowlist:
        actor.require(actor.has("platform_admin"), "只有平台管理员可以查看 LLM 白名单")
        async with db.tx(actor.db_scope()) as conn:
            _company(await get_org(conn, id))
            names = await al.list_names_on_conn(conn, id)
        return out.LlmAllowlist(company_id=str(id), names=names)

    @r.put(
        f"{V}/companies/{{id}}/llm-allowlist",
        operation_id="putLlmAllowlist",
        response_model=out.LlmAllowlist,
    )
    async def put_allowlist(id: uuid.UUID, body: AllowlistIn, actor: ActorDep, db: DbDep) -> out.LlmAllowlist:
        actor.require(actor.has("platform_admin"), "只有平台管理员可以配置 LLM 白名单")
        async with db.tx(actor.db_scope()) as conn:
            _company(await get_org(conn, id))
            await al.set_names_on_conn(conn, id, body.names)
            names = await al.list_names_on_conn(conn, id)
        return out.LlmAllowlist(company_id=str(id), names=names)

    @r.get(
        f"{V}/companies/{{id}}/llm-budget",
        operation_id="getLlmBudget",
        response_model=out.LlmBudget,
    )
    async def get_budget(id: uuid.UUID, actor: ActorDep, db: DbDep) -> out.LlmBudget:
        actor.require(actor.has("platform_admin"), "只有平台管理员可以查看 LLM 预算")
        async with db.tx(actor.db_scope()) as conn:
            _company(await get_org(conn, id))
            daily = await bd.get_budget_on_conn(conn, id)
        return out.LlmBudget(company_id=str(id), daily_tokens=daily)

    @r.put(
        f"{V}/companies/{{id}}/llm-budget",
        operation_id="putLlmBudget",
        response_model=out.LlmBudget,
    )
    async def put_budget(id: uuid.UUID, body: BudgetIn, actor: ActorDep, db: DbDep) -> out.LlmBudget:
        actor.require(actor.has("platform_admin"), "只有平台管理员可以配置 LLM 预算")
        async with db.tx(actor.db_scope()) as conn:
            _company(await get_org(conn, id))
            await bd.set_budget_on_conn(conn, id, body.daily_tokens)
        return out.LlmBudget(company_id=str(id), daily_tokens=body.daily_tokens)

    @r.get(
        f"{V}/companies/{{id}}/llm-usage",
        operation_id="getLlmUsage",
        response_model=out.LlmUsage,
    )
    async def get_usage(id: uuid.UUID, actor: ActorDep, db: DbDep) -> out.LlmUsage:
        actor.require(actor.has("platform_admin"), "只有平台管理员可以查看 LLM 用量")
        day = dt.datetime.now(dt.UTC).date()
        async with db.tx(actor.db_scope()) as conn:
            _company(await get_org(conn, id))
            used = await bd.get_usage_on_conn(conn, id, day)
            daily = await bd.get_budget_on_conn(conn, id)
        return out.LlmUsage(
            company_id=str(id),
            usage_date=day.isoformat(),
            tokens_used=used,
            daily_tokens=daily,
        )

    return r
