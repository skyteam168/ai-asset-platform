"""H22：risk / webhook / skill response_model。"""

from __future__ import annotations

from typing import Any, get_origin

from fastapi import FastAPI
from fastapi.routing import APIRoute
from pydantic import BaseModel

from aip.control_plane.app import create_app
from aip.control_plane.risk import schemas as risk_out
from aip.control_plane.skill import schemas as skill_out
from aip.control_plane.webhook import schemas as wh_out
from aip.libs.common.paging import Page


def _flatten(routes: list[Any]) -> list[Any]:
    out: list[Any] = []
    for r in routes:
        inner = getattr(r, "original_router", None)
        out += _flatten(list(inner.routes)) if inner is not None else [r]
    return out


def _route(app: FastAPI, op_id: str) -> APIRoute:
    for r in _flatten(list(app.routes)):
        if isinstance(r, APIRoute) and r.operation_id == op_id:
            return r
    raise AssertionError(f"missing route {op_id}")


def test_risk_webhook_skill_response_models() -> None:
    app = create_app()
    expected: dict[str, Any] = {
        "listRiskRules": Page[risk_out.RiskRule],
        "updateRiskRule": risk_out.RiskRule,
        "listRiskEvents": Page[risk_out.RiskEvent],
        "acknowledgeRiskEvent": risk_out.RiskEvent,
        "requestRiskResume": risk_out.RiskResumeRequested,
        "runRiskScan": risk_out.RiskScanResult,
        "createWebhook": wh_out.WebhookCreated,
        "listWebhooks": Page[wh_out.WebhookEndpoint],
        "rotateWebhookSecret": wh_out.WebhookSecretRotated,
        "setWebhookStatus": wh_out.WebhookStatus,
        "listWebhookDeliveries": Page[wh_out.WebhookDelivery],
        "processWebhooks": wh_out.WebhookProcessResult,
        "uploadSkillPackage": skill_out.SkillPackageUploaded,
        "listSkills": Page[skill_out.SkillCatalogItem],
        "getSkillManifest": skill_out.SkillManifest,
        "skillInstallCheck": skill_out.SkillInstallCheck,
    }
    for op_id, model in expected.items():
        route = _route(app, op_id)
        rm = route.response_model
        assert rm is not None and rm is not dict, op_id
        if get_origin(model) is Page:
            assert get_origin(rm) is Page, op_id
        else:
            assert isinstance(rm, type) and issubclass(rm, BaseModel) and rm is model, op_id
