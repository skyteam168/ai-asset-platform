"""审批单邮件：按角色发给可处理人，抄送 data_owner；正文带深链。"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from aip.control_plane.iam import tables as it
from aip.libs.common.config import get_settings
from aip.libs.notify import enqueue, mail_settings as ms

log = logging.getLogger(__name__)


async def emails_for_role(conn: AsyncConnection, role: str, *, exclude: set[uuid.UUID]) -> list[str]:
    detail = (
        await conn.execute(
            select(it.iam_user.c.id, it.iam_user.c.email)
            .select_from(it.iam_user.join(it.user_role, it.user_role.c.user_id == it.iam_user.c.id))
            .where(
                and_(
                    it.user_role.c.role == role,
                    it.iam_user.c.email.is_not(None),
                    it.iam_user.c.email != "",
                )
            )
        )
    ).all()
    out: list[str] = []
    seen: set[str] = set()
    for uid, email in detail:
        if uid in exclude:
            continue
        e = str(email).strip()
        if e and e not in seen:
            seen.add(e)
            out.append(e)
    return out


async def enqueue_approval_mail(
    conn: AsyncConnection,
    *,
    company_id: uuid.UUID,
    request_id: uuid.UUID,
    kind: str,
    approver_role: str,
    excluded_users: list[uuid.UUID],
    subject_hint: str = "",
    due_at: str = "",
) -> None:
    """入队一封邮件：To=可处理角色，Cc=data_owner（去重）。失败不影响建单。"""
    try:
        settings = get_settings()
        meta = ms.load_meta(settings)
        # 未启用控制台邮件且无环境 SMTP 时仍入队（LogMail 可观测）
        exclude = set(excluded_users or [])
        to_list = await emails_for_role(conn, approver_role, exclude=exclude)
        cc_list = await emails_for_role(conn, "data_owner", exclude=exclude)
        cc_list = [e for e in cc_list if e not in set(to_list)]
        if not to_list and not cc_list:
            log.info("approval mail skipped: no emails for role=%s", approver_role)
            return
        if not to_list:
            # 仅有抄送时，把抄送提到 To，避免空 To
            to_list, cc_list = cc_list, []
        link = ms.approve_url(str(request_id), settings)
        subj = ms.format_subject(kind, settings)
        body = (
            f"您有新的审批待办。\n"
            f"类型：{kind}\n"
            f"审批角色：{approver_role}\n"
            f"对象：{subject_hint or request_id}\n"
            f"截止：{due_at or '-'}\n"
            f"\n"
            f"请点击处理（未登录将先登录）：\n{link}\n"
            f"\n"
            f"— 申洲集团 AI 资产中台\n"
        )
        recipient = json.dumps({"to": to_list, "cc": cc_list}, ensure_ascii=False)
        await enqueue(
            conn,
            company_id=company_id,
            channel="email",
            recipient=recipient,
            subject=subj,
            body=body,
            kind="approval",
            ref={
                "request_id": str(request_id),
                "kind": kind,
                "approve_url": link,
                "to": to_list,
                "cc": cc_list,
                "console_mail": bool(meta.get("enabled")),
            },
        )
    except Exception:
        log.exception("enqueue approval mail failed")
