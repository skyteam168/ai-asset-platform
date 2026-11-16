"""H16：Kubernetes 清单经 kustomize 可构建，且关键安全/就绪探针约定成立。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
K8S = ROOT / "deploy" / "k8s"


def _kubectl() -> str | None:
    return shutil.which("kubectl")


def _build(overlay: Path) -> list[dict[str, Any]]:
    exe = _kubectl()
    if exe is None:
        pytest.skip("kubectl 不可用")
    proc = subprocess.run(
        [exe, "kustomize", str(overlay)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    docs = [d for d in yaml.safe_load_all(proc.stdout) if d]
    assert docs, f"empty build: {overlay}"
    return docs


def _by_kind(docs: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [d for d in docs if d.get("kind") == kind]


def test_kustomize_base_and_prod_build() -> None:
    base = _build(K8S / "base")
    prod = _build(K8S / "overlays" / "prod")
    assert {d["kind"] for d in base} >= {
        "Namespace",
        "ConfigMap",
        "Deployment",
        "StatefulSet",
        "Service",
        "Job",
        "PodDisruptionBudget",
    }
    assert len(prod) >= len(base)


def test_data_plane_fail_closed_readyz_and_security() -> None:
    docs = _build(K8S / "base")
    sts = _by_kind(docs, "StatefulSet")[0]
    assert sts["metadata"]["name"] == "data-plane"
    pod = sts["spec"]["template"]["spec"]
    assert pod["securityContext"]["runAsNonRoot"] is True
    dp = next(c for c in pod["containers"] if c["name"] == "data-plane")
    assert dp["readinessProbe"]["httpGet"]["path"] == "/readyz"
    assert dp["livenessProbe"]["httpGet"]["path"] == "/healthz"
    assert "ALL" in dp["securityContext"]["capabilities"]["drop"]
    assert dp["securityContext"]["readOnlyRootFilesystem"] is True
    opa = next(c for c in pod["containers"] if c["name"] == "opa")
    assert "127.0.0.1" in " ".join(opa.get("args") or [])


def test_control_plane_security_and_pdb() -> None:
    docs = _build(K8S / "base")
    dep = _by_kind(docs, "Deployment")[0]
    assert dep["metadata"]["name"] == "control-plane"
    c = dep["spec"]["template"]["spec"]["containers"][0]
    assert c["securityContext"]["allowPrivilegeEscalation"] is False
    assert "ALL" in c["securityContext"]["capabilities"]["drop"]
    pdbs = {p["metadata"]["name"]: p for p in _by_kind(docs, "PodDisruptionBudget")}
    assert "control-plane" in pdbs and "data-plane" in pdbs
    assert pdbs["control-plane"]["spec"]["minAvailable"] >= 1
    assert pdbs["data-plane"]["spec"]["minAvailable"] >= 2


def test_prod_overlay_env_and_replicas() -> None:
    docs = _build(K8S / "overlays" / "prod")
    cm = next(d for d in docs if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "aip-config")
    assert cm["data"]["AIP_ENV"] == "prod"
    assert cm["data"]["AIP_RUNTIME_MODE"] == "live"
    sts = next(d for d in docs if d.get("kind") == "StatefulSet")
    assert sts["spec"]["replicas"] == 4


def test_region_overlays_build_and_label() -> None:
    """G6.6：双机房草稿 overlay 可 kustomize；待集团确认 RPO/RTO 前不作生产切流。"""
    for name in ("region-a", "region-b"):
        docs = _build(K8S / "overlays" / name)
        cm = next(d for d in docs if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "aip-config")
        assert cm["data"]["AIP_REGION"] == name
        assert cm["data"]["AIP_RUNTIME_MODE"] == "live"
        sts = next(d for d in docs if d.get("kind") == "StatefulSet" and d["metadata"]["name"] == "data-plane")
        assert sts["spec"]["replicas"] == 2
        assert sts["metadata"]["labels"].get("aip.region") == name


def test_secret_ref_not_inlined() -> None:
    """机密只通过 Secret 引用注入，清单内不得出现明文口令字段。"""
    text = (K8S / "base" / "config.yaml").read_text(encoding="utf-8")
    assert "aip-dev-only" not in text
    assert "password" not in text.lower()
    docs = _build(K8S / "base")
    for d in docs:
        assert "aip-dev-only" not in yaml.safe_dump(d)
        if d.get("kind") not in {"Deployment", "StatefulSet", "Job"}:
            continue
        for c in d["spec"]["template"]["spec"]["containers"]:
            if c["name"] not in {"control-plane", "data-plane", "migrate"}:
                continue
            names = {(r.get("secretRef") or {}).get("name") for r in (c.get("envFrom") or [])}
            assert "aip-secrets" in names, c["name"]
