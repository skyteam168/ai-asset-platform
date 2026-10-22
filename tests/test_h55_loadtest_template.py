"""H55：多副本压测结果表模板存在于 slo.md。"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_slo_has_k8s_result_table() -> None:
    text = (ROOT / "docs" / "ops" / "slo.md").read_text(encoding="utf-8")
    assert "## 10. 多副本真实压测结果表（H55）" in text
    assert "loadtest-k8s-4replicas" in text
    assert "| 日期 | 集群 | 副本×worker |" in text
