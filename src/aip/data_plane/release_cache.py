"""数据面发布快照缓存：按 capability_id × env × channel / version 解析 Tool（architecture.md 5.1、7.3）。"""

from __future__ import annotations

from typing import Any

from aip.data_plane.snapshots import SnapshotCache
from aip.libs.common.errors import ErrorCode, PlatformError


class ReleaseCache(SnapshotCache):
    kind = "release"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._by_channel: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._by_version: dict[tuple[str, str, str], dict[str, Any]] = {}

    def _on_load(self, payload: dict[str, Any]) -> None:
        self._by_channel = {(r["capability_id"], r["env"], r["channel"]): r for r in payload["releases"]}
        self._by_version = {(r["capability_id"], r["env"], r["version"]): r for r in payload["releases"]}

    @property
    def policy(self) -> dict[str, Any]:
        return dict(self.data["policy"])

    def resolve(
        self, capability_id: str, env: str, *, channel: str = "stable", version: str | None = None
    ) -> dict[str, Any]:
        """只引用已发布版本，不引用“最新”（architecture.md 7.3）。"""
        self.data  # noqa: B018 - 快照缺失时抛 POLICY_UNAVAILABLE
        entry = (
            self._by_version.get((capability_id, env, version))
            if version
            else self._by_channel.get((capability_id, env, channel))
        )
        if entry is None:
            raise PlatformError(ErrorCode.NOT_FOUND, "能力不存在或未在该环境与渠道发布")
        return entry

    def releases(self, env: str) -> list[dict[str, Any]]:
        return [r for r in self.data["releases"] if r["env"] == env]
