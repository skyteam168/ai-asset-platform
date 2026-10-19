"""禁止在 aip.libs.httpclient 之外关闭证书校验（architecture.md 9.6 实现约束 1）。

ruff 的 banned-api 拦截 ssl.CERT_NONE / _create_unverified_context；本脚本补充拦截关键字参数
verify=False（httpx、requests 等）与 check_hostname = False 赋值。用法：python tools/check_tls_usage.py [路径...]
"""

from __future__ import annotations

import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
ALLOWED = ROOT / "src" / "aip" / "libs" / "httpclient"
DEFAULT_PATHS = [ROOT / "src", ROOT / "connectors"]


def violations(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.keyword)
            and node.arg == "verify"
            and isinstance(node.value, ast.Constant)
            and node.value.value is False
        ):
            found.append(f"{path}:{node.value.lineno}: verify=False")
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if (
                    isinstance(t, ast.Attribute)
                    and t.attr == "check_hostname"
                    and isinstance(node.value, ast.Constant)
                    and node.value.value is False
                ):
                    found.append(f"{path}:{node.lineno}: check_hostname = False")
                if isinstance(t, ast.Attribute) and t.attr == "verify_mode":
                    found.append(f"{path}:{node.lineno}: verify_mode 赋值")
    return found


def main(argv: list[str]) -> int:
    roots = [pathlib.Path(a) for a in argv] or DEFAULT_PATHS
    problems: list[str] = []
    for root in roots:
        files = [root] if root.is_file() else sorted(root.rglob("*.py")) if root.exists() else []
        for f in files:
            if ALLOWED in f.resolve().parents:
                continue
            problems += violations(f)
    for p in problems:
        print(p)
    if problems:
        print(f"{len(problems)} 处违反 TLS 约束：出站 HTTP 必须经 aip.libs.httpclient.build_client")
        return 1
    print("tls-usage ok")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
