"""Worker 进程入口：python worker.py journal|dbos <executor_id> [app_version]。一直运行，直到被杀。"""

from __future__ import annotations

import asyncio
import sys


async def journal(executor_id: str) -> None:
    from journal_engine import JournalWorker, connect

    pool = await connect()
    await JournalWorker(pool, executor_id, lease_s=2.0).run_forever()


async def dbos(executor_id: str, app_version: str | None) -> None:
    from dbos import DBOS

    from dbos_engine import QUEUE, worker_config

    DBOS(config=worker_config(executor_id, app_version))  # type: ignore[arg-type]
    DBOS.launch()  # 启动时恢复本 executor_id、本应用版本下未完成的 workflow
    await DBOS.register_queue_async(QUEUE, polling_interval_sec=0.2)  # 与 journal 的 0.2s 扫描间隔一致
    print("ready", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    engine, eid = sys.argv[1], sys.argv[2]
    version = sys.argv[3] if len(sys.argv) > 3 else None
    if engine == "journal":
        print("ready", flush=True)
        asyncio.run(journal(eid))
    else:
        asyncio.run(dbos(eid, version))
