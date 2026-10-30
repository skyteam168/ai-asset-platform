"""平台级 LLM 提供方：OpenAI 兼容 base_url + OpenBao 中的 API Key（architecture.md 8.6）。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from aip.connectors.credentials import SecretStore
from aip.data_plane.llm.openai_compat import list_models
from aip.libs.common.config import Settings, get_settings
from aip.libs.common.errors import ErrorCode, PlatformError

# 与连接器凭据同形；密钥只进 OpenBao，不进日志
DEFAULT_CREDENTIAL_REF = "kv/data/aip/llm/default"
_PROVIDER_FILE = "llm-provider.json"
_NAME_SAFE = re.compile(r"[^a-z0-9_.]+")


def provider_path(settings: Settings | None = None) -> Path:
    s = settings or get_settings()
    return Path(s.data_dir) / _PROVIDER_FILE


def load_meta(settings: Settings | None = None) -> dict[str, Any]:
    path = provider_path(settings)
    if not path.is_file():
        return {"base_url": "", "credential_ref": DEFAULT_CREDENTIAL_REF, "models_path": "/models"}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"base_url": "", "credential_ref": DEFAULT_CREDENTIAL_REF, "models_path": "/models"}
    if not isinstance(raw, dict):
        return {"base_url": "", "credential_ref": DEFAULT_CREDENTIAL_REF, "models_path": "/models"}
    return {
        "base_url": str(raw.get("base_url") or ""),
        "credential_ref": str(raw.get("credential_ref") or DEFAULT_CREDENTIAL_REF),
        "models_path": str(raw.get("models_path") or "/models"),
        "synced_models": list(raw.get("synced_models") or []),
        "synced_at": raw.get("synced_at"),
    }


def save_meta(
    *,
    base_url: str,
    credential_ref: str = DEFAULT_CREDENTIAL_REF,
    models_path: str = "/models",
    synced_models: list[dict[str, Any]] | None = None,
    synced_at: str | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    s = settings or get_settings()
    path = provider_path(s)
    path.parent.mkdir(parents=True, exist_ok=True)
    prev = load_meta(s)
    doc = {
        "base_url": base_url.strip().rstrip("/"),
        "credential_ref": credential_ref or DEFAULT_CREDENTIAL_REF,
        "models_path": models_path or "/models",
        "synced_models": synced_models if synced_models is not None else prev.get("synced_models") or [],
        "synced_at": synced_at if synced_at is not None else prev.get("synced_at"),
    }
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return doc


async def resolve_api_key(store: SecretStore, settings: Settings | None = None) -> str | None:
    """优先 OpenBao credential_ref；否则回落 AIP_LLM_API_KEY。"""
    s = settings or get_settings()
    meta = load_meta(s)
    ref = str(meta.get("credential_ref") or DEFAULT_CREDENTIAL_REF)
    try:
        secret = await store.get(ref)
        key = (secret.get("api_key") or secret.get("token") or "").strip()
        if key:
            return key
    except PlatformError:
        pass
    env_key = (s.llm_api_key or "").strip()
    return env_key or None


async def has_api_key(store: SecretStore, settings: Settings | None = None) -> bool:
    return bool(await resolve_api_key(store, settings))


async def save_api_key(store: SecretStore, api_key: str, settings: Settings | None = None) -> str:
    s = settings or get_settings()
    meta = load_meta(s)
    ref = str(meta.get("credential_ref") or DEFAULT_CREDENTIAL_REF)
    key = api_key.strip()
    if not key:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "api_key 不能为空")
    await store.put(ref, {"api_key": key})
    return ref


def profile_name_from_model(model_id: str) -> str:
    """把上游 model id 收成 registry / Gateway 可用的 name（小写 + [a-z0-9_.]）。"""
    raw = (model_id or "").strip().lower().replace("-", "_")
    raw = _NAME_SAFE.sub("_", raw).strip("._")
    if not raw or not raw[0].isalpha():
        raw = f"m_{raw}" if raw else "model"
    return raw[:64]


async def sync_upstream_models(
    store: SecretStore,
    *,
    settings: Settings | None = None,
    transport: Any = None,
) -> list[dict[str, Any]]:
    s = settings or get_settings()
    meta = load_meta(s)
    base = str(meta.get("base_url") or "").strip()
    if not base:
        raise PlatformError(ErrorCode.PRECONDITION_FAILED, "请先配置 LLM 提供方 base_url")
    key = await resolve_api_key(store, s)
    if not key:
        raise PlatformError(ErrorCode.PRECONDITION_FAILED, "请先填写 API Key（或设置 AIP_LLM_API_KEY）")
    models = await list_models(
        base_url=base,
        api_key=key,
        models_path=str(meta.get("models_path") or "/models"),
        transport=transport,
    )
    slim: list[dict[str, Any]] = []
    for m in models:
        mid = m.get("id")
        if not isinstance(mid, str) or not mid.strip():
            continue
        slim.append(
            {
                "id": mid.strip(),
                "owned_by": m.get("owned_by"),
                "created": m.get("created"),
                "suggested_profile": profile_name_from_model(mid),
            }
        )
    slim.sort(key=lambda x: x["id"])
    from aip.libs.common.ids import utcnow

    save_meta(
        base_url=base,
        credential_ref=str(meta.get("credential_ref") or DEFAULT_CREDENTIAL_REF),
        models_path=str(meta.get("models_path") or "/models"),
        synced_models=slim,
        synced_at=utcnow().isoformat(),
        settings=s,
    )
    return slim
