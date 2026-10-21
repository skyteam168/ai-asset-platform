"""A2A 1.0 最小出口（architecture.md 10）：Agent Card + JSON-RPC SendMessage / GetTask。

仅暴露 asset_type=workflow 且 exposure 含 a2a、经 PDP 可见的能力；执行走统一链（exposure=a2a），
Task.id = run_id。本片不做 streaming / CancelTask / 确认回传。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from aip.data_plane.oauth import AuthContext
from aip.data_plane.principal import Principal
from aip.data_plane.runtime import ToolRuntime
from aip.libs.common.config import get_settings
from aip.libs.common.errors import ErrorCode, PlatformError

log = logging.getLogger("aip.a2a")

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602

# A2A 1.0 TaskState（JSON 枚举名）
STATE_WORKING = "TASK_STATE_WORKING"
STATE_COMPLETED = "TASK_STATE_COMPLETED"
STATE_FAILED = "TASK_STATE_FAILED"
STATE_CANCELED = "TASK_STATE_CANCELED"
STATE_REJECTED = "TASK_STATE_REJECTED"
STATE_INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None, *, http_status: int = 200) -> None:
        self.code, self.message, self.data, self.http_status = code, message, data, http_status
        super().__init__(message)


def _task_state_from_view(view: dict[str, Any]) -> str:
    st = view["status"]
    if st == "waiting" and view.get("pending_tasks"):
        return STATE_INPUT_REQUIRED
    if st == "running":
        return STATE_WORKING
    if st == "succeeded":
        return STATE_COMPLETED
    if st in ("rejected",):
        return STATE_REJECTED
    if st in ("cancelled",):
        return STATE_CANCELED
    return STATE_FAILED


def _task_state_from_invoke(res: dict[str, Any]) -> str:
    st = res["status"]
    if st == "pending_confirm":
        return STATE_INPUT_REQUIRED
    if st == "running":
        return STATE_WORKING
    if st == "succeeded":
        return STATE_COMPLETED
    if st in ("rejected",):
        return STATE_REJECTED
    if st in ("cancelled",):
        return STATE_CANCELED
    return STATE_FAILED


def _task_from_view(view: dict[str, Any]) -> dict[str, Any]:
    state = _task_state_from_view(view)
    task: dict[str, Any] = {
        "id": view["run_id"],
        "status": {"state": state},
    }
    err = view.get("error")
    if err and isinstance(err, dict) and err.get("message"):
        task["status"]["message"] = err["message"]
    if state == STATE_COMPLETED and view.get("result") is not None:
        task["artifacts"] = [
            {
                "artifactId": "result",
                "parts": [{"kind": "data", "data": view["result"]}],
            }
        ]
    return task


def _task_from_invoke(res: dict[str, Any]) -> dict[str, Any]:
    run_id = res.get("run_id") or res.get("execution_id")
    state = _task_state_from_invoke(res)
    task: dict[str, Any] = {
        "id": run_id,
        "status": {"state": state},
    }
    if state == STATE_COMPLETED and res.get("data") is not None:
        task["artifacts"] = [
            {
                "artifactId": "result",
                "parts": [{"kind": "data", "data": res["data"]}],
            }
        ]
    if state == STATE_INPUT_REQUIRED:
        nxt = res.get("next_action") or {}
        msg = nxt.get("message") or "需要确认或人工处理（请用 REST confirm / 平台待办）"
        task["status"]["message"] = msg
        if nxt.get("confirmation_id"):
            task["metadata"] = {"confirmationId": nxt["confirmation_id"]}
    err = res.get("error")
    if state == STATE_FAILED and isinstance(err, dict):
        task["status"]["message"] = err.get("code") or res["status"]
    return task


def _message_meta(message: dict[str, Any]) -> dict[str, Any]:
    meta = message.get("metadata")
    return meta if isinstance(meta, dict) else {}


def _extract_capability(message: dict[str, Any]) -> str:
    meta = _message_meta(message)
    for key in ("capabilityId", "skillId", "capability_id", "skill_id"):
        val = meta.get(key)
        if isinstance(val, str) and val:
            return val
    raise RpcError(INVALID_PARAMS, "message.metadata.capabilityId（或 skillId）必填")


def _extract_args(message: dict[str, Any]) -> dict[str, Any]:
    meta = _message_meta(message)
    raw_args = meta.get("args")
    if isinstance(raw_args, dict):
        return dict(raw_args)
    parts = message.get("parts")
    if not isinstance(parts, list):
        return {}
    for part in parts:
        if not isinstance(part, dict):
            continue
        if isinstance(part.get("data"), dict):
            return dict(part["data"])
        text = part.get("text")
        if isinstance(text, str) and text.strip():
            try:
                parsed = json.loads(text)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                return parsed
    return {}


class A2aGateway:
    def __init__(self, auth: AuthContext, runtime: ToolRuntime) -> None:
        self.auth, self.runtime = auth, runtime

    async def visible_workflows(self, principal: Principal) -> list[dict[str, Any]]:
        policy = self.runtime.policy
        out: list[dict[str, Any]] = []
        for rel in self.runtime.releases.releases(policy.env):
            if (
                rel["asset_type"] != "workflow"
                or rel["channel"] != "stable"
                or rel["status"] != "active"
                or "a2a" not in rel["exposure"]
            ):
                continue
            if await policy.visible(principal, rel, exposure="a2a"):
                out.append(rel)
        out.sort(key=lambda r: r["capability_id"])
        return out

    async def is_visible(self, principal: Principal, capability_id: str) -> bool:
        try:
            rel = self.runtime.releases.resolve(capability_id, self.runtime.policy.env)
        except PlatformError:
            return False
        if rel["asset_type"] != "workflow" or "a2a" not in rel["exposure"]:
            return False
        return await self.runtime.policy.visible(principal, rel, exposure="a2a")

    def agent_card(self, skills: list[dict[str, Any]]) -> dict[str, Any]:
        s = get_settings()
        base = s.issuer.rstrip("/")
        return {
            "name": "AIP Workflow Agent",
            "description": "企业 AI 资产管理中台：经 A2A 挂载的 Workflow Agent（执行留在平台）。",
            "version": s.version,
            "supportedInterfaces": [
                {
                    "url": f"{base}/a2a",
                    "protocolBinding": "JSONRPC",
                    "protocolVersion": "1.0",
                }
            ],
            "capabilities": {
                "streaming": False,
                "pushNotifications": False,
                "extendedAgentCard": False,
            },
            "defaultInputModes": ["application/json"],
            "defaultOutputModes": ["application/json"],
            "securitySchemes": {
                "bearer": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"},
            },
            "security": [{"bearer": []}],
            "skills": skills,
        }

    def _skill(self, rel: dict[str, Any]) -> dict[str, Any]:
        spec = rel["spec"]
        return {
            "id": rel["capability_id"],
            "name": spec.get("name") or rel["name"] or rel["capability_id"],
            "description": spec.get("description") or "",
            "tags": ["workflow", "aip"],
            "inputModes": ["application/json"],
            "outputModes": ["application/json"],
        }

    async def handle_card(self, request: Request) -> Response:
        try:
            principal = await self.auth.principal_from_request(
                request.headers.get("authorization"), request.headers.get("x-user-assertion")
            )
        except PlatformError as e:
            if e.status == 401:
                return self._unauthorized(request, e)
            return JSONResponse(
                e.problem(request.url.path, getattr(request.state, "trace_id", "")),
                status_code=e.status,
                media_type="application/problem+json",
            )
        rels = await self.visible_workflows(principal)
        return JSONResponse(self.agent_card([self._skill(r) for r in rels]))

    async def handle_rpc(self, request: Request) -> Response:
        try:
            body = json.loads(await request.body() or b"null")
        except ValueError:
            return _rpc_error(None, RpcError(PARSE_ERROR, "Parse error"))
        if not isinstance(body, dict) or body.get("jsonrpc") != "2.0" or "method" not in body:
            return _rpc_error(
                body.get("id") if isinstance(body, dict) else None,
                RpcError(INVALID_REQUEST, "Invalid Request"),
            )
        rid, method, params = body.get("id"), body["method"], body.get("params") or {}
        if rid is None:
            return Response(status_code=202)
        if not isinstance(params, dict):
            return _rpc_error(rid, RpcError(INVALID_PARAMS, "params 必须是对象"))
        try:
            principal = await self.auth.principal_from_request(
                request.headers.get("authorization"), request.headers.get("x-user-assertion")
            )
            if method == "SendMessage":
                result = await self._send_message(params, principal, request)
            elif method == "GetTask":
                result = await self._get_task(params, principal)
            else:
                raise RpcError(METHOD_NOT_FOUND, f"Method not found: {method}")
        except PlatformError as e:
            if e.status == 401:
                return self._unauthorized(request, e)
            return _rpc_error(
                rid, RpcError(INVALID_PARAMS if e.status < 500 else -32000, f"{e.code}: {e.detail}")
            )
        except RpcError as e:
            return _rpc_error(rid, e)
        return JSONResponse({"jsonrpc": "2.0", "id": rid, "result": result})

    def _unauthorized(self, request: Request, e: PlatformError) -> JSONResponse:
        meta = f"{get_settings().issuer}/.well-known/oauth-protected-resource"
        return JSONResponse(
            e.problem(request.url.path, getattr(request.state, "trace_id", "")),
            status_code=401,
            headers={"WWW-Authenticate": f'Bearer resource_metadata="{meta}"'},
            media_type="application/problem+json",
        )

    async def _send_message(
        self, params: dict[str, Any], principal: Principal, request: Request
    ) -> dict[str, Any]:
        message = params.get("message")
        if not isinstance(message, dict):
            raise RpcError(INVALID_PARAMS, "params.message 必填")
        capability_id = _extract_capability(message)
        if not await self.is_visible(principal, capability_id):
            raise RpcError(INVALID_PARAMS, f"Unknown or unauthorized skill: {capability_id}")
        args = _extract_args(message)
        meta = _message_meta(message)
        key = request.headers.get("idempotency-key") or meta.get("idempotencyKey")
        if key is not None and not isinstance(key, str):
            key = None
        trace = getattr(request.state, "trace_id", "") or ""
        res = await self.runtime.invoke(
            principal,
            capability_id,
            args,
            idempotency_key=key,
            exposure="a2a",
            trace_id=trace,
        )
        task = _task_from_invoke(res)
        if not task.get("id"):
            raise RpcError(-32000, f"invoke 未返回 run_id（status={res.get('status')}）")
        return {"task": task}

    async def _get_task(self, params: dict[str, Any], principal: Principal) -> dict[str, Any]:
        task_id = params.get("id") or params.get("taskId") or params.get("name")
        if not isinstance(task_id, str) or not task_id:
            raise RpcError(INVALID_PARAMS, "params.id 必填")
        if self.runtime.workflows is None:
            raise RpcError(INVALID_PARAMS, "本数据面未启用 Workflow 引擎")
        try:
            view = await self.runtime.workflows.view(task_id, consumer_id=principal.consumer_id)
        except PlatformError as e:
            if e.code == ErrorCode.NOT_FOUND:
                raise RpcError(INVALID_PARAMS, "Unknown task") from e
            raise RpcError(INVALID_PARAMS, f"{e.code}: {e.detail}") from e
        return {"task": _task_from_view(view)}


def _rpc_error(rid: Any, e: RpcError) -> JSONResponse:
    err: dict[str, Any] = {"code": e.code, "message": e.message}
    if e.data is not None:
        err["data"] = e.data
    return JSONResponse({"jsonrpc": "2.0", "id": rid, "error": err}, status_code=e.http_status)


def router(gateway: A2aGateway) -> APIRouter:
    r = APIRouter(tags=["a2a"])

    @r.get("/.well-known/agent-card.json", operation_id="agentCard")
    async def agent_card(request: Request) -> Response:
        """A2A Agent Card（需认证；skills 为已授权且 exposure 含 a2a 的 Workflow）。"""
        return await gateway.handle_card(request)

    @r.post("/a2a", operation_id="a2a")
    async def a2a(request: Request) -> Response:
        """A2A 1.0 JSON-RPC（SendMessage / GetTask）。"""
        return await gateway.handle_rpc(request)

    return r
