"""审计完整性校验：python tools/audit_verify.py [--dsn postgresql+asyncpg://...]

逐 shard 重算哈希链并校验锚点签名；发现问题时打印 shard 与 seq 并以 1 退出。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy.ext.asyncio import create_async_engine

from aip.libs.audit.store import verify
from aip.libs.common.config import get_settings
from aip.libs.tokens.keys import platform_key


async def main(dsn: str) -> int:
    engine = create_async_engine(dsn)
    async with engine.connect() as conn:
        problems = await verify(conn, [platform_key()])
    await engine.dispose()
    for p in problems:
        print(p)
    print("audit chain ok" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default=get_settings().app_database_url)
    sys.exit(asyncio.run(main(ap.parse_args().dsn)))
