"""Notifier 运维接口：flush outbox、控制台邮件配置与测试发送。"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from aip.connectors.credentials import OpenBaoSecretStore, SecretStore
from aip.control_plane.iam.api import ActorDep, DbDep
from aip.libs.common.config import get_settings
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.notify import mail_settings as ms
from aip.libs.notify.service import SmtpMailChannel, build_notifier_resolved, flush_outbox

V = "/api/v1"


class NotifyFlushResult(BaseModel):
    sent: int = Field(ge=0)


class MailSettingsOut(BaseModel):
    enabled: bool
    host: str
    port: int
    user: str
    mail_from: str
    starttls: bool
    subject_template: str
    public_base_url: str
    credential_ref: str
    has_password: bool


class MailSettingsIn(BaseModel):
    enabled: bool = False
    host: str = ""
    port: int = Field(default=587, ge=1, le=65535)
    user: str = ""
    mail_from: str = ""
    password: str | None = None  # 空=不改密码
    starttls: bool = True
    subject_template: str = ms.DEFAULT_SUBJECT_TEMPLATE
    public_base_url: str = ""


class MailTestIn(BaseModel):
    to: str = Field(min_length=3, max_length=200)


class MailTestResult(BaseModel):
    ok: bool
    detail: str


def _store() -> SecretStore:
    s = get_settings()
    return OpenBaoSecretStore(s.openbao_addr, s.openbao_token)


def router() -> APIRouter:
    r = APIRouter()

    @r.post(
        f"{V}/notify/flush",
        tags=["notify"],
        operation_id="notifyFlush",
        response_model=NotifyFlushResult,
    )
    async def notify_flush(actor: ActorDep, db: DbDep) -> NotifyFlushResult:
        actor.require(actor.has("platform_admin"), "只有平台管理员可以触发通知投递")
        settings = get_settings()
        n = await build_notifier_resolved(settings, _store())
        sent = await flush_outbox(db.engine, n)
        return NotifyFlushResult(sent=sent)

    @r.get(
        f"{V}/notify/mail-settings",
        tags=["notify"],
        operation_id="getMailSettings",
        response_model=MailSettingsOut,
    )
    async def get_mail_settings(actor: ActorDep) -> MailSettingsOut:
        actor.require(actor.has("platform_admin"), "只有平台管理员可以查看邮件配置")
        settings = get_settings()
        meta = ms.load_meta(settings)
        has_pw = await ms.has_password(_store(), settings)
        return MailSettingsOut.model_validate(ms.public_view(meta, has_password_flag=has_pw))

    @r.put(
        f"{V}/notify/mail-settings",
        tags=["notify"],
        operation_id="putMailSettings",
        response_model=MailSettingsOut,
    )
    async def put_mail_settings(body: MailSettingsIn, actor: ActorDep) -> MailSettingsOut:
        actor.require(actor.has("platform_admin"), "只有平台管理员可以修改邮件配置")
        settings = get_settings()
        store = _store()
        if body.enabled and not str(body.host or "").strip():
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, "启用邮件时必须填写 SMTP 主机")
        meta = ms.save_meta(body.model_dump(exclude={"password"}), settings)
        if body.password is not None and body.password.strip():
            await ms.save_password(store, body.password, settings)
        has_pw = await ms.has_password(store, settings)
        if body.enabled and not has_pw and not (settings.smtp_password or "").strip():
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, "启用邮件时请填写 SMTP 密码（或配置 AIP_SMTP_PASSWORD）")
        return MailSettingsOut.model_validate(ms.public_view(meta, has_password_flag=has_pw))

    @r.post(
        f"{V}/notify/mail-settings/test",
        tags=["notify"],
        operation_id="testMailSettings",
        response_model=MailTestResult,
    )
    async def test_mail_settings(body: MailTestIn, actor: ActorDep) -> MailTestResult:
        actor.require(actor.has("platform_admin"), "只有平台管理员可以测试邮件发送")
        settings = get_settings()
        store = _store()
        meta = ms.load_meta(settings)
        use_console = bool(meta.get("enabled") and str(meta.get("host") or "").strip())
        host = str(meta.get("host") if use_console else settings.smtp_host or "").strip()
        if not host:
            raise PlatformError(ErrorCode.PRECONDITION_FAILED, "尚未配置 SMTP 主机")
        password = (await ms.resolve_password(store, settings)) if use_console else (settings.smtp_password or "")
        if use_console and not password:
            password = settings.smtp_password or ""
        if not password:
            raise PlatformError(ErrorCode.PRECONDITION_FAILED, "尚未配置 SMTP 密码")
        ch = SmtpMailChannel(
            host=host,
            port=int(meta.get("port") if use_console else settings.smtp_port),
            user=str(meta.get("user") if use_console else settings.smtp_user),
            password=password,
            mail_from=str(
                (meta.get("mail_from") or meta.get("user"))
                if use_console
                else (settings.smtp_from or settings.smtp_user)
            ),
            starttls=bool(meta.get("starttls", True) if use_console else settings.smtp_starttls),
        )
        to = body.to.strip()
        subject = ms.format_subject("test", settings)
        link = ms.approve_url("00000000-0000-0000-0000-000000000000", settings)
        body_txt = (
            "这是申洲集团 AI 资产中台的邮件配置测试信。\n"
            f"若收到本邮件，说明 SMTP 可用。\n"
            f"审批深链示例：{link}\n"
        )
        try:
            await ch.send(to, subject, body_txt)
        except Exception as e:  # SMTP 失败应对用户可读，勿落成 500
            msg = str(e) or type(e).__name__
            if "STARTTLS" in msg or type(e).__name__ == "SMTPNotSupportedError":
                msg = "邮局不支持 STARTTLS，请关闭 STARTTLS 后保存再测（内网 25 端口常见）"
            raise PlatformError(ErrorCode.DOWNSTREAM_UNAVAILABLE, f"发送失败：{msg}") from e
        return MailTestResult(ok=True, detail="已发送测试邮件")

    return r
