"""M0 / P0：两个服务的健康检查、错误格式，以及“实现的路由必须在 OpenAPI 契约里”。"""

from __future__ import annotations

import pathlib

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aip.control_plane.app import create_app as create_cp
from aip.data_plane.app import create_app as create_dp

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_control_plane_healthz() -> None:
    r = TestClient(create_cp()).get("/healthz")
    assert r.status_code == 200
    assert r.json()["service"] == "control-plane"
    assert len(r.headers["X-Trace-Id"]) == 32


def test_data_plane_healthz_and_fail_closed_readiness() -> None:
    c = TestClient(create_dp())
    assert c.get("/healthz").json()["status"] == "ok"
    r = c.get("/readyz")
    assert r.status_code == 503, "没有快照与策略时数据面必须不就绪"
    assert r.json()["checks"] == {"iam_snapshot": "missing", "release_snapshot": "missing", "policy_bundle": "missing"}


def test_traceparent_is_propagated() -> None:
    tp = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    r = TestClient(create_dp()).get("/healthz", headers={"traceparent": tp})
    assert r.headers["X-Trace-Id"] == "4bf92f3577b34da6a3ce929d0e0e4736"


def test_unknown_path_returns_problem_json() -> None:
    r = TestClient(create_cp()).get("/api/v1/nope")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["code"] == "NOT_FOUND"


def _routes(app: FastAPI) -> set[tuple[str, str]]:
    out = set()
    for r in app.routes:
        methods = getattr(r, "methods", None) or set()
        path = getattr(r, "path", "")
        if path.startswith(("/docs", "/openapi", "/redoc")):
            continue
        out |= {(m.lower(), path) for m in methods if m not in {"HEAD", "OPTIONS"}}
    return out


@pytest.mark.parametrize(("factory", "spec"), [(create_cp, "control-plane"), (create_dp, "data-plane")])
def test_implemented_routes_are_in_contract(factory: object, spec: str) -> None:
    contract = yaml.safe_load((ROOT / f"docs/api/{spec}.openapi.yaml").read_text(encoding="utf-8"))
    declared = {(m, p) for p, ops in contract["paths"].items() for m in ops}
    implemented = _routes(factory())  # type: ignore[operator]
    assert implemented <= declared, f"未登记到 OpenAPI 的路由：{implemented - declared}"
