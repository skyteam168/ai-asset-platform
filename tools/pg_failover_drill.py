#!/usr/bin/env python3
"""本地 PostgreSQL 主从切换演练编排（architecture.md 12.2）。

不替代 CI 用例 test_m7_pg_failover.py；用于人工按 docs/ops/pg-failover.md 操作时打印检查清单与可选 compose 命令。

示例：
  python tools/pg_failover_drill.py --phase print
  python tools/pg_failover_drill.py --phase up
  python tools/pg_failover_drill.py --phase baseline   # 写若干 spool 并 ship 到当前 DATABASE
  python tools/pg_failover_drill.py --phase verify
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import os
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
COMPOSE = [
    "docker",
    "compose",
    "-f",
    str(ROOT / "deploy" / "docker-compose.yml"),
    "-f",
    str(ROOT / "deploy" / "docker-compose.pg-ha.yml"),
    "--profile",
    "pg-ha",
]


def run(cmd: list[str]) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, cwd=ROOT)


def phase_print() -> None:
    print(
        """
M7k PostgreSQL 主从演练检查清单
================================
1) 空卷启动主从：
   docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.pg-ha.yml --profile pg-ha down -v
   docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.pg-ha.yml --profile pg-ha up -d
2) 对主库迁移：python tools/dev.py migrate
3) 基线审计：python tools/pg_failover_drill.py --phase baseline
4) 停主：docker stop <postgres 容器>
5) 促升从库：见 docs/ops/pg-failover.md（pg_ctl promote）
6) 应用改连新主后：python tools/pg_failover_drill.py --phase verify
7) 恢复日常：compose down -v && python tools/dev.py up

CI 对照：pytest tests/test_m7_pg_failover.py
文档：docs/ops/pg-failover.md
""".strip()
    )


def phase_up() -> None:
    run([*COMPOSE, "down", "-v"])
    run([*COMPOSE, "up", "-d"])


async def _baseline_and_verify(*, verify_only: bool) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from aip.libs.audit.spool import AuditSpool
    from aip.libs.audit.store import ship, verify_spool_file
    from aip.libs.common.config import get_settings

    settings = get_settings()
    url = settings.audit_database_url or settings.app_database_url
    engine = create_async_engine(url)
    spool_dir = pathlib.Path(tempfile.mkdtemp(prefix="aip-pg-failover-"))
    spool = AuditSpool(spool_dir, "drill")
    now = dt.datetime.now(dt.UTC)
    step = "after_promote" if verify_only else "baseline"
    n_recs = 3 if verify_only else 5
    try:
        for i in range(n_recs):
            spool.append(
                "ops.pg_failover_drill",
                company_id=None,
                now=now + dt.timedelta(seconds=i),
                record={"step": step, "i": i},
            )
        for path in list(spool_dir.glob("*.jsonl")):  # noqa: ASYNC240
            probs = verify_spool_file(path)
            if probs:
                print("spool verify FAILED:", probs)
                raise SystemExit(1)
        n = await ship(spool, engine)
        print(f"shipped={n} spool_dir={spool_dir} unshipped={spool.unshipped_bytes()}")
        if spool.unshipped_bytes() != 0:
            raise SystemExit("ship 后仍有未入库字节")
        async with engine.begin() as conn:
            cnt = (await conn.execute(text("SELECT count(*) FROM audit.audit_log"))).scalar_one()
        print(f"audit_log count={cnt}（含历史行）；本地 spool 链已校验")
        print("OK")
    finally:
        await engine.dispose()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--phase",
        choices=("print", "up", "baseline", "verify"),
        default="print",
    )
    args = ap.parse_args()
    if args.phase == "print":
        phase_print()
    elif args.phase == "up":
        phase_up()
    elif args.phase == "baseline":
        asyncio.run(_baseline_and_verify(verify_only=False))
    else:
        asyncio.run(_baseline_and_verify(verify_only=True))


if __name__ == "__main__":
    # 允许覆盖 DATABASE（与应用相同环境）
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    main()
