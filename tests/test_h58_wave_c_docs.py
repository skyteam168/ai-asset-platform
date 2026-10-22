"""H58：pgvector / 成本分摊 / 双机房文档与 prod-check。"""

from __future__ import annotations

from pathlib import Path

from aip.libs.prod_check import REQUIRED_SECTIONS, run_prod_check

ROOT = Path(__file__).resolve().parents[1]


def test_wave_c_docs() -> None:
    backlog = (ROOT / "docs" / "ops" / "wave-c-backlog.md").read_text(encoding="utf-8")
    assert "pgvector" in backlog
    assert "成本分摊" in backlog or "分摊" in backlog
    assert "双机房" in backlog
    search = (ROOT / "docs" / "ops" / "search-index.md").read_text(encoding="utf-8")
    assert "pgvector" in search


def test_prod_check_wave_c_sections() -> None:
    for sec in ("SMTP / 飞书", "Connector worker", "pgvector / 双机房"):
        assert sec in REQUIRED_SECTIONS
    assert run_prod_check(ROOT) == []
