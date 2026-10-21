"""Docker 拉起 Stoplight Prism Mock（architecture.md 7.2）。"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
import yaml

from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.common.ids import new_id

PRISM_IMAGE = "stoplight/prism:5.12.0"
SPEC_NAME = "openapi.yaml"


@dataclass
class PrismSession:
    id: uuid.UUID
    base_url: str
    container_name: str
    port: int
    spec_dir: Path
    ready_url: str


def docker_available() -> bool:
    """PATH 中有 docker 即可；Windows 上 which 可能指向非 Win32 入口，实际调用仍用命令名 docker。"""
    return shutil.which("docker") is not None


def _docker_run(args: list[str]) -> subprocess.CompletedProcess[str]:
    # 使用命令名而非 which 绝对路径（Docker Desktop 在 Windows 上 which 常不可 CreateProcess）
    return subprocess.run(  # noqa: S603
        ["docker", *args],  # noqa: S607
        check=False,
        capture_output=True,
        text=True,
    )


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _load_openapi(document: str) -> dict[str, Any]:
    text = document.strip()
    if not text:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "OpenAPI 文档为空")
    try:
        if text[0] in "{[":
            doc = json.loads(text)
        else:
            doc = yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError) as e:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, f"无法解析 OpenAPI：{e}") from e
    if not isinstance(doc, dict) or not str(doc.get("openapi") or "").startswith("3."):
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "仅支持 OpenAPI 3.x")
    if not isinstance(doc.get("paths"), dict) or not doc["paths"]:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "OpenAPI 缺少 paths")
    return doc


def _param_probe_value(param: dict[str, Any]) -> str | None:
    schema = param.get("schema") or {}
    if "example" in schema:
        return str(schema["example"])
    if "default" in schema:
        return str(schema["default"])
    if schema.get("enum"):
        return str(schema["enum"][0])
    t = schema.get("type", "string")
    name = str(param.get("name") or "").lower()
    if t == "integer":
        return "1"
    if name == "plant" or (isinstance(schema.get("pattern"), str) and "F" in schema["pattern"]):
        return "F001"
    if t == "boolean":
        return "true"
    return "x"


def default_ready_probe(doc: dict[str, Any]) -> tuple[str, dict[str, str]]:
    """返回 (path, query) 用于就绪探测。"""
    for path, item in (doc.get("paths") or {}).items():
        if not isinstance(item, dict) or "get" not in item:
            continue
        op = item["get"]
        if not isinstance(op, dict):
            continue
        query: dict[str, str] = {}
        for p in list(item.get("parameters") or []) + list(op.get("parameters") or []):
            if not isinstance(p, dict) or p.get("in") != "query":
                continue
            if not p.get("required"):
                continue
            name = str(p.get("name") or "")
            if not name:
                continue
            val = _param_probe_value(p)
            if val is not None:
                query[name] = val
        return path, query
    return "/", {}


def start_mock(
    document: str,
    *,
    ready_path: str | None = None,
    ready_timeout_s: float = 30.0,
    session_id: uuid.UUID | None = None,
) -> PrismSession:
    """写临时 OpenAPI 并 docker run Prism；失败时清理。"""
    if not docker_available():
        raise PlatformError(ErrorCode.PRECONDITION_FAILED, "本机未安装或未找到 docker，无法启动 Prism Mock")
    doc = _load_openapi(document)
    path, query = default_ready_probe(doc)
    if ready_path:
        path = ready_path if ready_path.startswith("/") else f"/{ready_path}"
        query = {}
    sid = session_id or new_id()
    port = _free_port()
    name = f"aip-prism-{sid.hex[:12]}"
    spec_dir = Path(tempfile.mkdtemp(prefix="aip-prism-"))
    # Prism 容器内须听 0.0.0.0；宿主机仅绑定 127.0.0.1:port
    listen_all = "0.0.0.0"  # noqa: S104
    try:
        (spec_dir / SPEC_NAME).write_text(document.strip() + "\n", encoding="utf-8")
        proc = _docker_run(
            [
                "run",
                "-d",
                "--rm",
                "--name",
                name,
                "-p",
                f"127.0.0.1:{port}:4010",
                "-v",
                f"{spec_dir.resolve()}:/specs",
                PRISM_IMAGE,
                "mock",
                "-h",
                listen_all,
                f"/specs/{SPEC_NAME}",
            ]
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "docker run 失败").strip()
            raise PlatformError(ErrorCode.PRECONDITION_FAILED, f"启动 Prism 失败：{detail[:200]}")
        base = f"http://127.0.0.1:{port}"
        qs = f"?{urlencode(query)}" if query else ""
        ready_url = f"{base}{path}{qs}"
        deadline = time.time() + ready_timeout_s
        last_err = "timeout"
        while time.time() < deadline:
            try:
                r = httpx.get(ready_url, timeout=1.0)
                if r.status_code < 500:
                    return PrismSession(
                        id=sid,
                        base_url=base,
                        container_name=name,
                        port=port,
                        spec_dir=spec_dir,
                        ready_url=ready_url,
                    )
                last_err = f"HTTP {r.status_code}"
            except httpx.HTTPError as e:
                last_err = str(e)
            time.sleep(0.5)
        _docker_run(["rm", "-f", name])
        raise PlatformError(ErrorCode.PRECONDITION_FAILED, f"Prism 未在时限内就绪：{last_err}")
    except PlatformError:
        shutil.rmtree(spec_dir, ignore_errors=True)
        raise
    except Exception as e:
        try:
            _docker_run(["rm", "-f", name])
        except OSError:
            pass
        shutil.rmtree(spec_dir, ignore_errors=True)
        raise PlatformError(ErrorCode.PRECONDITION_FAILED, f"启动 Prism 异常：{e}") from e


def stop_mock(session: PrismSession) -> None:
    try:
        _docker_run(["rm", "-f", session.container_name])
    except OSError:
        pass
    shutil.rmtree(session.spec_dir, ignore_errors=True)
