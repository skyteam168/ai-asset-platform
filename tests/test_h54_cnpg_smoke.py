"""H54：CNPG/kind 冒烟脚本（无集群 skip）。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_cnpg_kind_smoke_exits_ok_without_cluster() -> None:
    import os

    env = {**os.environ, "AIP_CNPG_SMOKE": "0", "PYTHONIOENCODING": "utf-8"}
    r = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "cnpg_kind_smoke.py")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    out = (r.stdout or "") + (r.stderr or "")
    assert r.returncode == 0, out
    assert "skip:" in out or "ok:" in out


def test_pg_ha_prod_mentions_h54() -> None:
    text = (ROOT / "docs" / "ops" / "pg-ha-prod.md").read_text(encoding="utf-8")
    assert "H54" in text
    assert "cnpg_kind_smoke" in text
