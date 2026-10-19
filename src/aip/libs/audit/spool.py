"""审计本地 spool（architecture.md 12.2、12.4）：先同步追加到本地文件并 fsync，再由 shipper 异步入库。

- 审计库不可用时调用照常写 spool；spool 使用超过 80% 时 write_blocked 为真，Runtime 据此拒绝写操作；
  spool 满时 append 抛 POLICY_UNAVAILABLE，调用全部拒绝（审计零丢失优先）。
- 哈希链在追加时计算，所以 spool 文件本身就可以校验。
"""

from __future__ import annotations

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
        self._lock = threading.Lock()
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

    def append(
        self,
        kind: str,
        *,
        record: dict[str, Any],
        company_id: str | uuid.UUID | None = None,
        now: dt.datetime | None = None,
    ) -> dict[str, Any]:
        now = now or dt.datetime.now(dt.UTC)
        with self._lock:
            if self.unshipped_bytes() >= self.max_bytes:
                raise PlatformError(ErrorCode.POLICY_UNAVAILABLE, "审计缓冲已满，拒绝服务以保证审计零丢失")
            shard = self.shard_for(now)
            if shard != self._shard:
                self._restore(shard)
            entry = body(shard, self._seq + 1, now.isoformat(), str(company_id) if company_id else None, kind, record)
            h = chain_hash(self._hash, entry)
            line = {**entry, "prev_hash": self._hash, "hash": h}
            with (self.dir / f"{shard}.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n")
                f.flush()
                os.fsync(f.fileno())
            self._seq, self._hash = entry["seq"], h
            return line
