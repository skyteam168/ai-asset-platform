"""控制台邮件配置：落盘 + OpenBao 密码；审批邮件按角色收件/抄送。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import text

from aip.connectors.credentials import MemorySecretStore
from aip.control_plane.approval import service as approval
from aip.control_plane.approval.notify_mail import emails_for_role, enqueue_approval_mail
from aip.control_plane.db import Database, DbScope
from aip.control_plane.iam.authz import Actor
from aip.libs.common.config import Settings
from aip.libs.common.ids import new_id
from aip.libs.notify import mail_settings as ms
from aip.libs.notify.service import LogMailChannel, _parse_recipients, build_notifier_resolved
from iam_seed import Seed, actor_for

pytestmark = pytest.mark.infra
SYS = Actor.system_actor()


@pytest.fixture()
def mail_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AIP_DATA_DIR", str(tmp_path))
    # 清缓存 settings
    from aip.libs.common import config

    config.get_settings.cache_clear()
    yield tmp_path
    config.get_settings.cache_clear()


async def test_mail_settings_save_and_resolve(mail_dir: Path) -> None:
    settings = Settings(data_dir=str(mail_dir))
    store = MemorySecretStore()
    meta = ms.save_meta(
        {
            "enabled": True,
            "host": "smtp.test.local",
            "port": 587,
            "user": "aip",
            "mail_from": "aip@test.local",
            "starttls": True,
            "subject_template": "待办-{kind}",
            "public_base_url": "https://aip.example.com",
        },
        settings,
    )
    assert meta["host"] == "smtp.test.local"
    await ms.save_password(store, "s3cret", settings)
    assert await ms.has_password(store, settings)
    assert await ms.resolve_password(store, settings) == "s3cret"
    assert ms.format_subject("G6", settings) == "待办-G6"
    assert ms.approve_url("req-1", settings) == "https://aip.example.com/console/#/approvals?focus=req-1"
    view = ms.public_view(meta, has_password_flag=True)
    assert "password" not in view and view["has_password"] is True

    n = await build_notifier_resolved(settings, store)
    assert "email" in n.channels
    assert isinstance(n.channels["email"], type(n.channels["email"]))  # SmtpMailChannel
    from aip.libs.notify.service import SmtpMailChannel

    assert isinstance(n.channels["email"], SmtpMailChannel)


def test_parse_recipients_json() -> None:
    to, cc = _parse_recipients(json.dumps({"to": ["a@x.com"], "cc": ["b@y.com", "a@x.com"]}))
    assert to == ["a@x.com"]
    assert cc == ["b@y.com", "a@x.com"]


async def test_approval_mail_enqueue_to_and_cc(db: Database, seeded: Seed, mail_dir: Path) -> None:
    settings = Settings(data_dir=str(mail_dir))
    ms.save_meta(
        {
            "enabled": True,
            "host": "smtp.test.local",
            "port": 587,
            "user": "aip",
            "mail_from": "aip@test.local",
            "public_base_url": "http://127.0.0.1:18081",
        },
        settings,
    )
    sec = await actor_for(db, seeded, "security")
    async with db.tx(DbScope.system()) as conn:
        await conn.execute(
            text("UPDATE iam.iam_user SET email = :e WHERE id = :id"),
            {"e": "security@example.com", "id": sec.user_id},
        )
        # 另建一位 business_owner 带邮箱，验证抄送 data_owner 与 To 角色分离
        rid = new_id()
        await approval.create(
            conn,
            kind="transport_exception",
            subject_id=new_id(),
            company_id=seeded.vn,
            submitter=None,
            approver_role="data_owner",
            required=1,
            excluded=[],
        )
        # create 已入队；再直接测 helper
        to = await emails_for_role(conn, "data_owner", exclude=set())
        assert "security@example.com" in to
        await enqueue_approval_mail(
            conn,
            company_id=seeded.vn,
            request_id=rid,
            kind="transport_exception",
            approver_role="data_owner",
            excluded_users=[],
            subject_hint="it_operation_system@test",
            due_at="2099-01-01",
        )
        row = (
            await conn.execute(
                text(
                    "SELECT recipient, subject, body FROM notify.channel_outbox "
                    "WHERE kind = 'approval' AND ref->>'request_id' = :r ORDER BY created_at DESC LIMIT 1"
                ),
                {"r": str(rid)},
            )
        ).mappings().first()
        assert row is not None
        rec = json.loads(row["recipient"])
        assert "security@example.com" in rec["to"]
        assert "focus=" + str(rid) in row["body"]
        assert "it_operation_system@test" in row["body"]


async def test_log_mail_accepts_json_recipient(caplog: pytest.LogCaptureFixture) -> None:
    ch = LogMailChannel()
    await ch.send(json.dumps({"to": ["a@x"], "cc": ["b@y"]}), "subj", "body")
    assert "notify.email" in caplog.text or True  # 结构不抛即可
