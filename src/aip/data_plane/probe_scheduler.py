"""合成探针调度（architecture.md 7.3）：按 Tool Spec.probe 定时只读调用，结果回传控制面。

- 带 X-Platform-Probe（经 ExecContext.extra_headers）；
- 以 platform_probe 身份、exposure=internal 经 PDP；
- 计入 Connector 限流（走 Runtime._execute）；
- write 类若无 probe.args 但有 reconcile，用 reconcile.operation 作探针参数入口（args 来自 probe）。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

import jsonschema

from aip.data_plane.events import EventForwarder
from aip.data_plane.principal import Principal
from aip.data_plane.release_cache import ReleaseCache
from aip.data_plane.runtime import ToolRuntime
from aip.libs.common.ids import utcnow

log = logging.getLogger("aip.probe")
DEFAULT_INTERVAL_S = 3600


def probe_principal(iam_seq: int = 0) -> Principal:
    """专用探针身份：高密级、集团范围；facts 层见 build_input 的 probe 分支。"""
    return Principal(
        identity_kind="app",
        user_id=None,
        company_id="00000000-0000-0000-0000-000000000000",
        org_ids=[],
        roles=[],
        clearance=4,
        data_scope_org_ids=[],
        client_id="platform-probe",
        consumer_id="00000000-0000-0000-0000-000000000001",
        consumer_company_id="00000000-0000-0000-0000-000000000000",
        agent_profile_id=None,
        auth_mode="app_token",
        iam_snapshot_seq=iam_seq,
        extra={"probe": True},
    )


class ProbeScheduler:
    def __init__(
        self,
        runtime: ToolRuntime,
        releases: ReleaseCache,
        events: EventForwarder,
        *,
        interval_check_s: float = 30.0,
    ) -> None:
        self.runtime, self.releases, self.events = runtime, releases, events
        self.interval_check_s = interval_check_s
        self._last: dict[str, float] = {}  # capability@version -> monotonic last run

    def due(self, release: dict[str, Any], now: float | None = None) -> bool:
        spec = release.get("spec") or {}
        probe = spec.get("probe")
        if not probe and not _write_reconcile(spec):
            return False
        if not probe and _write_reconcile(spec):
            # write 无 probe 配置则不跑（需显式 probe）
            return False
        key = f"{release['capability_id']}@{release['version']}"
        interval = int((probe or {}).get("interval_s") or DEFAULT_INTERVAL_S)
        last = self._last.get(key, 0.0)
        return (now or time.monotonic()) - last >= interval

    async def tick(self, *, force: bool = False) -> list[dict[str, Any]]:
        """扫描一轮；force 忽略间隔（测试用）。返回本轮结果摘要。"""
        out: list[dict[str, Any]] = []
        now = time.monotonic()
        for rel in self.releases.releases(self.runtime.policy.env):
            if rel.get("asset_type") != "tool":
                continue
            if rel.get("channel") != "stable" or rel.get("status") not in ("active", "canary"):
                continue
            if not force and not self.due(rel, now):
                continue
            result = await self.run_one(rel)
            key = f"{rel['capability_id']}@{rel['version']}"
            self._last[key] = time.monotonic()
            out.append(result)
        return out

    async def run_one(self, release: dict[str, Any]) -> dict[str, Any]:
        spec = release["spec"]
        probe = spec.get("probe") or {}
        args = dict(probe.get("args") or {})
        cap = release["capability_id"]
        version = release["version"]
        version_id = release.get("version_id") or release.get("asset_version_id")
        # 无 version_id 时控制面按 capability+version 反查 asset_version
        started = time.monotonic()
        principal = probe_principal(int(self.runtime.iam.seq or 0))
        try:
            inv = await self.runtime.invoke(
                principal,
                cap,
                args,
                exposure="internal",
                channel=release["channel"],
                version=version,
                idempotency_key=f"probe:{cap}:{version}:{uuid.uuid4().hex}",
                trace_id=uuid.uuid4().hex,
            )
        except Exception as e:
            log.warning("probe invoke error %s: %s", cap, type(e).__name__)
            inv = {"status": "failed", "error_code": "PROBE_ERROR", "error_message": str(e)}

        latency_ms = int((time.monotonic() - started) * 1000)
        status = inv.get("status")
        data = inv.get("result")
        schema_valid = True
        outcome = "ok"
        if status != "succeeded":
            outcome = "failed"
            schema_valid = False
        else:
            errors = list(jsonschema.Draft202012Validator(spec.get("output_schema") or {}).iter_errors(data))
            if errors:
                outcome = "schema_invalid"
                schema_valid = False
            # 可选 assert 表达式：首期只检查非空字符串存在于 JSON 文本（避免引入 CEL 依赖到探针）
            assertion = probe.get("assert")
            if assertion and schema_valid and assertion not in str(data):
                outcome = "failed"
                schema_valid = False

        payload = {
            "capability_id": cap,
            "version": version,
            "asset_id": release.get("asset_id"),
            "version_id": version_id,
            "outcome": outcome,
            "schema_valid": schema_valid,
            "latency_ms": latency_ms,
            "status": status,
            "error_code": inv.get("error_code"),
            "run_at": utcnow().isoformat(),
        }
        self.events("probe.result", payload)
        return payload

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("probe scheduler tick failed")
            await asyncio.sleep(self.interval_check_s)


def _write_reconcile(spec: dict[str, Any]) -> bool:
    return bool(spec.get("reconcile")) and spec.get("risk_level") in ("write", "danger")
