"""数据面本地 IAM 快照缓存（architecture.md 4.4）。"""

from __future__ import annotations

from typing import Any

from aip.data_plane.snapshots import SnapshotCache, SnapshotRejected

__all__ = ["IamCache", "SnapshotRejected"]


class IamCache(SnapshotCache):
    kind = "iam"

    def consumer_by_client_id(self, client_id: str) -> tuple[str, dict[str, Any]] | None:
        for cid, c in self.data["consumers"].items():
            if c["client_id"] == client_id:
                return cid, c
        return None

    def consumer_by_token_hash(self, digest: str) -> tuple[str, dict[str, Any], dict[str, Any]] | None:
        for cid, c in self.data["consumers"].items():
            tok = c["static_tokens"].get(digest)
            if tok is not None:
                return cid, c, tok
        return None
