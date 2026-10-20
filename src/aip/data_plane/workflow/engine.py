"""Workflow 引擎：自研 step journal（ADR-0003，architecture.md 8.5）。

存储：exec.wf_run（钉住 DSL、版本、Principal；当前节点、上下文、预算用量、租约、唤醒时间）、
exec.wf_step（journal，(run_id, node_id) 唯一）、exec.human_task。

执行模型：
- Worker 用 FOR UPDATE SKIP LOCKED 认领可运行的 Run 并持有租约；租约过期任何 Worker 都能接手，
  不依赖崩溃的进程回来。每推进一个节点续租一次。
- 每个节点完成与“当前节点指针前移”在同一事务里提交；崩溃后要么重做该节点，要么从下一节点继续。
- Tool 节点经 ToolRuntime（统一执行链：PDP、确认、幂等、超时、重试、审计）执行，幂等键 = wf:{run}:{node}，
  重做时由 Runtime 的幂等记录返回第一次的结果，下游只执行一次；结果未知时按退避对账，仍不确定升级人工。
- 等待（wait、人工任务、确认、对账退避、Run 总期限）一律落在 wf_run.wake_at，由扫描唤醒，不在内存里计时。
- 失败或被驳回时按完成顺序逆序执行补偿（Saga）；补偿同样带幂等键，补偿中途崩溃可续做。
- 预算：步数、LLM 调用数、token、总期限；超出即失败并补偿。
- 污点（8.7）：用到 untrusted_output 的 Tool 后 Run 标记 tainted；之后的 write / danger / 对外发送节点
  无论 confirm_policy 如何，都先经人工确认。
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import jsonschema
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from aip.data_plane.confirmation import principal_from_json, principal_to_json
from aip.data_plane.principal import Principal
from aip.data_plane.runtime import ToolRuntime
from aip.data_plane.workflow.llm import LlmClient, UnconfiguredLlm
from aip.libs.audit.spool import AuditSpool
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.workflow import expr
from aip.libs.workflow.dsl import WRITE_RISKS, assignee_org_path, resolve_args, split_ref, validate

log = logging.getLogger(__name__)

DEFAULT_TASK_TIMEOUT_S = 86_400
RECONCILE_MAX = 5
FINAL = ("succeeded", "failed", "compensated", "rejected", "cancelled")


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _j(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, default=str)


def _str(row: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in dict(row).items():
        if isinstance(v, uuid.UUID):
            out[k] = str(v)
        elif isinstance(v, dt.datetime):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


@dataclass
class Next:
    target: str
    output: Any = None


@dataclass
class Park:
    until: dt.datetime


@dataclass
class Stop:
    status: str  # failed / rejected
    code: str
    message: str
    node: str


CLAIM = """
UPDATE exec.wf_run SET lease_owner = :me, lease_until = now() + make_interval(secs => :lease),
       status = 'running', updated_at = now()
