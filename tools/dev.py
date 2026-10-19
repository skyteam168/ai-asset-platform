"""跨平台任务入口（Windows 无 make 时使用；Makefile 只是它的薄封装）。

python tools/dev.py up        启动基础设施并执行迁移
python tools/dev.py down      停止基础设施（保留数据卷）
python tools/dev.py migrate   alembic upgrade head
python tools/dev.py lint      ruff + mypy + TLS 约束检查
python tools/dev.py docs      生成并校验 ADR / JSON Schema / OpenAPI，链接检查，spectral（需 npx）
python tools/dev.py test      pytest（含 infra 用例，需先 up）
python tools/dev.py ci        docs + lint + test
python tools/dev.py bootstrap 初始化集团、总部与首个平台管理员（只运行一次）
python tools/dev.py serve     本地运行控制面(18081)与数据面(18082)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
COMPOSE = ["docker", "compose", "-f", str(ROOT / "deploy" / "docker-compose.yml")]


def run(cmd: list[str], check: bool = True) -> int:
    print("$", " ".join(cmd), flush=True)
    rc = subprocess.call(cmd, cwd=ROOT)
    if check and rc != 0:
        sys.exit(rc)
    return rc


def wait_postgres(timeout: float = 60) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if (
            subprocess.call(
                [*COMPOSE, "exec", "-T", "postgres", "pg_isready", "-U", "aip", "-d", "aip"],
                cwd=ROOT,
                stdout=subprocess.DEVNULL,
            )
            == 0
        ):
            return
        time.sleep(1)
    sys.exit("postgres not ready")


def up() -> None:
    run([*COMPOSE, "up", "-d"])
    wait_postgres()
    migrate()


def down() -> None:
    run([*COMPOSE, "down"])


def migrate() -> None:
    run([PY, "-m", "alembic", "upgrade", "head"])
    # dev 专用：给应用角色设置登录口令（生产由 OpenBao 动态数据库凭据管理）
    run(
        [
            *COMPOSE,
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "aip",
            "-d",
            "aip",
            "-q",
            "-c",
            "ALTER ROLE aip_app WITH LOGIN PASSWORD 'aip-app-dev-only'",
        ]
    )


def lint() -> None:
    run([PY, "-m", "ruff", "check", "src", "tests", "tools", "migrations"])
    run([PY, "-m", "mypy"])
    run([PY, "tools/check_tls_usage.py"])


def docs() -> None:
    run([PY, "tools/gen_adrs.py"])
    run([PY, "tools/gen_schemas.py"])
    run([PY, "tools/gen_openapi.py"])
    run(["git", "diff", "--exit-code", "--", "docs/specs", "docs/api", "docs/adr"])  # 生成物必须已提交
    run([PY, "tools/check_links.py"])
    npx = shutil.which("npx.cmd") or shutil.which("npx")
    if npx:
        run(
            [
                npx,
                "--yes",
                "@stoplight/spectral-cli@6.14.2",
                "lint",
                "--ruleset",
                ".spectral.yaml",
                "docs/api/control-plane.openapi.yaml",
                "docs/api/data-plane.openapi.yaml",
            ]
        )
    else:
        print("skip spectral: npx not found")


def test() -> None:
    run([PY, "-m", "pytest", "-q", *sys.argv[2:]])


def dev_env() -> dict[str, str]:
    """本地运行用的共享配置：两个进程必须用同一把签名密钥，否则数据面会拒绝控制面签发的快照。"""
    key_file = ROOT / ".aip-data" / "dev-signing-key.pem"
    if not key_file.exists():
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        key_file.parent.mkdir(parents=True, exist_ok=True)
        pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
        key_file.write_bytes(pem)
    return {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "AIP_SIGNING_KEY_PEM": key_file.read_text(encoding="utf-8"),
        "AIP_CONTROL_PLANE_URL": "http://127.0.0.1:18081",
        "AIP_AUDIT_DATABASE_URL": "postgresql+asyncpg://aip_app:aip-app-dev-only@127.0.0.1:55432/aip",
        "AIP_RUNTIME_DATABASE_URL": "postgresql+asyncpg://aip_app:aip-app-dev-only@127.0.0.1:55432/aip",
    }


def bootstrap() -> None:
    """初始化集团、总部与首个平台管理员（只运行一次）；口令与 TOTP 写入 .aip-data/dev-admin.txt。"""
    import secrets

    password = secrets.token_urlsafe(18)
    out = subprocess.run(
        [PY, "tools/bootstrap.py", "--admin-login", "admin", "--admin-password", password],
        cwd=ROOT,
        env=dev_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout
    cred = ROOT / ".aip-data" / "dev-admin.txt"
    cred.write_text(f"login_name=admin\npassword={password}\n{out}", encoding="utf-8")
    print(f"bootstrap ok；管理员凭据见 {cred.relative_to(ROOT)}（仅 dev）")


def serve() -> None:
    env = dev_env()
    procs = [
        subprocess.Popen(
            [PY, "-m", "uvicorn", "aip.control_plane.app:app", "--port", "18081", "--app-dir", "src"],
            cwd=ROOT,
            env=env,
        ),
        subprocess.Popen(
            [PY, "-m", "uvicorn", "aip.data_plane.app:app", "--port", "18082", "--app-dir", "src"],
            cwd=ROOT,
            env=env,
        ),
    ]
    try:
        for p in procs:
            p.wait()
    finally:
        for p in procs:
            p.terminate()


TASKS = {
    "up": up,
    "down": down,
    "migrate": migrate,
    "lint": lint,
    "docs": docs,
    "test": test,
    "ci": lambda: (docs(), lint(), test()),
    "serve": serve,
    "bootstrap": bootstrap,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in TASKS:
        print(__doc__)
        sys.exit(2)
    TASKS[sys.argv[1]]()
