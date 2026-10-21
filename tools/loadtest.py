"""数据面压测（architecture.md 1 非功能目标：平台自身附加延迟 P95 ≤ 100ms，不含下游）。

起 1..N 个**独立进程**数据面（各 tempfile + instance_id，Mock 下游耗时为 0），
客户端对 worker 轮询并发调用 REST /invoke 与 MCP tools/call。

python tools/loadtest.py --concurrency 20 --requests 2000
python tools/loadtest.py --workers 2 --concurrency 20 --requests 2000
python tools/loadtest.py --workers 1,2,4 --concurrency 20 --requests 2000
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import multiprocessing as mp
import os
import pathlib
import socket
import statistics
import sys
import tempfile
import time
from typing import Any

import httpx

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

CAP = "production.mes.query_work_orders"
TOKEN = "aipk_loadtest"


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def pct(values: list[float], p: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, int(len(s) * p))]


def parse_workers(spec: str) -> list[int]:
    vals = [int(x.strip()) for x in spec.split(",") if x.strip()]
    if not vals or any(v < 1 for v in vals):
        raise ValueError("--workers 须为 ≥1 的整数或逗号列表，如 1,2,4")
    return vals


def _worker_main(port: int, instance_id: str, ready: Any, stop: Any) -> None:
    """子进程入口：构造 World 并阻塞服务直到 stop 置位。"""
    import uvicorn

    from aip.libs.common.config import get_settings
    from aip.libs.tokens.secrets_util import token_hash
    from m3_world import CONSUMER, World, connector, tool

    get_settings.cache_clear()
    os.environ["AIP_INSTANCE_ID"] = instance_id
    w = World(
        pathlib.Path(tempfile.mkdtemp(prefix=f"aip-lt-{instance_id}-")),
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

    async def _serve() -> None:
        await w.ready()
        config = uvicorn.Config(w.app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
        server = uvicorn.Server(config)
        task = asyncio.create_task(server.serve())
        while not server.started:
            await asyncio.sleep(0.05)
        ready.set()
        while not stop.is_set():
            await asyncio.sleep(0.05)
        server.should_exit = True
        await task

    asyncio.run(_serve())


def start_cluster(workers: int) -> tuple[list[str], list[mp.Process], list[Any]]:
    """启动 workers 个子进程；返回 (bases, processes, stop_events)。"""
    ctx = mp.get_context("spawn")
    bases: list[str] = []
    procs: list[mp.Process] = []
    stops: list[Any] = []
    for i in range(workers):
        port = _port()
        ready = ctx.Event()
        stop = ctx.Event()
        p = ctx.Process(
            target=_worker_main,
            args=(port, f"dp-lt-{i}", ready, stop),
            name=f"aip-loadtest-{i}",
            daemon=True,
        )
        p.start()
        if not ready.wait(timeout=60):
            stop.set()
            p.kill()
            raise RuntimeError(f"worker {i} failed to become ready")
        bases.append(f"http://127.0.0.1:{port}")
        procs.append(p)
        stops.append(stop)
    return bases, procs, stops


def stop_cluster(procs: list[mp.Process], stops: list[Any]) -> None:
    for ev in stops:
        ev.set()
    for p in procs:
        p.join(timeout=15)
        if p.is_alive():
            p.kill()
            p.join(timeout=5)


async def run(kind: str, bases: list[str], n: int, concurrency: int) -> dict[str, Any]:
    """对 bases 轮询发 n 个请求；返回聚合延迟与吞吐。"""
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
    cycle = itertools.cycle(bases)
    limits = httpx.Limits(max_connections=max(concurrency, len(bases)), max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(headers=headers, timeout=30, limits=limits) as c:

        async def one() -> None:
            nonlocal errors
            base = next(cycle)
            async with sem:
                t0 = time.perf_counter()
                if kind == "rest":
                    r = await c.post(f"{base}/api/v1/invoke/{CAP}", json=body_rest)
                    ok = r.status_code == 200 and r.json()["status"] == "succeeded"
                else:
                    r = await c.post(f"{base}/mcp", json=body_mcp)
                    ok = r.status_code == 200 and r.json()["result"]["isError"] is False
                lat.append((time.perf_counter() - t0) * 1000)
                errors += 0 if ok else 1

        warm = min(20, max(n, 1))
        for _ in range(warm):
            await one()
        lat.clear()
        errors = 0
        t0 = time.perf_counter()
        await asyncio.gather(*[one() for _ in range(n)])
        elapsed = time.perf_counter() - t0
    return {
        "workers": len(bases),
        "requests": n,
        "concurrency": concurrency,
        "errors": errors,
        "rps": round(n / elapsed, 1) if elapsed > 0 else 0.0,
        "p50_ms": round(statistics.median(lat), 1) if lat else 0.0,
        "p95_ms": round(pct(lat, 0.95), 1) if lat else 0.0,
        "p99_ms": round(pct(lat, 0.99), 1) if lat else 0.0,
        "max_ms": round(max(lat), 1) if lat else 0.0,
    }


async def measure(
    workers: int,
    requests: int,
    concurrency: int,
    *,
    kinds: tuple[str, ...] = ("rest", "mcp"),
) -> dict[str, Any]:
    """起 workers 个数据面进程，跑 kinds；供 CLI 与 pytest 复用。"""
    bases, procs, stops = start_cluster(workers)
    try:
        out: dict[str, Any] = {"workers": workers}
        for k in kinds:
            out[k] = await run(k, bases, requests, concurrency)
        return out
    finally:
        stop_cluster(procs, stops)


async def measure_scaling(
    worker_counts: list[int],
    requests: int,
    concurrency: int,
    *,
    kinds: tuple[str, ...] = ("rest", "mcp"),
) -> dict[str, Any]:
    """按 worker 列表依次测量，并相对首档计算 scaling = rps_N / (rps_1 * N)。"""
    series: list[dict[str, Any]] = []
    for n in worker_counts:
        series.append(await measure(n, requests, concurrency, kinds=kinds))
    baseline = series[0]
    scaling: dict[str, Any] = {}
    for row in series:
        n = row["workers"]
        entry: dict[str, float] = {}
        for k in kinds:
            r0 = float(baseline[k]["rps"])
            rn = float(row[k]["rps"])
            ideal = r0 * n
            entry[k] = round(rn / ideal, 3) if ideal > 0 else 0.0
        scaling[str(n)] = entry
    return {"series": series, "scaling_vs_workers1": scaling}


async def main(a: argparse.Namespace) -> None:
    counts = parse_workers(a.workers)
    if len(counts) == 1:
        print(json.dumps(await measure(counts[0], a.requests, a.concurrency), ensure_ascii=False, indent=2))
    else:
        print(
            json.dumps(
                await measure_scaling(counts, a.requests, a.concurrency),
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    # Windows spawn：子进程会再 import 本模块，需保护
    mp.freeze_support()
    ap = argparse.ArgumentParser()
    ap.add_argument("--requests", type=int, default=2000)
    ap.add_argument("--concurrency", type=int, default=20)
    ap.add_argument(
        "--workers",
        type=str,
        default="1",
        help="worker 进程数，或逗号列表做扩展对比（如 1,2,4）",
    )
    asyncio.run(main(ap.parse_args()))
