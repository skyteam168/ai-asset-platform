"""数据面快照缓存基类（architecture.md 4.4、5.1）：校验签名与摘要、seq 只增不减、落本地磁盘。

控制面不可用时，进程重启也能从本地文件恢复最近一份已验签的快照；缺失或损坏即 fail closed。
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
from typing import Any

from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.tokens.keys import SigningKey
from aip.libs.tokens.signed import verify_payload

log = logging.getLogger("aip.data_plane.snapshots")


class SnapshotRejected(Exception):
    pass


class SnapshotCache:
    kind = "snapshot"

    def __init__(self, trusted_keys: list[SigningKey], store_dir: pathlib.Path | None = None) -> None:
        self._keys = trusted_keys
        self._dir = store_dir
        self._data: dict[str, Any] | None = None
        self.seq: int | None = None

    def load(self, snapshot: dict[str, Any], *, persist: bool = True) -> bool:
        """校验并加载；返回 True 表示替换了当前快照。旧快照（seq 不大于当前）被忽略。"""
        payload = snapshot["payload"]
        if snapshot.get("kind", self.kind) != self.kind:
            raise SnapshotRejected("kind mismatch")
        if not verify_payload(payload, snapshot["sha256"], snapshot["signature"], self._keys):
            raise SnapshotRejected("bad digest or signature")
        if payload.get("seq") != snapshot["seq"]:
            raise SnapshotRejected("seq mismatch")
        if self.seq is not None and snapshot["seq"] <= self.seq:
            return False
        self._data = payload
        self.seq = int(snapshot["seq"])
        self._on_load(payload)
        if persist and self._dir is not None:
            self._persist(snapshot)
        return True

    def _on_load(self, payload: dict[str, Any]) -> None:
        """子类建索引。"""

    @property
    def path(self) -> pathlib.Path | None:
        return self._dir / f"{self.kind}.json" if self._dir else None

    def _persist(self, snapshot: dict[str, Any]) -> None:
        assert self.path is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def restore_from_disk(self) -> bool:
        """启动时调用：本地文件存在且验签通过才加载。"""
        if self.path is None or not self.path.exists():
            return False
        try:
            return self.load(json.loads(self.path.read_text(encoding="utf-8")), persist=False)
        except (SnapshotRejected, KeyError, ValueError) as e:
            log.error("local %s snapshot rejected: %s", self.kind, e)
            return False

    @property
    def data(self) -> dict[str, Any]:
        if self._data is None:
            raise PlatformError(ErrorCode.POLICY_UNAVAILABLE, f"{self.kind} 快照未加载，默认拒绝")
        return self._data
