"""H42：生产清单文档与 prod-check。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from aip.libs.prod_check import REQUIRED_SECTIONS, run_prod_check

ROOT = Path(__file__).resolve().parents[1]


def test_checklist_sections_present() -> None:
    problems = run_prod_check(ROOT)
    assert problems == [], problems


def test_required_sections_constant_matches_doc() -> None:
    md = (ROOT / "docs" / "ops" / "production-checklist.md").read_text(encoding="utf-8")
    for sec in REQUIRED_SECTIONS:
        assert f"## {sec}" in md


def test_prod_check_cli_exits_zero() -> None:
    # 无 kubectl 时 k8s-check 可能失败；prod-check 内含 k8s-check
    # 此处只验文档/Settings 部分逻辑（run_prod_check）已由上例覆盖；
    # CLI 完整路径在有 kubectl 的环境跑 tools/dev.py prod-check。
    rc = subprocess.call(
        [sys.executable, "-c", "from aip.libs.prod_check import run_prod_check; "
         "from pathlib import Path; "
         "p=run_prod_check(Path('.').resolve()); "
         "raise SystemExit(1 if p else 0)"],
        cwd=ROOT,
    )
    assert rc == 0