WHERE id = (
  SELECT id FROM exec.wf_run
  WHERE (status = 'running' AND (lease_until IS NULL OR lease_until < now()))
     OR (status = 'waiting' AND wake_at <= now())
  ORDER BY updated_at FOR UPDATE SKIP LOCKED LIMIT 1
) RETURNING *
"""


class WorkflowEngine:
    def __init__(
        self,
        engine: AsyncEngine,
        runtime: ToolRuntime,
        audit: AuditSpool,
        *,
        instance_id: str,
        llm: LlmClient | None = None,
        lease_s: float = 30.0,
        poll_s: float = 0.5,
        stale_s: float = 30.0,
        reconcile_base_s: float = 2.0,
    ) -> None:
        self.db, self.runtime, self.audit = engine, runtime, audit
        self.me, self.llm = instance_id, llm or UnconfiguredLlm()
        self.lease_s, self.poll_s, self.stale_s, self.reconcile_base_s = lease_s, poll_s, stale_s, reconcile_base_s
        self._kick = asyncio.Event()
        runtime.on_confirmation_done = self.wake

    @contextlib.asynccontextmanager
    async def _tx(self) -> AsyncIterator[AsyncConnection]:
        async with self.db.begin() as conn:
            # 引擎服务所有公司：系统身份绕过 RLS；对外查询接口自己按消费方过滤
            await conn.execute(text("SELECT set_config('app.bypass_rls', 'on', true)"))
            yield conn

    # ------------------------------------------------------------------ 执行方接口

    def _tool_spec(self, cap: str, version: str) -> dict[str, Any] | None:
        try:
            rel = self.runtime.releases.resolve(cap, self.runtime.policy.env, version=version)
        except PlatformError:
            return None
        return rel["spec"] if rel.get("asset_type", "tool") == "tool" else None

    async def start(
        self,
        principal: Principal,
        release: dict[str, Any],
        input_: dict[str, Any],
        *,
        trace_id: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        dsl = release["spec"]
        problems = validate(dsl, self._tool_spec)
        if problems:
            raise PlatformError(ErrorCode.PRECONDITION_FAILED, "Workflow 定义无效：" + "；".join(problems[:3]))
        errors = list(jsonschema.Draft202012Validator(dsl.get("input_schema") or {}).iter_errors(input_))
        if errors:
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, errors[0].message)
        # 同一消费方、同一 Workflow、同一幂等键只启动一个 Run
        run_id = (
            str(uuid.uuid5(uuid.NAMESPACE_URL, f"{principal.consumer_id}/{release['capability_id']}/{idempotency_key}"))
            if idempotency_key
            else str(uuid.uuid4())
        )
        deadline = _now() + dt.timedelta(seconds=int(dsl["budget"]["timeout_s"]))
        async with self._tx() as conn:
            created = (
                await conn.execute(
                    text(
                        """INSERT INTO exec.wf_run (id, company_id, consumer_id, end_user_id, capability_id, version,
                             release_id, dsl, principal, input, ctx, current, status, deadline, trace_id)
                           VALUES (:id, :company, :consumer, :user, :cap, :ver, :rel, CAST(:dsl AS jsonb),
                             CAST(:principal AS jsonb), CAST(:input AS jsonb), CAST(:ctx AS jsonb), :current,
                             'running', :deadline, :trace)
                           ON CONFLICT (id) DO NOTHING RETURNING id"""
                    ),
                    {
                        "id": run_id,
                        "company": principal.consumer_company_id,
                        "consumer": principal.consumer_id,
                        "user": principal.user_id,
                        "cap": release["capability_id"],
                        "ver": release["version"],
                        "rel": release["release_id"],
                        "dsl": _j(dsl),
                        "principal": _j(principal_to_json(principal)),
                        "input": _j(input_),
                        "ctx": _j({"input": input_}),
                        "current": dsl["nodes"][0]["id"],
                        "deadline": deadline,
                        "trace": trace_id,
                    },
                )
            ).first()
        if created:
            await self._audit_run(
                "workflow.run.started",
                run_id,
                principal.consumer_company_id,
                {
                    "capability_id": release["capability_id"],
                    "version": release["version"],
                    "consumer_id": principal.consumer_id,
                    "end_user_id": principal.user_id,
                    "trace_id": trace_id,
                },
            )
            self._kick.set()
        return await self.view(run_id)

    async def view(self, run_id: str, consumer_id: str | None = None) -> dict[str, Any]:
        async with self._tx() as conn:
            run = (
                (await conn.execute(text("SELECT * FROM exec.wf_run WHERE id = :id"), {"id": run_id}))
                .mappings()
                .first()
            )
            if run is None or (consumer_id is not None and str(run["consumer_id"]) != consumer_id):
                raise PlatformError(ErrorCode.NOT_FOUND, "Run 不存在")
            steps = (
                (
                    await conn.execute(
                        text(
                            "SELECT node_id, kind, status, attempts, executor FROM exec.wf_step "
                            "WHERE run_id = :id ORDER BY seq"
                        ),
                        {"id": run_id},
                    )
                )
                .mappings()
                .all()
            )
            tasks = (
                (
                    await conn.execute(
                        text(
                            "SELECT id, node_id, kind, assignee, assignee_user_id, assignee_role, assignee_org_id, "
                            "summary, status, deadline FROM exec.human_task WHERE run_id = :id AND status = 'pending'"
                        ),
                        {"id": run_id},
                    )
                )
                .mappings()
                .all()
            )
        return {
            "run_id": run_id,
            "company_id": str(run["company_id"]),
            "capability_id": run["capability_id"],
            "version": run["version"],
            "status": run["status"],
            "current": run["current"],
            "result": run["result"],
            "error": run["error"],
            "tainted": run["tainted"],
            "taint_sources": list(run["taint_sources"]),
            "budget_used": {"steps": run["steps_used"], "llm_calls": run["llm_calls"], "tokens": run["tokens_used"]},
            "steps": [dict(s) for s in steps],
            "pending_tasks": [_str(t) for t in tasks],
            "trace_id": run["trace_id"],
        }

    async def wait_settled(self, run_id: str, timeout_s: float) -> dict[str, Any]:
        """同步调用方等待最多 timeout_s 秒：结束或进入人工等待即返回。"""
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout_s
        while True:
            view = await self.view(run_id)
            if view["status"] in FINAL or view["pending_tasks"] or loop.time() >= end:
                return view
            await asyncio.sleep(0.05)

    async def wake(self, run_id: str) -> None:
        async with self._tx() as conn:
            await conn.execute(
                text("UPDATE exec.wf_run SET wake_at = now() WHERE id = :id AND status = 'waiting'"), {"id": run_id}
            )
        self._kick.set()

    async def task(self, task_id: str) -> dict[str, Any] | None:
        async with self._tx() as conn:
            m = (
                (await conn.execute(text("SELECT * FROM exec.human_task WHERE id = :id"), {"id": task_id}))
                .mappings()
                .first()
            )
        return _str(m) if m else None

    async def pending_tasks(self, company_ids: list[str] | None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM exec.human_task WHERE status = 'pending' AND deadline > now()"
        params: dict[str, Any] = {}
        if company_ids is not None:
            sql += " AND company_id = ANY(CAST(:c AS uuid[]))"
            params["c"] = company_ids
        async with self._tx() as conn:
            rows = (await conn.execute(text(sql + " ORDER BY created_at LIMIT 500"), params)).mappings().all()
        return [_str(r) for r in rows]

    async def decide_task(
        self,
        task_id: str,
        *,
        decision: str,
        user_id: str,
        data: dict[str, Any] | None = None,
        comment: str | None = None,
    ) -> dict[str, Any]:
        """授权（谁能处理这个任务）由调用方完成；这里只保证：任务仍待处理、未过期、指定了人时必须是那个人、
        补录数据符合 input_schema。"""
        t = await self.task(task_id)
        if t is None:
            raise PlatformError(ErrorCode.NOT_FOUND, "任务不存在")
        if t["assignee_user_id"] and t["assignee_user_id"] != user_id:
            raise PlatformError(ErrorCode.PERMISSION_DENIED, "该任务指定了处理人")
        status = {"approve": "completed" if t["kind"] == "input" else "approved", "reject": "rejected"}[decision]
        if t["kind"] == "input" and decision == "approve":
            schema = (t.get("data") or {}).get("input_schema") or {}
            errs = list(jsonschema.Draft202012Validator(schema).iter_errors(data or {}))
            if errs:
                raise PlatformError(ErrorCode.INVALID_ARGUMENT, errs[0].message)
        async with self._tx() as conn:
            ok = (
                await conn.execute(
                    text(
                        "UPDATE exec.human_task SET status = :s, decided_by = :by, decided_at = now(), "
                        "data = COALESCE(data, '{}'::jsonb) || CAST(:data AS jsonb) "
                        "WHERE id = :id AND status = 'pending' AND deadline > now() RETURNING run_id"
                    ),
                    {
                        "id": task_id,
                        "s": status,
                        "by": f"user:{user_id}",
                        "data": _j({"submitted": data or {}, "comment": comment}),
                    },
                )
            ).first()
            if ok is None:
                raise PlatformError(ErrorCode.PRECONDITION_FAILED, "任务已处理或已过期")
            await conn.execute(
                text("UPDATE exec.wf_run SET wake_at = now() WHERE id = :id AND status = 'waiting'"), {"id": ok[0]}
            )
        await self.audit.append_async(
            "workflow.task.decided",
            company_id=t["company_id"],
            record={
                "task_id": task_id,
                "run_id": t["run_id"],
                "node_id": t["node_id"],
                "decision": status,
                "by": user_id,
            },
        )
        self._kick.set()
        return {"id": task_id, "status": status}

    # ------------------------------------------------------------------ Worker

    async def run_worker(self, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        while not stop.is_set():
            try:
                busy = await self.tick()
            except Exception:  # Worker 不能因为单个 Run 的异常退出
                log.exception("workflow tick failed")
                busy = False
            if not busy:
                self._kick.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._kick.wait(), self.poll_s)

    async def tick(self) -> bool:
        async with self._tx() as conn:
            row = (await conn.execute(text(CLAIM), {"me": self.me, "lease": self.lease_s})).mappings().first()
        if row is None:
            return False
        await self.drive(dict(row))
        return True

    async def drive(self, run: dict[str, Any]) -> None:
        run_id = str(run["id"])
        dsl: dict[str, Any] = run["dsl"]
        ctx: dict[str, Any] = run["ctx"]
        principal = principal_from_json(run["principal"])
        nodes = {n["id"]: n for n in dsl["nodes"]}
        current: str = run["current"]
        if run["error"] and run["error"].get("compensating"):
            return await self._compensate_and_close(run, ctx, run["error"])
        while True:
            if _now() >= run["deadline"]:
                return await self._fail(run, ctx, Stop("failed", "RUN_TIMEOUT", "超过 Run 总期限", current))
            if current == "end":
                return await self._close(run_id, run, "succeeded", ctx, result=ctx.get("_result", {}))
            n = nodes[current]
            await self._renew(run_id)
            if n["type"] == "end":
                ctx["_result"] = resolve_args(n.get("output") or {}, ctx)
                return await self._close(run_id, run, "succeeded", ctx, result=ctx["_result"])
            try:
                outcome = await self._step(run, principal, n, ctx)
            except PlatformError as e:
                outcome = Stop("failed", str(e.code), e.detail, current)
            if isinstance(outcome, Park):
                return await self._park(run_id, ctx, current, min(outcome.until, run["deadline"]))
            if isinstance(outcome, Stop):
                return await self._fail(run, ctx, outcome)
            run["steps_used"] += 1
            if run["steps_used"] > int(dsl["budget"]["max_steps"]):
                return await self._fail(run, ctx, Stop("failed", "BUDGET_EXCEEDED", "超过最大步数", current))
            if outcome.output is not None:
                ctx[current] = outcome.output
            await self._advance(run, ctx, outcome.target)
            current = outcome.target

    async def _step(
        self, run: dict[str, Any], principal: Principal, n: dict[str, Any], ctx: dict[str, Any]
    ) -> Next | Park | Stop:
        kind = n["type"]
        if kind == "condition":
            target = n["then"] if expr.check(n["expr"], ctx) else n["else"]
            await self._journal(run, n, {"result": target == n["then"]})
            return Next(target)
        if kind == "wait":
            out = await self._journal(
                run, n, {"until": (_now() + dt.timedelta(seconds=float(n["seconds"]))).isoformat()}
            )
            until = dt.datetime.fromisoformat(out["until"])
            return Park(until) if _now() < until else Next(n["next"])
        if kind == "human_task":
            return await self._human(run, n, ctx)
        if kind == "llm":
            return await self._llm(run, n, ctx)
        return await self._tool(run, principal, n, ctx)

    # ------------------------------------------------------------------ 节点

    async def _tool(
        self, run: dict[str, Any], principal: Principal, n: dict[str, Any], ctx: dict[str, Any]
    ) -> Next | Park | Stop:
        run_id, nid = str(run["id"]), n["id"]
        step = await self._get_step(run_id, nid)
        if step and step["status"] == "done":
            return Next(n["next"], step["output"])
        cap, version = split_ref(n["ref"])
        spec = self._tool_spec(cap, version)
        if spec is None:
            return Stop("failed", "NOT_FOUND", f"{n['ref']} 不可用", nid)
        risky = spec.get("risk_level") in WRITE_RISKS or spec.get("egress_class") == "external"
        if run["tainted"] and risky:
            gate = await self._taint_gate(run, n, ctx)
            if not isinstance(gate, Next):
                return gate
        if step and step["status"] == "waiting" and "reconcile" in (step["output"] or {}):
            return await self._reconcile(run, n, step)
        await self._mark_started(run, n)
        # 写操作与需要确认的调用带固定幂等键（重做时由 Runtime 返回第一次的结果）；只读调用崩溃后直接重做
        keyed = spec.get("risk_level") in WRITE_RISKS or spec.get("confirm_policy", "auto") != "auto"
        res = await self.runtime.invoke(
            principal,
            cap,
            resolve_args(n.get("args") or {}, ctx),
            idempotency_key=f"wf:{run_id}:{nid}" if keyed else None,
            exposure="internal",
            version=version,
            trace_id=run["trace_id"] or "",
            run_id=run_id,
        )
        return await self._tool_result(run, n, spec, res)

    async def _tool_result(
        self, run: dict[str, Any], n: dict[str, Any], spec: dict[str, Any], res: dict[str, Any]
    ) -> Next | Park | Stop:
        nid, status = n["id"], res["status"]
        if status == "succeeded":
            await self._done(run, n, res["data"], res["execution_id"])
            if spec.get("untrusted_output") and not run["tainted"]:
                await self._taint(run, f"{nid}:{n['ref']}")
            return Next(n["next"], res["data"])
        if status == "pending_confirm":
            cid = (res.get("next_action") or {}).get("confirmation_id")
            conf = await self.runtime.confirmations.get(cid) if cid else None
            if conf is None:
                return Stop("failed", "CONFIRMATION_REQUIRED", "确认对象缺失", nid)
            if conf["status"] == "expired" or conf["expires_at"] <= _now():
                return Stop("failed", "CONFIRMATION_EXPIRED", "确认已过期", nid)
            await self._set_step(run, nid, "waiting", {"confirmation_id": cid}, res["execution_id"])
            return Park(conf["expires_at"])
        if status == "cancelled":
            return Stop("rejected", "CONFIRMATION_REJECTED", "调用确认被拒绝", nid)
        if status == "unknown":
            await self._set_step(run, nid, "waiting", {"reconcile": 0}, res["execution_id"])
            return Park(_now() + dt.timedelta(seconds=self.reconcile_base_s))
        if status == "running":
            # 上次执行中途进程崩溃：记录卡在 running。超过陈旧阈值后按结果未知对账
            settled = await self.runtime.settle_stale(res["execution_id"], self.stale_s)
            if settled == "unknown":
                await self._set_step(run, nid, "waiting", {"reconcile": 0}, res["execution_id"])
                return Park(_now())
            return Park(_now() + dt.timedelta(seconds=1))
        err = res.get("error") or {}
        await self._set_step(run, nid, "failed", {"error": err}, res["execution_id"])
        return Stop("failed", err.get("code", "STEP_FAILED"), err.get("message", ""), nid)

    async def _reconcile(self, run: dict[str, Any], n: dict[str, Any], step: dict[str, Any]) -> Next | Park | Stop:
        nid, attempts = n["id"], int(step["output"]["reconcile"])
        manual = (step["output"] or {}).get("manual_task")
        if manual:
            t = await self.task(manual)
            if t and t["status"] == "pending" and dt.datetime.fromisoformat(t["deadline"]) > _now():
                return Park(dt.datetime.fromisoformat(t["deadline"]))
            submitted = ((t or {}).get("data") or {}).get("submitted") or {}
            if t and t["status"] == "completed" and submitted.get("outcome") == "succeeded":
                await self._done(run, n, submitted.get("result"), step["execution_id"])
                return Next(n["next"], submitted.get("result"))
            return Stop("failed", "OUTCOME_UNKNOWN", "人工核实为未成功或未处理", nid)
        res = await self.runtime.reconcile(str(step["execution_id"]))
        cap, version = split_ref(n["ref"])
        spec = self._tool_spec(cap, version) or {}
        if res["status"] == "failed" and (spec.get("idempotency") or {}).get("idempotent"):
            rec = await self.runtime.store.get(str(step["execution_id"]))
            if rec and rec.get("reconcile_status") == "not_executed":
                # 确认未执行：清掉等待标记，下次推进时用同一幂等键重试
                await self._set_step(run, nid, "started", None, step["execution_id"])
                return Park(_now())
        if res["status"] in ("succeeded", "failed"):
            return await self._tool_result(run, n, spec, res)
        if attempts + 1 >= RECONCILE_MAX:
            task_id = await self._create_task(
                run,
                f"{nid}#reconcile",
                "input",
                f"role:platform_admin@{run['company_id']}",
                f"{n['ref']} 执行结果未知，已对账 {RECONCILE_MAX} 次仍不确定，请到下游系统核实后填写结果",
                DEFAULT_TASK_TIMEOUT_S,
                {
                    "input_schema": {
                        "type": "object",
                        "properties": {"outcome": {"enum": ["succeeded", "failed"]}},
                        "required": ["outcome"],
                    }
                },
            )
            await self._set_step(
                run, nid, "waiting", {"reconcile": attempts + 1, "manual_task": task_id}, step["execution_id"]
            )
            return Park(_now() + dt.timedelta(seconds=DEFAULT_TASK_TIMEOUT_S))
        await self._set_step(run, nid, "waiting", {"reconcile": attempts + 1}, step["execution_id"])
        return Park(_now() + dt.timedelta(seconds=self.reconcile_base_s * 2**attempts))

    async def _llm(self, run: dict[str, Any], n: dict[str, Any], ctx: dict[str, Any]) -> Next | Park | Stop:
        nid, budget = n["id"], run["dsl"]["budget"]
        step = await self._get_step(str(run["id"]), nid)
        if step and step["status"] == "done":
            return Next(n["next"], step["output"])
        if run["llm_calls"] + 1 > int(budget["max_llm_calls"]):
            return Stop("failed", "BUDGET_EXCEEDED", "超过 LLM 调用次数上限", nid)
        schema = run["dsl"]["schemas"][n["output_schema"]]
        await self._mark_started(run, n)
        res = await self.llm.complete(
            model_profile=n["model_profile"],
            prompt=n["prompt"],
            variables=resolve_args(n.get("inputs") or {}, ctx),
            output_schema=schema,
            company_id=str(run["company_id"]),
        )
        run["llm_calls"] += 1
        run["tokens_used"] += res.tokens
        if budget.get("max_tokens") and run["tokens_used"] > int(budget["max_tokens"]):
            return Stop("failed", "BUDGET_EXCEEDED", "超过 token 预算", nid)
        data = res.data
        if isinstance(data, dict) and isinstance(schema.get("properties"), dict):
            # 只保留 Schema 声明的字段：模型多吐出的任何“指令”字段都不会进入上下文
            data = {k: v for k, v in data.items() if k in schema["properties"]}
        errs = list(jsonschema.Draft202012Validator(schema).iter_errors(data))
        if errs:
            await self._set_step(run, nid, "failed", {"error": errs[0].message})
            return Stop("failed", "LLM_OUTPUT_INVALID", errs[0].message, nid)
        await self._done(run, n, data)
        return Next(n["next"], data)

    async def _human(self, run: dict[str, Any], n: dict[str, Any], ctx: dict[str, Any]) -> Next | Park | Stop:
        nid = n["id"]
        assignee = n["assignee"]
        role, at, where = assignee.partition("@")
        path = assignee_org_path(where) if at else None
        if path is not None:
            assignee = f"{role}@{resolve_args('$' + path, ctx)}"
        timeout = int(n.get("timeout_s") or DEFAULT_TASK_TIMEOUT_S)
        task_id = await self._create_task(
            run,
            nid,
            n["kind"],
            assignee,
            n.get("summary") or f"{run['capability_id']} 的 {nid} 需要处理",
            timeout,
            {"input_schema": n.get("input_schema")} if n["kind"] == "input" else None,
        )
        t = await self.task(task_id)
        assert t is not None
        status = t["status"]
        if status == "pending" and dt.datetime.fromisoformat(t["deadline"]) > _now():
            return Park(dt.datetime.fromisoformat(t["deadline"]))
        if status == "pending":
            async with self._tx() as conn:
                await conn.execute(
                    text("UPDATE exec.human_task SET status = 'timeout' WHERE id = :id AND status = 'pending'"),
                    {"id": task_id},
                )
            await self._journal(run, n, {"decision": "timeout"})
            if n.get("on_timeout"):
                return Next(n["on_timeout"], {"decision": "timeout"})
            return Stop("failed", "HUMAN_TASK_TIMEOUT", f"人工任务 {nid} 超时", nid)
        out = {"decision": status, "by": t["decided_by"], "data": ((t.get("data") or {}).get("submitted") or {})}
        await self._journal(run, n, out)
        if status == "rejected":
            if n.get("on_reject"):
                return Next(n["on_reject"], out)
            return Stop("rejected", "HUMAN_TASK_REJECTED", f"人工任务 {nid} 被驳回", nid)
        return Next(n["next"], out)

    async def _taint_gate(self, run: dict[str, Any], n: dict[str, Any], ctx: dict[str, Any]) -> Next | Park | Stop:
        """tainted 的 Run 在写 / 危险 / 对外发送之前强制人工确认（8.7），不论该 Tool 的 confirm_policy。"""
        gate = {
            **n,
            "id": f"{n['id']}#taint",
            "type": "human_task",
            "kind": "confirm",
            "assignee": "initiator" if run["end_user_id"] else f"role:data_owner@{run['company_id']}",
            "summary": f"本次运行读取过不可信内容（{', '.join(run['taint_sources'])}），执行 {n['ref']} 前需要人工确认",
        }
        out = await self._human(run, gate, ctx)
        return Next(n["id"]) if isinstance(out, Next) else out

    # ------------------------------------------------------------------ 补偿与收尾

    async def _fail(self, run: dict[str, Any], ctx: dict[str, Any], stop: Stop) -> None:
        err = {
            "code": stop.code,
            "message": stop.message,
            "node": stop.node,
            "final": stop.status,
            "compensating": True,
        }
        async with self._tx() as conn:
            await conn.execute(
                text(
                    "UPDATE exec.wf_run SET error = CAST(:e AS jsonb), ctx = CAST(:ctx AS jsonb), updated_at = now() "
                    "WHERE id = :id AND lease_owner = :me"
                ),
                {"id": str(run["id"]), "e": _j(err), "ctx": _j(ctx), "me": self.me},
            )
        await self._compensate_and_close(run, ctx, err)

    async def _compensate_and_close(self, run: dict[str, Any], ctx: dict[str, Any], err: dict[str, Any]) -> None:
        run_id = str(run["id"])
        principal = principal_from_json(run["principal"])
        nodes = {n["id"]: n for n in run["dsl"]["nodes"]}
        async with self._tx() as conn:
            done = (
                await conn.execute(
                    text(
                        "SELECT node_id FROM exec.wf_step WHERE run_id = :id AND kind = 'tool' AND status = 'done' "
                        "ORDER BY seq DESC"
                    ),
                    {"id": run_id},
                )
            ).all()
        compensated: list[dict[str, Any]] = []
        for (nid,) in done:
            n = nodes.get(nid)
            if not n or not n.get("compensation"):
                continue
            cap, version = split_ref(n["compensation"])
            res = await self.runtime.invoke(
                principal,
                cap,
                resolve_args(n.get("compensation_args") or {}, ctx),
                idempotency_key=f"wf:{run_id}:{nid}:comp",
                exposure="internal",
                version=version,
                trace_id=run["trace_id"] or "",
                run_id=run_id,
            )
            ok = res["status"] == "succeeded"
            compensated.append({"node": nid, "compensation": n["compensation"], "status": res["status"]})
            async with self._tx() as conn:
                await conn.execute(
                    text("UPDATE exec.wf_step SET status = :s WHERE run_id = :id AND node_id = :n"),
                    {"id": run_id, "n": nid, "s": "compensated" if ok else "compensation_failed"},
                )
        final = err.get("final", "failed")
        status = "compensated" if final == "failed" and compensated else final
        err = {k: v for k, v in err.items() if k not in ("compensating", "final")} | {"compensation": compensated}
        await self._close(run_id, run, status, ctx, error=err)

    async def _close(
        self,
        run_id: str,
        run: dict[str, Any],
        status: str,
        ctx: dict[str, Any],
        *,
        result: Any = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        async with self._tx() as conn:
            await conn.execute(
                text(
                    "UPDATE exec.wf_run SET status = :s, result = CAST(:r AS jsonb), error = CAST(:e AS jsonb), "
                    "ctx = CAST(:ctx AS jsonb), steps_used = :steps, llm_calls = :llm, tokens_used = :tok, "
                    "lease_owner = NULL, lease_until = NULL, wake_at = NULL, updated_at = now() WHERE id = :id"
                ),
                {
                    "id": run_id,
                    "s": status,
                    "r": _j(result),
                    "e": _j(error),
                    "ctx": _j(ctx),
                    "steps": run["steps_used"],
                    "llm": run["llm_calls"],
                    "tok": run["tokens_used"],
                },
            )
            await conn.execute(
                text("UPDATE exec.human_task SET status = 'cancelled' WHERE run_id = :id AND status = 'pending'"),
                {"id": run_id},
            )
        await self._audit_run(
            "workflow.run.finished",
            run_id,
            str(run["company_id"]),
            {
                "status": status,
                "steps_used": run["steps_used"],
                "llm_calls": run["llm_calls"],
                "tainted": run["tainted"],
                "error": (error or {}).get("code"),
                "trace_id": run["trace_id"],
            },
        )

    # ------------------------------------------------------------------ 持久化小工具

    async def _renew(self, run_id: str) -> None:
        async with self._tx() as conn:
            owned = (
                await conn.execute(
                    text(
                        "UPDATE exec.wf_run SET lease_until = now() + make_interval(secs => :lease) "
                        "WHERE id = :id AND lease_owner = :me RETURNING 1"
                    ),
                    {"id": run_id, "me": self.me, "lease": self.lease_s},
                )
            ).first()
        if not owned:
            raise RuntimeError("租约已被其他 Worker 接手")

    async def _advance(self, run: dict[str, Any], ctx: dict[str, Any], target: str) -> None:
        async with self._tx() as conn:
            await conn.execute(
                text(
                    "UPDATE exec.wf_run SET current = :c, ctx = CAST(:ctx AS jsonb), steps_used = :steps, "
                    "llm_calls = :llm, tokens_used = :tok, updated_at = now() WHERE id = :id AND lease_owner = :me"
                ),
                {
                    "id": str(run["id"]),
                    "c": target,
                    "ctx": _j(ctx),
                    "steps": run["steps_used"],
                    "llm": run["llm_calls"],
                    "tok": run["tokens_used"],
                    "me": self.me,
                },
            )

    async def _park(self, run_id: str, ctx: dict[str, Any], current: str, until: dt.datetime) -> None:
        async with self._tx() as conn:
            # 当前节点的任务可能在建任务与挂起之间就被处理了：那样立即可再认领，不必等到截止时间
            await conn.execute(
                text(
                    """UPDATE exec.wf_run SET status = 'waiting', current = :c, ctx = CAST(:ctx AS jsonb),
                         lease_owner = NULL, lease_until = NULL, updated_at = now(),
                         wake_at = CASE WHEN EXISTS (
                             SELECT 1 FROM exec.human_task
                             WHERE run_id = :id AND node_id IN (:c, :c || '#taint', :c || '#reconcile')
                               AND status IN ('approved', 'rejected', 'completed'))
                           THEN now() ELSE :until END
                       WHERE id = :id"""
                ),
                {"id": run_id, "c": current, "ctx": _j(ctx), "until": until},
            )

    async def _taint(self, run: dict[str, Any], source: str) -> None:
        run["tainted"] = True
        run["taint_sources"] = [*run["taint_sources"], source]
        async with self._tx() as conn:
            await conn.execute(
                text(
                    "UPDATE exec.wf_run SET tainted = true, taint_sources = array_append(taint_sources, :s) "
                    "WHERE id = :id"
                ),
                {"id": str(run["id"]), "s": source},
            )
        await self._audit_run("workflow.run.tainted", str(run["id"]), str(run["company_id"]), {"source": source})

    async def _get_step(self, run_id: str, nid: str) -> dict[str, Any] | None:
        async with self._tx() as conn:
            m = (
                (
                    await conn.execute(
                        text("SELECT * FROM exec.wf_step WHERE run_id = :id AND node_id = :n"), {"id": run_id, "n": nid}
                    )
                )
                .mappings()
                .first()
            )
        return dict(m) if m else None

    async def _mark_started(self, run: dict[str, Any], n: dict[str, Any]) -> None:
        async with self._tx() as conn:
            await conn.execute(
                text(
                    """INSERT INTO exec.wf_step (run_id, node_id, seq, company_id, kind, status, attempts,
                         started_at, executor)
                       VALUES (:id, :n, (SELECT count(*) FROM exec.wf_step WHERE run_id = :id), :company, :kind,
                               'started', 1, now(), :me)
                       ON CONFLICT (run_id, node_id) DO UPDATE SET attempts = exec.wf_step.attempts + 1,
                         status = 'started', executor = :me"""
                ),
                {
                    "id": str(run["id"]),
                    "n": n["id"],
                    "company": str(run["company_id"]),
                    "kind": n["type"],
                    "me": self.me,
                },
            )

    async def _set_step(
        self, run: dict[str, Any], nid: str, status: str, output: Any, execution_id: str | None = None
    ) -> None:
        async with self._tx() as conn:
            await conn.execute(
                text(
                    """INSERT INTO exec.wf_step (run_id, node_id, seq, company_id, kind, status, output, execution_id,
                         attempts, started_at, executor)
                       VALUES (:id, :n, (SELECT count(*) FROM exec.wf_step WHERE run_id = :id), :company, 'tool', :s,
                               CAST(:o AS jsonb), :ex, 1, now(), :me)
                       ON CONFLICT (run_id, node_id) DO UPDATE SET status = :s, output = CAST(:o AS jsonb),
                         execution_id = COALESCE(:ex, exec.wf_step.execution_id)"""
                ),
                {
                    "id": str(run["id"]),
                    "n": nid,
                    "company": str(run["company_id"]),
                    "s": status,
                    "o": _j(output),
                    "ex": execution_id,
                    "me": self.me,
                },
            )

    async def _done(self, run: dict[str, Any], n: dict[str, Any], output: Any, execution_id: str | None = None) -> None:
        async with self._tx() as conn:
            await conn.execute(
                text(
                    """INSERT INTO exec.wf_step (run_id, node_id, seq, company_id, kind, status, output, execution_id,
                         attempts, started_at, finished_at, executor)
                       VALUES (:id, :n, (SELECT count(*) FROM exec.wf_step WHERE run_id = :id), :company, :kind, 'done',
                               CAST(:o AS jsonb), :ex, 1, now(), now(), :me)
                       ON CONFLICT (run_id, node_id) DO UPDATE SET status = 'done', output = CAST(:o AS jsonb),
                         execution_id = COALESCE(:ex, exec.wf_step.execution_id), finished_at = now()"""
                ),
                {
                    "id": str(run["id"]),
                    "n": n["id"],
                    "company": str(run["company_id"]),
                    "kind": n["type"],
                    "o": _j(output),
                    "ex": execution_id,
                    "me": self.me,
                },
            )

    async def _journal(self, run: dict[str, Any], n: dict[str, Any], output: Any) -> Any:
        """非 Tool 步骤：首次写入，之后读回首次的值（重放时结果不变）。"""
        async with self._tx() as conn:
            await conn.execute(
                text(
                    """INSERT INTO exec.wf_step (run_id, node_id, seq, company_id, kind, status, output, attempts,
                         started_at, finished_at, executor)
                       VALUES (:id, :n, (SELECT count(*) FROM exec.wf_step WHERE run_id = :id), :company, :kind, 'done',
                               CAST(:o AS jsonb), 1, now(), now(), :me)
                       ON CONFLICT (run_id, node_id) DO NOTHING"""
                ),
                {
                    "id": str(run["id"]),
                    "n": n["id"],
                    "company": str(run["company_id"]),
                    "kind": n["type"],
                    "o": _j(output),
                    "me": self.me,
                },
            )
            return (
                await conn.execute(
                    text("SELECT output FROM exec.wf_step WHERE run_id = :id AND node_id = :n"),
                    {"id": str(run["id"]), "n": n["id"]},
                )
            ).scalar_one()

    async def _create_task(
        self,
        run: dict[str, Any],
        node_id: str,
        kind: str,
        assignee: str,
        summary: str,
        timeout_s: int,
        data: dict[str, Any] | None,
    ) -> str:
        user_id, role, org = self._resolve_assignee(run, assignee)
        deadline = min(_now() + dt.timedelta(seconds=timeout_s), run["deadline"])
        async with self._tx() as conn:
            await conn.execute(
                text(
                    """INSERT INTO exec.human_task (id, run_id, node_id, company_id, kind, assignee, assignee_user_id,
                         assignee_role, assignee_org_id, summary, status, deadline, data)
                       VALUES (:tid, :run, :n, :company, :kind, :assignee, :uid, :role, :org, :summary, 'pending',
                               :deadline, CAST(:data AS jsonb))
                       ON CONFLICT (run_id, node_id) DO NOTHING"""
                ),
                {
                    "tid": str(uuid.uuid4()),
                    "run": str(run["id"]),
                    "n": node_id,
                    "company": str(run["company_id"]),
                    "kind": kind,
                    "assignee": assignee,
                    "uid": user_id,
                    "role": role,
                    "org": org,
                    "summary": summary[:500],
                    "deadline": deadline,
                    "data": _j(data or {}),
                },
            )
            tid = (
                await conn.execute(
                    text("SELECT id FROM exec.human_task WHERE run_id = :run AND node_id = :n"),
                    {"run": str(run["id"]), "n": node_id},
                )
            ).scalar_one()
        return str(tid)

    def _resolve_assignee(self, run: dict[str, Any], assignee: str) -> tuple[str | None, str | None, str | None]:
        """initiator → 发起 Run 的用户；user:<id>；role:<角色>@<组织编码或 id>（只能是本公司或集团组织）。"""
        if assignee == "initiator":
            if run["end_user_id"]:
                return str(run["end_user_id"]), None, None
            return None, "data_owner", str(run["company_id"])
        if assignee.startswith("user:"):
            return assignee[5:], None, None
        role, _, where = assignee[5:].partition("@")
        orgs: dict[str, dict[str, Any]] = self.runtime.iam.data.get("orgs", {})
        org_id = where if where in orgs else next((oid for oid, o in orgs.items() if o.get("code") == where), None)
        if org_id is None:
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, f"人工任务的组织 {where} 不存在")
        if orgs[org_id].get("company_id") != str(run["company_id"]) and orgs[org_id].get("type") != "group":
            raise PlatformError(ErrorCode.PERMISSION_DENIED, "人工任务只能分派给本公司或集团层级的角色")
        return None, role, org_id

    async def _audit_run(self, kind: str, run_id: str, company_id: str, record: dict[str, Any]) -> None:
        await self.audit.append_async(kind, company_id=company_id, record={"run_id": run_id, **record})
