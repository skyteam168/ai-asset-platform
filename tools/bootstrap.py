"""初始化集团根、总部公司与首个平台管理员（只运行一次）。

  python tools/bootstrap.py --group-code GROUP --hq-code HQ --admin-login admin --admin-password '<至少12位>'

输出管理员的 TOTP 绑定 URI（只显示一次）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aip.control_plane.db import Database, DbScope
from aip.control_plane.iam import orgs, users
from aip.control_plane.iam.authz import Actor


async def main(a: argparse.Namespace) -> None:
    db = Database()
    async with db.tx(DbScope.system()) as conn:
        group, hq = await orgs.bootstrap_group(
            conn, group_code=a.group_code, group_name=a.group_name, hq_code=a.hq_code, hq_name=a.hq_name
        )
        created = await users.create_user(
            conn,
            Actor.system_actor(),
            display_name="平台管理员",
            home_org_id=hq.id,
            login_name=a.admin_login,
            password=a.admin_password,
            login_enabled=True,
        )
        await users.assign_role(conn, Actor.system_actor(), created.id, "platform_admin", group.id)
    await db.dispose()
    print(f"group={group.id} hq={hq.id} admin={created.id}")
    print(f"TOTP（只显示一次，请立即导入验证器）：{created.totp_provisioning_uri}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--group-code", default="GROUP")
    p.add_argument("--group-name", default="集团")
    p.add_argument("--hq-code", default="HQ")
    p.add_argument("--hq-name", default="总部")
    p.add_argument("--admin-login", required=True)
    p.add_argument("--admin-password", required=True)
    asyncio.run(main(p.parse_args()))
