"""数据面事件转发：进程内队列，后台批量回传控制面；控制面不可用时保留并重试（有上限，超出丢弃最旧并告警）。"""

from __future__ import annotations

import asyncio
import collections
import logging
from typing import Any

import httpx

log = logging.getLogger("aip.data_plane.events")
MAX_QUEUE = 10_000


class EventForwarder:
    def __init__(self, control_plane_url: str | None, token: str, instance_id: str) -> None:
        self.url, self.token, self.instance_id = control_plane_url, token, instance_id
        self.queue: collections.deque[dict[str, Any]] = collections.deque(maxlen=MAX_QUEUE)

    def __call__(self, kind: str, data: dict[str, Any]) -> None:
        if len(self.queue) == MAX_QUEUE:
            log.error("event queue full, dropping oldest")
        self.queue.append({"kind": kind, "data": data})

    async def flush(self, client: httpx.AsyncClient) -> int:
        if not self.queue:
            return 0
        batch = list(self.queue)[:500]
        r = await client.post(
            "/api/v1/internal/events",
            json={"instance_id": self.instance_id, "events": batch},
            headers={"Authorization": f"Bearer {self.token}"},
        )
        r.raise_for_status()
        for _ in batch:
            self.queue.popleft()
        return len(batch)

    async def run(self, interval_s: float = 1.0) -> None:
        if not self.url:
            return
        async with httpx.AsyncClient(base_url=self.url, timeout=10) as client:
            while True:
                try:
                    await self.flush(client)
                except httpx.HTTPError as e:
                    log.warning("event forward failed, will retry: %s", e)
                await asyncio.sleep(interval_s)
