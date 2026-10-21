"""Prism Mock HTTP：按 OpenAPI 自动拉起（architecture.md 7.2）。"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field

from aip.control_plane.iam.api import ActorDep
from aip.control_plane.testlab import mock_service as svc

V = "/api/v1"


class PrismMockIn(BaseModel):
    document: str = Field(min_length=1, max_length=2_000_000)
    ttl_seconds: int = Field(default=svc.DEFAULT_TTL, ge=60, le=svc.MAX_TTL)
    ready_path: str | None = Field(default=None, max_length=512)


def router() -> APIRouter:
    r = APIRouter(tags=["testlab"])

    @r.post(f"{V}/prism-mocks", operation_id="createPrismMock", status_code=201)
    async def create(body: PrismMockIn, actor: ActorDep) -> dict[str, Any]:
        return svc.create_mock(
            actor,
            document=body.document,
            ttl_seconds=body.ttl_seconds,
            ready_path=body.ready_path,
        )

    @r.get(f"{V}/prism-mocks/{{id}}", operation_id="getPrismMock")
    async def get(id: uuid.UUID, actor: ActorDep) -> dict[str, Any]:
        return svc.get_mock(actor, id)

    @r.delete(f"{V}/prism-mocks/{{id}}", operation_id="deletePrismMock", status_code=204)
    async def delete(id: uuid.UUID, actor: ActorDep) -> Response:
        svc.delete_mock(actor, id)
        return Response(status_code=204)

    return r
