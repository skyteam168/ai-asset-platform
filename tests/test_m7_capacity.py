"""M7l：多进程容量冒烟（architecture.md 非功能 / docs/ops/slo.md）。

全量校准见 tools/loadtest.py；本文件只做轻量 CI，避免拖垮流水线。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from loadtest import measure  # noqa: E402

pytestmark = pytest.mark.infra


async def test_multi_worker_rest_scales_and_no_errors() -> None:
    """workers=1 与 2（真进程）：errors=0；双进程聚合 rps ≥ 单进程 × 1.15。"""
    one = await measure(1, requests=200, concurrency=20, kinds=("rest",))
    two = await measure(2, requests=200, concurrency=20, kinds=("rest",))
    assert one["rest"]["errors"] == 0, one
    assert two["rest"]["errors"] == 0, two
    assert two["rest"]["rps"] >= one["rest"]["rps"] * 1.15, {
        "one": one["rest"],
        "two": two["rest"],
        "ratio": two["rest"]["rps"] / max(one["rest"]["rps"], 0.1),
    }
