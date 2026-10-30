"""跨平台任务入口（Windows 无 make 时使用；Makefile 只是它的薄封装）。

python tools/dev.py up        启动基础设施并执行迁移
python tools/dev.py down      停止基础设施（保留数据卷）
python tools/dev.py migrate   alembic upgrade head
python tools/dev.py lint      ruff + mypy + TLS 约束检查
python tools/dev.py docs      生成并校验 ADR / JSON Schema / OpenAPI，链接检查，spectral（需 npx）
python tools/dev.py test      pytest（含 infra 用例，需先 up）
python tools/dev.py ci        docs + lint + test
python tools/dev.py bootstrap 初始化集团、总部与首个平台管理员（只运行一次）
python tools/dev.py seed-demo  M5 演示数据（需先 serve）
python tools/dev.py activate-nike  Nike 导入草稿走完门禁/授权/REST·MCP（需 serve + 已导入 + seed-demo）
python tools/dev.py activate-equipment  设备管家 RSA+JWT 真连接 → Tool → 门禁 → REST/MCP（需 serve + seed-demo）
python tools/dev.py serve     本地运行控制面(18081)与数据面(18082)
python tools/dev.py otel-up   启动 OTel Collector / Tempo / Prometheus / Grafana
python tools/dev.py serve-otel  同上 serve，并开启 AIP_OTEL_EXPORTER=otlp
python tools/dev.py k8s-check  kubectl kustomize 构建 base 与 prod overlay
python tools/dev.py loadtest [--workers …]  数据面压测（透传 tools/loadtest.py）
python tools/dev.py zhparser-up  可选：构建并启动带 zhparser 的 PostgreSQL
python tools/dev.py prod-check 生产清单本地校验（kustomize + 文档章节）
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
COMPOSE_OTEL = [
    "docker",
    "compose",
    "-f",
    str(ROOT / "deploy" / "docker-compose.otel.yml"),
]
OTEL_HTTP = "http://127.0.0.1:14318/v1/traces"


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
    run([PY, "tools/gen_console_api_types.py"])  # G1.4：控制台 OpenAPI 类型生成物须提交
    run(["git", "diff", "--exit-code", "--", "docs/specs", "docs/api", "docs/adr", "console/src/api-types.generated.ts"])
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


def seed_demo() -> None:
    """M5 演示数据：采购单审核 Workflow 与需审批的付款调用（需先 serve）。"""
    subprocess.run(
        [PY, "tools/seed_m5_demo.py"], cwd=ROOT, env={**dev_env(), "PYTHONPATH": str(ROOT / "src")}, check=True
    )


def activate_nike() -> None:
    """Nike 导入草稿 → 门禁 → Grant → REST/MCP（需先 serve + 已导入 Collection + seed-demo）。"""
    subprocess.run(
        [PY, "tools/activate_nike_path.py"],
        cwd=ROOT,
        env={**dev_env(), "PYTHONPATH": str(ROOT / "src"), "PYTHONIOENCODING": "utf-8"},
        check=True,
    )


def activate_equipment() -> None:
    """设备管家 RSA 登录握手 → 声明式 Tool → 门禁 → Grant → REST/MCP。"""
    subprocess.run(
        [PY, "tools/activate_equipment_path.py"],
        cwd=ROOT,
        env={**dev_env(), "PYTHONPATH": str(ROOT / "src"), "PYTHONIOENCODING": "utf-8"},
        check=True,
    )


def serve(*, with_otel: bool = False) -> None:
    env = dev_env()
    # G2.1：可选 AIP_SERVICE_AUTH_MODE=jwt 逼近生产；默认 both 兼容静态回退
    mode = env.get("AIP_SERVICE_AUTH_MODE") or "both"
    env = {**env, "AIP_SERVICE_AUTH_MODE": mode}
    kid = ""
    try:
        from aip.libs.tokens.keys import SigningKey

        kid = SigningKey.from_pem(env["AIP_SIGNING_KEY_PEM"]).kid
    except Exception:
        kid = "?"
    print(f"service-auth: mode={mode} shared_signing_kid={kid} (.aip-data/dev-signing-key.pem)")
    if with_otel:
        env = {
            **env,
            "AIP_OTEL_EXPORTER": "otlp",
            "AIP_OTEL_ENDPOINT": env.get("AIP_OTEL_ENDPOINT") or OTEL_HTTP,
        }
        print(f"otel: exporter=otlp endpoint={env['AIP_OTEL_ENDPOINT']}")
        print("grafana: http://127.0.0.1:13000/  （需先 otel-up）")
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


def otel_up() -> None:
    """启动 OTel Collector + Tempo + Prometheus + Grafana（deploy/docker-compose.otel.yml）。"""
    run([*COMPOSE_OTEL, "up", "-d"])
    print("otel stack up")
    print("  OTLP HTTP  http://127.0.0.1:14318")
    print("  Grafana    http://127.0.0.1:13000/  (admin / aip-dev-only；匿名 Viewer)")
    print("  Tempo      http://127.0.0.1:13200")
    print("  Prometheus http://127.0.0.1:19090")
    print("然后：python tools/dev.py serve-otel")


def otel_down() -> None:
    run([*COMPOSE_OTEL, "down"])


def k8s_check() -> None:
    """本地校验 deploy/k8s kustomize 构建（需 kubectl；不连接集群）。"""
    kubectl = shutil.which("kubectl")
    if kubectl is None:
        raise SystemExit("需要 kubectl（仅用于 kustomize 构建，无需集群）")
    for path in (
        ROOT / "deploy" / "k8s" / "base",
        ROOT / "deploy" / "k8s" / "overlays" / "prod",
    ):
        cmd = [kubectl, "kustomize", str(path)]
        print("$", " ".join(cmd), flush=True)
        proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
        if proc.returncode != 0:
            sys.stderr.write(proc.stderr or proc.stdout or "kustomize failed\n")
            raise SystemExit(proc.returncode)
        if not proc.stdout.strip():
            raise SystemExit(f"empty kustomize output: {path}")
    print("k8s kustomize ok")


def loadtest(argv: list[str] | None = None) -> None:
    """透传参数给 tools/loadtest.py。"""
    run([PY, str(ROOT / "tools" / "loadtest.py"), *(argv or [])])


COMPOSE_ZHPARSER = [
    "docker",
    "compose",
    "-f",
    str(ROOT / "deploy" / "docker-compose.yml"),
    "-f",
    str(ROOT / "deploy" / "docker-compose.zhparser.yml"),
]


def zhparser_up() -> None:
    """构建带 zhparser 的 Postgres 并替换默认库（会重建 postgres 卷）。"""
    run([*COMPOSE, "stop", "postgres"], check=False)
    run([*COMPOSE, "rm", "-f", "-v", "postgres"], check=False)
    run([*COMPOSE_ZHPARSER, "build", "postgres"])
    run([*COMPOSE_ZHPARSER, "up", "-d", "postgres"])
    deadline = time.time() + 90
    while time.time() < deadline:
        if (
            subprocess.call(
                [*COMPOSE_ZHPARSER, "exec", "-T", "postgres", "pg_isready", "-U", "aip", "-d", "aip"],
                cwd=ROOT,
                stdout=subprocess.DEVNULL,
            )
            == 0
        ):
            break
        time.sleep(1)
    else:
        sys.exit("zhparser postgres not ready")
    migrate()
    print("zhparser postgres ready on 55432")


def prod_check() -> None:
    """生产清单本地校验：kustomize + 文档章节 + Settings 环境变量名对照。"""
    from aip.libs.prod_check import run_prod_check

    k8s_check()
    problems = run_prod_check(ROOT)
    if problems:
        for p in problems:
            print(f"FAIL: {p}", flush=True)
        raise SystemExit(1)
    print("prod-check ok")


TASKS = {
    "up": up,
    "down": down,
    "migrate": migrate,
    "lint": lint,
    "docs": docs,
    "test": test,
    "ci": lambda: (docs(), lint(), test()),
    "serve": serve,
    "serve-otel": lambda: serve(with_otel=True),
    "otel-up": otel_up,
    "otel-down": otel_down,
    "k8s-check": k8s_check,
    "loadtest": loadtest,  # 实际由 __main__ 传 argv
    "zhparser-up": zhparser_up,
    "prod-check": prod_check,
    "bootstrap": bootstrap,
    "seed-demo": seed_demo,
    "activate-nike": activate_nike,
    "activate-equipment": activate_equipment,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in TASKS:
        print(__doc__)
        sys.exit(2)
    name = sys.argv[1]
    if name == "loadtest":
        loadtest(sys.argv[2:])
    else:
        TASKS[name]()
