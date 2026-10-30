"""控制台只读接口响应模型。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from aip.control_plane.registry.schemas import AssetVersion


class VersionSummary(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    semver: str
    state: str
    content_hash: str | None = None
    created_at: str | None = None


class VersionDetail(AssetVersion):
    """版本详情：AssetVersion + 用例与验收单。"""

    test_cases: list[dict[str, Any]] = Field(default_factory=list)
    acceptance: dict[str, Any] | None = None


class UsageRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    company_id: str | None = None
    asset_id: str | None = None
    status: str | None = None
    env: str | None = None
    created_at: str | None = None


class ConsoleAuditRecord(BaseModel):
    """控制台审计检索行（与库列对齐，异于历史契约里的简化 AuditRecord）。"""

    model_config = ConfigDict(extra="ignore")

    shard_id: str
    seq: int
    occurred_at: str
    kind: str
    hash: str
    company_id: str | None = None
    record: dict[str, Any] = Field(default_factory=dict)


class AuditVerifyResult(BaseModel):
    ok: bool
    problems: list[str] = Field(default_factory=list)
    shards: int = 0
    records: int = 0


class CatalogTool(BaseModel):
    model_config = ConfigDict(extra="ignore")

    capability_id: str | None = None
    version: str | None = None
    title: str | None = None
    description: str = ""
    risk_level: str = "danger"
    egress_class: str = "none"
    data_classification: int | None = None
    bundles: list[str] = Field(default_factory=list)
    connector: str | None = None
    system_key: str | None = None
    system_host: str | None = None
    company_id: str | None = None
    owner_org_id: str | None = None
    asset_id: str | None = None
    # 未过期且 status=active 的 Grant 条数（运维参考；MCP 可见性仍按调用方 Principal 判定）
    active_grant_count: int = 0


class CatalogBundle(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str | None = None
    capability_id: str | None = None
    version: str | None = None
    description: str = ""
    tools: list[str] = Field(default_factory=list)


class ConsoleCatalog(BaseModel):
    items: list[CatalogTool]
    bundles: list[CatalogBundle] = Field(default_factory=list)
    next_cursor: str | None = None
    has_more: bool = False


class UsageMeterRow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    day: str | None = None
    company_id: str | None = None
    consumer_id: str | None = None
    capability_id: str | None = None
    calls: int | None = None
    failed: int | None = None
    writes: int | None = None


class UsageReport(BaseModel):
    items: list[UsageMeterRow]


class UsageCounterRow(BaseModel):
    """usage_counter 成本分摊行（G5.2）。"""

    model_config = ConfigDict(extra="ignore")

    group_by: str
    dim: str
    company_id: str | None = None
    calls: int = 0
    writes: int = 0


class UsageCounterReport(BaseModel):
    from_day: str
    to_day: str
    group_by: str
    items: list[UsageCounterRow]
