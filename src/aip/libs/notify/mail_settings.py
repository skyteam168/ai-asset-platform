"""控制台邮件配置（平台级）：非密字段落 data_dir；SMTP 密码进 OpenBao。

优先于环境变量 AIP_SMTP_*；未启用或未配主机时回退环境变量 / LogMail。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aip.connectors.credentials import SecretStore
from aip.libs.common.config import Settings, get_settings
from aip.libs.common.errors import ErrorCode, PlatformError

DEFAULT_CREDENTIAL_REF = "kv/data/aip/mail/smtp"
_SETTINGS_FILE = "mail-settings.json"
DEFAULT_SUBJECT_TEMPLATE = "审批待办：{kind}"


def settings_path(settings: Settings | None = None) -> Path:
    s = settings or get_settings()
    return Path(s.data_dir) / _SETTINGS_FILE


def load_meta(settings: Settings | None = None) -> dict[str, Any]:
    path = settings_path(settings)
    defaults: dict[str, Any] = {
        "enabled": False,
        "host": "",
        "port": 587,
        "user": "",
        "mail_from": "",
        "starttls": True,
        "subject_template": DEFAULT_SUBJECT_TEMPLATE,
        "public_base_url": "",
        "credential_ref": DEFAULT_CREDENTIAL_REF,
    }
    if not path.is_file():
        return defaults
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return defaults
    if not isinstance(raw, dict):
        return defaults
    out = {**defaults, **{k: raw[k] for k in defaults if k in raw}}
    out["port"] = int(out.get("port") or 587)
    out["enabled"] = bool(out.get("enabled"))
    out["starttls"] = bool(out.get("starttls", True))
    out["subject_template"] = str(out.get("subject_template") or DEFAULT_SUBJECT_TEMPLATE)
    return out


def save_meta(data: dict[str, Any], settings: Settings | None = None) -> dict[str, Any]:
    s = settings or get_settings()
    path = settings_path(s)
    path.parent.mkdir(parents=True, exist_ok=True)
    prev = load_meta(s)
    host = str(data.get("host") or "").strip()
    port = int(data.get("port") or 587)
    if port < 1 or port > 65535:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "SMTP 端口不合法")
    doc = {
        "enabled": bool(data.get("enabled")),
        "host": host,
        "port": port,
        "user": str(data.get("user") or "").strip(),
        "mail_from": str(data.get("mail_from") or "").strip(),
        "starttls": bool(data.get("starttls", True)),
        "subject_template": str(data.get("subject_template") or DEFAULT_SUBJECT_TEMPLATE).strip()
        or DEFAULT_SUBJECT_TEMPLATE,
        "public_base_url": str(data.get("public_base_url") or "").strip().rstrip("/"),
        "credential_ref": str(data.get("credential_ref") or prev.get("credential_ref") or DEFAULT_CREDENTIAL_REF),
    }
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return doc


async def save_password(store: SecretStore, password: str, settings: Settings | None = None) -> str:
    meta = load_meta(settings)
    ref = str(meta.get("credential_ref") or DEFAULT_CREDENTIAL_REF)
    pwd = password.strip()
    if not pwd:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "SMTP 密码不能为空")
    await store.put(ref, {"password": pwd})
    return ref


async def resolve_password(store: SecretStore, settings: Settings | None = None) -> str:
    """控制台 OpenBao 密码优先；否则 AIP_SMTP_PASSWORD。"""
    s = settings or get_settings()
    meta = load_meta(s)
    ref = str(meta.get("credential_ref") or DEFAULT_CREDENTIAL_REF)
    try:
        secret = await store.get(ref)
        pwd = (secret.get("password") or secret.get("smtp_password") or "").strip()
        if pwd:
            return pwd
    except PlatformError:
        pass
    return (s.smtp_password or "").strip()


async def has_password(store: SecretStore, settings: Settings | None = None) -> bool:
    return bool(await resolve_password(store, settings))


def format_subject(kind: str, settings: Settings | None = None) -> str:
    meta = load_meta(settings)
    tmpl = str(meta.get("subject_template") or DEFAULT_SUBJECT_TEMPLATE)
    try:
        return tmpl.format(kind=kind)[:500]
    except (KeyError, ValueError):
        return f"审批待办：{kind}"[:500]


def approve_url(request_id: str, settings: Settings | None = None) -> str:
    """深链：{public_base}/console/#/approvals?focus={id}（HashRouter）。"""
    meta = load_meta(settings)
    s = settings or get_settings()
    base = str(meta.get("public_base_url") or s.public_base_url or "").rstrip("/")
    if not base:
        base = "http://127.0.0.1:18081"
    return f"{base}/console/#/approvals?focus={request_id}"


def public_view(meta: dict[str, Any], *, has_password_flag: bool) -> dict[str, Any]:
    """API 出站：永不含密码明文。"""
    return {
        "enabled": bool(meta.get("enabled")),
        "host": str(meta.get("host") or ""),
        "port": int(meta.get("port") or 587),
        "user": str(meta.get("user") or ""),
        "mail_from": str(meta.get("mail_from") or ""),
        "starttls": bool(meta.get("starttls", True)),
        "subject_template": str(meta.get("subject_template") or DEFAULT_SUBJECT_TEMPLATE),
        "public_base_url": str(meta.get("public_base_url") or ""),
        "credential_ref": str(meta.get("credential_ref") or DEFAULT_CREDENTIAL_REF),
        "has_password": has_password_flag,
    }
