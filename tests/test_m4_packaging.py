"""部署打包：src 下用到的第三方包都必须是运行时依赖（镜像只装运行时依赖）。"""

from __future__ import annotations

import ast
import pathlib
import sys
import tomllib

ROOT = pathlib.Path(__file__).resolve().parents[1]
# import 名 → 发行包名（或随其他包一起安装的模块）
ALIAS = {
    "jwt": "pyjwt",
    "argon2": "argon2_cffi",
    "httpcore": "httpx",
    "starlette": "fastapi",
    "multipart": "python_multipart",
    "yaml": "pyyaml",
    "opentelemetry": "opentelemetry_api",
}


def test_runtime_imports_are_declared_dependencies() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    # 可选 extras（sdk、pii）只允许函数内延迟导入，未安装时按功能降级
    deps = project["dependencies"] + [d for ds in project.get("optional-dependencies", {}).values() for d in ds]
    declared = {d.split(">")[0].split("[")[0].split("=")[0].strip().lower().replace("-", "_") for d in deps}
    used: set[str] = set()
    for f in (ROOT / "src").rglob("*.py"):
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                used |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                used.add(node.module.split(".")[0])
    third_party = {m for m in used if m not in sys.stdlib_module_names and m != "aip"}
    missing = sorted(m for m in third_party if ALIAS.get(m, m).lower() not in declared)
    assert missing == [], f"这些包只在开发环境里有，镜像中会缺失：{missing}"


def test_image_contains_runtime_contract_files() -> None:
    dockerfile = (ROOT / "deploy" / "Dockerfile").read_text(encoding="utf-8")
    for path in ("docs/specs", "policies", "migrations", "alembic.ini"):
        assert f"COPY {path}" in dockerfile
