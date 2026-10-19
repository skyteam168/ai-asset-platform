"""M0 / P0：CI 必须拦截关闭证书校验的写法（附录 B T-GEN-N7）。"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]

BAD = """import ssl
import httpx

def f() -> None:
    httpx.get("https://10.0.0.1", verify=False)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
"""


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    return subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", env=env)


def test_repo_is_clean() -> None:
    r = _run("tools/check_tls_usage.py")
    assert r.returncode == 0, r.stdout


def test_ast_check_flags_violations(tmp_path: pathlib.Path) -> None:
    f = tmp_path / "bad_connector.py"
    f.write_text(BAD, encoding="utf-8")
    r = _run("tools/check_tls_usage.py", str(f))
    assert r.returncode == 1
    assert "verify=False" in r.stdout and "check_hostname = False" in r.stdout and "verify_mode" in r.stdout


def test_ruff_banned_api_flags_cert_none(tmp_path: pathlib.Path) -> None:
    target = ROOT / "src" / "aip" / "connectors" / "_tmp_bad_for_test.py"
    target.write_text(BAD, encoding="utf-8")
    try:
        r = _run("-m", "ruff", "check", "--no-cache", "--select", "TID251", str(target))
    finally:
        target.unlink()
    assert r.returncode == 1
    assert "ssl.CERT_NONE" in r.stdout


def test_httpclient_package_is_exempt() -> None:
    allowed = ROOT / "src" / "aip" / "libs" / "httpclient" / "_tmp_ok_for_test.py"
    allowed.write_text(BAD, encoding="utf-8")
    try:
        r = _run("tools/check_tls_usage.py")
    finally:
        allowed.unlink()
    assert r.returncode == 0
