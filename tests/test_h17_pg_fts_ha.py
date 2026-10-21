"""H17：pg_trgm 迁移 + CloudNativePG 示例清单。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from sqlalchemy import text

from aip.control_plane.db import Database, DbScope

ROOT = Path(__file__).resolve().parents[1]
CNPG = ROOT / "deploy" / "k8s" / "cnpg" / "cluster.yaml"

pytestmark = pytest.mark.infra


@pytest.mark.asyncio
async def test_pg_trgm_extension_present(db: Database) -> None:
    async with db.tx(DbScope.system()) as conn:
        q = text("SELECT extname FROM pg_extension WHERE extname = 'pg_trgm'")
        name = (await conn.execute(q)).scalar_one_or_none()
    assert name == "pg_trgm"


def test_cnpg_sample_yaml_parses() -> None:
    docs = [d for d in yaml.safe_load_all(CNPG.read_text(encoding="utf-8")) if d]
    kinds = {(d["kind"], d["metadata"]["name"]) for d in docs}
    assert ("Cluster", "aip-pg") in kinds
    assert ("ScheduledBackup", "aip-pg-daily") in kinds
    cluster = next(d for d in docs if d["kind"] == "Cluster")
    assert cluster["spec"]["instances"] >= 3
    assert "barmanObjectStore" in cluster["spec"]["backup"]


def test_cnpg_not_in_base_kustomization() -> None:
    text_ = (ROOT / "deploy" / "k8s" / "base" / "kustomization.yaml").read_text(encoding="utf-8")
    assert "cnpg" not in text_
