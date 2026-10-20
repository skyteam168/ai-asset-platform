"""审计本地 spool（architecture.md 12.2、12.4）：先同步追加到本地文件并 fsync，再由 shipper 异步入库。

- 审计库不可用时调用照常写 spool；spool 使用超过 80% 时 write_blocked 为真，Runtime 据此拒绝写操作；
  spool 满时 append 抛 POLICY_UNAVAILABLE，调用全部拒绝（审计零丢失优先）。
- 哈希链在追加时计算，所以 spool 文件本身就可以校验。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import pathlib
import threading
import uuid
from typing import Any

from aip.libs.audit.chain import GENESIS, body, chain_hash
from aip.libs.common.errors import ErrorCode, PlatformError

WRITE_BLOCK_RATIO = 0.8


class AuditSpool:
    def __init__(self, directory: pathlib.Path, instance_id: str, max_bytes: int = 512 * 1024 * 1024) -> None:
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        self.instance_id = instance_id
        self.max_bytes = max_bytes
        self._lock = threading.Lock()  # 保护 seq / 哈希链 / 待写队列
        self._wlock = threading.Lock()  # 串行化写盘，保证落盘顺序
        self._flush_lock = asyncio.Lock()
        self._pending: list[tuple[str, int, str]] = []
        self._durable: dict[str, int] = {}  # shard → 已 fsync 的最大 seq
        self._shard: str | None = None
        self._seq = 0
        self._hash = GENESIS

    def shard_for(self, now: dt.datetime) -> str:
        return f"{self.instance_id}-{now:%Y%m%d}"

    def _restore(self, shard: str) -> None:
        path = self.dir / f"{shard}.jsonl"
        self._shard, self._seq, self._hash = shard, 0, GENESIS
        if path.exists():
            last = None
            with path.open("rb") as f:
                for line in f:
                    if line.strip():
                        last = line
            if last is not None:
                row = json.loads(last)
                self._seq, self._hash = int(row["seq"]), row["hash"]

    def unshipped_bytes(self) -> int:
        total = 0
        for p in self.dir.glob("*.jsonl"):
            off = self.dir / f"{p.stem}.offset"
            shipped = int(off.read_text()) if off.exists() else 0
            total += p.stat().st_size - shipped
        return total

    @property
    def write_blocked(self) -> bool:
        return self.unshipped_bytes() >= self.max_bytes * WRITE_BLOCK_RATIO

    def _assign(
        self, kind: str, record: dict[str, Any], company_id: str | uuid.UUID | None, now: dt.datetime
    ) -> tuple[str, dict[str, Any]]:
        """在 _lock 内调用：分配 seq、计算哈希链并放入待写队列（写入顺序与 seq 一致）。"""
        if self.unshipped_bytes() >= self.max_bytes:
            raise PlatformError(ErrorCode.POLICY_UNAVAILABLE, "审计缓冲已满，拒绝服务以保证审计零丢失")
        shard = self.shard_for(now)
        if shard != self._shard:
            self._restore(shard)
        entry = body(shard, self._seq + 1, now.isoformat(), str(company_id) if company_id else None, kind, record)
        h = chain_hash(self._hash, entry)
        line = {**entry, "prev_hash": self._hash, "hash": h}
        self._seq, self._hash = entry["seq"], h
        text = json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n"
        self._pending.append((shard, line["seq"], text))
        return shard, line

    def _drain(self) -> dict[str, int]:
        """把待写队列写入文件并 fsync（一次 fsync 覆盖整批）。先拿写锁再取批，保证多个写者按 seq 顺序落盘。"""
        with self._wlock:
            with self._lock:
                batch, self._pending = self._pending, []
            by_shard: dict[str, list[str]] = {}
            written: dict[str, int] = {}
            for shard, seq, text in batch:
                by_shard.setdefault(shard, []).append(text)
                written[shard] = max(written.get(shard, 0), seq)
            for shard, texts in by_shard.items():
                with (self.dir / f"{shard}.jsonl").open("a", encoding="utf-8") as f:
                    f.write("".join(texts))
                    f.flush()
                    os.fsync(f.fileno())
            return written

    def append(
        self,
        kind: str,
        *,
        record: dict[str, Any],
        company_id: str | uuid.UUID | None = None,
        now: dt.datetime | None = None,
    ) -> dict[str, Any]:
        """同步追加：返回时记录已 fsync（工具脚本与测试使用）。"""
        with self._lock:
            _, line = self._assign(kind, record, company_id, now or dt.datetime.now(dt.UTC))
        self._drain()
        return line

    async def append_async(
        self,
        kind: str,
        *,
        record: dict[str, Any],
        company_id: str | uuid.UUID | None = None,
        now: dt.datetime | None = None,
    ) -> dict[str, Any]:
        """组提交：在事件循环外写盘与 fsync，并发请求共享一次 fsync；返回时本条记录已落盘（审计零丢失不变）。"""
        with self._lock:
            _, line = self._assign(kind, record, company_id, now or dt.datetime.now(dt.UTC))
        async with self._flush_lock:
            # 等待前一批写完后，本条可能已被那一批顺带写入；只有确实未落盘时才再刷一次
            if self._durable.get(line["shard_id"], 0) < line["seq"]:
                written = await asyncio.to_thread(self._drain)
                for shard, seq in written.items():
                    self._durable[shard] = max(self._durable.get(shard, 0), seq)
        return line
