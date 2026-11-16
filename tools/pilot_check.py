"""Wave P：约 200 人试点自检（不要求 CNPG / 双机房 / K8s）。

用法：
  .venv/Scripts/python tools/pilot_check.py
  AIP_ENV=staging AIP_SERVICE_AUTH_MODE=jwt AIP_SIGNING_KEY_PEM=... .venv/Scripts/python tools/pilot_check.py

检查：
  - Settings 在 staging/prod 下能否通过「无 dev-only」校验（若当前 AIP_ENV 为 staging/prod）
  - 或在 dev 下打印「尚未切到 staging」提示（exit 0，带 WARN）
  - compose 依赖容器是否在跑（可选）
  - 控制面 / 数据面 healthz（可选）
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

COMPOSE = ["docker", "compose", "-f", str(ROOT / "deploy" / "docker-compose.yml")]
SERVICES = ("postgres", "valkey", "openbao", "opa", "seaweedfs")


def _ok(msg: str) -> None:
    print(f"OK  {msg}")


def _warn(msg: str) -> None:
    print(f"WARN {msg}")


def _fail(msg: str) -> None:
    print(f"FAIL {msg}")


def check_settings() -> list[str]:
    from pydantic import ValidationError

    from aip.libs.common.config import Settings

    problems: list[str] = []
    env = (os.environ.get("AIP_ENV") or "dev").strip()
    try:
        # 绕过 get_settings 缓存，按当前环境变量重新解析
        s = Settings()
    except ValidationError as e:
        problems.append(f"Settings 校验失败: {e}")
        return problems
    if s.env not in ("staging", "prod"):
        _warn(f"当前 AIP_ENV={s.env!r}（试点上线前请改为 staging 或 prod 并去掉 dev-only）")
        return problems
    if s.service_auth_mode == "static":
        problems.append("AIP_SERVICE_AUTH_MODE=static（试点须 jwt 或 both）")
    if "dev-only" in s.database_url or "dev-only" in s.app_database_url:
        problems.append("数据库 URL 仍含 dev-only")
    if s.s3_secret_key == "aip-dev-only":
        _warn("AIP_S3_SECRET_KEY 仍为 aip-dev-only（建议试点更换）")
    _ok(f"Settings env={s.env} service_auth_mode={s.service_auth_mode}")
    return problems


def check_compose(*, strict: bool) -> list[str]:
    problems: list[str] = []
    try:
        proc = subprocess.run(
            [*COMPOSE, "ps", "--status", "running", "--services"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
    except OSError as e:
        msg = f"无法执行 docker compose: {e}"
        if strict:
            problems.append(msg)
        else:
            _warn(msg)
        return problems
    if proc.returncode != 0:
        msg = "docker compose ps 失败（未安装 Docker 或未在本机跑依赖时可忽略）"
        if strict:
            problems.append(msg)
        else:
            _warn(msg)
        return problems
    running = {x.strip() for x in (proc.stdout or "").splitlines() if x.strip()}
    for name in SERVICES:
        if name in running:
            _ok(f"compose 服务运行中: {name}")
        else:
            msg = f"compose 服务未运行: {name}（先 tools/dev.py up）"
            if strict:
                problems.append(msg)
            else:
                _warn(msg)
    return problems


def check_healthz() -> list[str]:
    problems: list[str] = []
    for port, name in ((18081, "control-plane"), (18082, "data-plane")):
        url = f"http://127.0.0.1:{port}/healthz"
        try:
            with urlopen(url, timeout=3) as r:  # noqa: S310 — 仅本机健康检查
                body = r.read().decode("utf-8", errors="replace")
            if r.status != 200:
                problems.append(f"{name} healthz HTTP {r.status}")
            else:
                _ok(f"{name} healthz ok ({body[:80]})")
        except URLError as e:
            _warn(f"{name} 未监听 :{port}（未 serve 时可忽略）: {e}")
    return problems


def main() -> int:
    print("=== Wave P pilot_check（约 200 人试点）===")
    env = (os.environ.get("AIP_ENV") or "dev").strip()
    strict = env in ("staging", "prod") or os.environ.get("AIP_PILOT_STRICT") == "1"
    problems: list[str] = []
    problems.extend(check_settings())
    problems.extend(check_compose(strict=strict))
    problems.extend(check_healthz())
    if problems:
        for p in problems:
            _fail(p)
        print("结果: FAIL — 见上；说明见 docs/ops/pilot-200.md")
        return 1
    print("结果: OK — 依赖与当前环境通过试点自检（不等于企业级 G6 全勾）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
