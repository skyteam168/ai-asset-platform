"""诊断：DBOS durable sleep 跨重启是否提前结束。打印 sleep 步骤记录的起止时间与测试侧观测。"""

from __future__ import annotations

import asyncio
import sys
import time
import uuid

from common import human
from dbos_engine import DbosExecutor
from test_spike import Workers, _current_is, wait_until


async def once(kill: bool) -> None:
    ex = DbosExecutor()
    w = Workers("dbos")
    w.start("w1")
    r = "diag-" + uuid.uuid4().hex[:8]
    t_enqueue = time.time()
    await ex.start(r, human(timeout_s=30, wait_s=3), {})
    await wait_until(lambda: _current_is(ex, r, "w"))
    t_seen_w = time.time()
    if kill:
        w.kill("w1")
        w.start("w1")
    await wait_until(lambda: _current_is(ex, r, "h"))
    t_seen_h = time.time()
    steps = ex.client.list_workflow_steps(r)
    w.stop_all()
    for s in steps:
        if "sleep" in s["function_name"].lower():
            st, en = s["started_at_epoch_ms"], s["completed_at_epoch_ms"]
            print(f"kill={kill} sleep step: start={st / 1000 - t_enqueue:+.2f}s end={(en or 0) / 1000 - t_enqueue:+.2f}s "
                  f"output={s['output']!r}")
    print(f"kill={kill} seen w at {t_seen_w - t_enqueue:+.2f}s, seen h at {t_seen_h - t_enqueue:+.2f}s")
    ex.close()


if __name__ == "__main__":
    asyncio.run(once("--kill" in sys.argv))
