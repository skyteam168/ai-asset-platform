"""H54：可选 kind/k3d + CNPG 冒烟（无集群则 exit 0 + skip 说明）。

用法：
  .venv/Scripts/python tools/cnpg_kind_smoke.py
环境：
  AIP_CNPG_SMOKE=1  强制要求集群（无则 exit 2）
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def main() -> int:
    force = os.environ.get("AIP_CNPG_SMOKE") == "1"
    kubectl = shutil.which("kubectl")
    if not kubectl:
        print("skip: kubectl not in PATH (see docs/ops/pg-ha-prod.md)")
        return 2 if force else 0
    ctx = _run([kubectl, "config", "current-context"])
    if ctx.returncode != 0 or not (ctx.stdout or "").strip():
        print("skip: no kube context")
        return 2 if force else 0
    # 仅检查 CNPG CRD 是否已装；不强制 apply（避免误伤共享集群）
    crd = _run([kubectl, "get", "crd", "clusters.postgresql.cnpg.io"])
    if crd.returncode != 0:
        print("skip: CloudNativePG CRD missing (see docs/ops/pg-ha-prod.md)")
        return 2 if force else 0
    print(f"ok: context={(ctx.stdout or '').strip()} CNPG CRD present")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sample = os.path.join(root, "deploy", "k8s", "cnpg", "cluster.yaml")
    if os.path.isfile(sample):
        dry = _run([kubectl, "apply", "--dry-run=client", "-f", sample])
        if dry.returncode != 0:
            print((dry.stderr or dry.stdout or "dry-run failed")[:2000])
            return 1
        print(f"ok: dry-run client apply {sample}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
