"""H13：可观测 compose / 配置文件可解析。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
OTEL = ROOT / "deploy" / "otel"


def test_otel_yaml_configs_parse() -> None:
    for name in ("otel-collector-config.yaml", "tempo.yaml", "prometheus.yml"):
        data = yaml.safe_load((OTEL / name).read_text(encoding="utf-8"))
        assert isinstance(data, dict) and data


def test_grafana_dashboard_json() -> None:
    import json

    dash = json.loads((OTEL / "grafana" / "dashboards" / "aip-overview.json").read_text(encoding="utf-8"))
    assert dash["uid"] == "aip-otel-overview"
    assert dash["panels"]


def test_compose_otel_config_valid() -> None:
    r = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(ROOT / "deploy" / "docker-compose.otel.yml"),
            "config",
            "--quiet",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert r.returncode == 0, r.stderr or r.stdout
