"""数据面压测（architecture.md 1 非功能目标：平台自身附加延迟 P95 ≤ 100ms，不含下游）。

在本进程内用 uvicorn 起数据面（Mock Connector，下游耗时为 0），并发调用 REST /invoke 与 MCP tools/call，
统计端到端延迟分位数与吞吐。结果即“平台自身附加延迟”的上界（含 PDP、审计 spool fsync、Result Guard）。

python tools/loadtest.py --concurrency 20 --requests 2000
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import socket
import statistics
import sys
import tempfile
import time

import httpx
import uvicorn

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from aip.libs.tokens.secrets_util import token_hash  # noqa: E402
from m3_world import CONSUMER, World, connector, tool  # noqa: E402

CAP = "production.mes.query_work_orders"
TOKEN = "aipk_loadtest"


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def pct(values: list[float], p: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, int(len(s) * p))]


async def run(kind: str, base: str, n: int, concurrency: int) -> dict[str, float]:
    lat: list[float] = []
    errors = 0
    sem = asyncio.Semaphore(concurrency)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    body_rest = {"args": {"factory_id": "F001"}}
    body_mcp = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": CAP,
            "arguments": {"factory_id": "F001"},
            "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"},
        },
    }
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(base_url=base, headers=headers, timeout=30, limits=limits) as c:

        async def one() -> None:
            nonlocal errors
            async with sem:
                t0 = time.perf_counter()
                if kind == "rest":
                    r = await c.post(f"/api/v1/invoke/{CAP}", json=body_rest)
                    ok = r.status_code == 200 and r.json()["status"] == "succeeded"
                else:
                    r = await c.post("/mcp", json=body_mcp)
                    ok = r.status_code == 200 and r.json()["result"]["isError"] is False
                lat.append((time.perf_counter() - t0) * 1000)
                errors += 0 if ok else 1

        for _ in range(20):  # 预热
            await one()
        lat.clear()
        t0 = time.perf_counter()
        await asyncio.gather(*[one() for _ in range(n)])
        elapsed = time.perf_counter() - t0
    return {
        "requests": n,
        "concurrency": concurrency,
        "errors": errors,
        "rps": round(n / elapsed, 1),
        "p50_ms": round(statistics.median(lat), 1),
        "p95_ms": round(pct(lat, 0.95), 1),
        "p99_ms": round(pct(lat, 0.99), 1),
        "max_ms": round(max(lat), 1),
    }


async def main(a: argparse.Namespace) -> None:
    w = World(
        pathlib.Path(tempfile.mkdtemp()),
        [tool(CAP, "mock", "q", timeout={"connect_ms": 2000, "read_ms": 5000, "deadline_ms": 8000})],
        [
            connector(
                "mock",
                "mock",
                {"operations": {"q": {"response": [{"wo_no": "W", "qty": 1, "status": "open"}]}}},
                rate_per_s=100000,
                burst=100000,
                concurrency=256,
            )
        ],
    )
    w.app.state.iam.data["consumers"][CONSUMER]["static_tokens"] = {
        token_hash(TOKEN): {"expires_at": "2099-01-01T00:00:00+00:00", "ip_binding": []}
    }
    port = _port()
    server = uvicorn.Server(uvicorn.Config(w.app, host="127.0.0.1", port=port, log_level="warning", access_log=False))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    try:
        results = {k: await run(k, f"http://127.0.0.1:{port}", a.requests, a.concurrency) for k in ("rest", "mcp")}
    finally:
        server.should_exit = True
        await task
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--requests", type=int, default=2000)
    ap.add_argument("--concurrency", type=int, default=20)
    asyncio.run(main(ap.parse_args()))
