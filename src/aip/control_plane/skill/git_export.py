"""Skill Git bare 镜像分发（architecture.md 10.4）：与 zip 内容哈希一致。"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from aip.control_plane.skill import package as pkg
from aip.libs.common.errors import ErrorCode, PlatformError


def _run_git(args: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    full_env = {**os.environ, **(env or {})}
    r = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env=full_env,
    )
    if r.returncode != 0:
        detail = (r.stderr or r.stdout or "").strip()[:500]
        raise PlatformError(ErrorCode.INTERNAL, f"git {' '.join(args)} 失败：{detail}")


def bare_repo_path(git_root: Path, name: str) -> Path:
    safe = name.replace("/", "_").replace("\\", "_")
    return git_root / "bare" / f"{safe}.git"


def publish_skill_to_git(
    *,
    git_root: Path,
    name: str,
    semver: str,
    zip_bytes: bytes,
    content_sha256: str,
    public_base: str = "",
) -> dict[str, Any]:
    """把 zip 解压写入 worktree 并推到 bare 仓库；tag = v{semver}，branch = main。"""
    git_root = git_root.resolve()
    git_root.mkdir(parents=True, exist_ok=True)
    bare = bare_repo_path(git_root, name)
    files = pkg.unpack_zip(zip_bytes, check_scripts=False)

    with tempfile.TemporaryDirectory(prefix="aip-skill-git-") as tmp:
        work = Path(tmp) / "work"
        work.mkdir()
        if not bare.exists():
            bare.parent.mkdir(parents=True, exist_ok=True)
            _run_git(["init", "--bare", "-b", "main", str(bare)])
        _run_git(["init", "-b", "main"], cwd=work)
        _run_git(["remote", "add", "origin", str(bare)], cwd=work)
        # 若已有历史，先拉再覆盖工作区
        fetch = subprocess.run(
            ["git", "fetch", "origin", "main"],
            cwd=work,
            capture_output=True,
            text=True,
            check=False,
        )
        if fetch.returncode == 0:
            subprocess.run(
                ["git", "checkout", "-B", "main", "origin/main"],
                cwd=work,
                capture_output=True,
                check=False,
            )
        for p in list(work.iterdir()):
            if p.name == ".git":
                continue
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
        for path, data in files.items():
            dest = work / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        (work / ".aip-skill-meta").write_text(
            f"name={name}\nversion={semver}\ncontent_sha256={content_sha256}\n",
            encoding="utf-8",
        )
        _run_git(["add", "-A"], cwd=work)
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=work,
            capture_output=True,
            text=True,
            check=False,
        )
        env = {
            "GIT_AUTHOR_NAME": "AIP Skill Registry",
            "GIT_AUTHOR_EMAIL": "skill@aip.internal",
            "GIT_COMMITTER_NAME": "AIP Skill Registry",
            "GIT_COMMITTER_EMAIL": "skill@aip.internal",
        }
        if status.stdout.strip():
            _run_git(["-c", "user.email=skill@aip.internal", "-c", "user.name=AIP", "commit", "-m", f"skill {name}@{semver}"], cwd=work, env=env)
        elif not (work / ".git" / "HEAD").exists() or not list((work / ".git" / "refs" / "heads").glob("*")):
            # 空提交防止无历史
            _run_git(
                ["-c", "user.email=skill@aip.internal", "-c", "user.name=AIP", "commit", "--allow-empty", "-m", f"skill {name}@{semver}"],
                cwd=work,
                env=env,
            )
        tag = f"v{semver}"
        subprocess.run(["git", "tag", "-d", tag], cwd=work, capture_output=True, check=False)
        _run_git(["-c", "user.email=skill@aip.internal", "-c", "user.name=AIP", "tag", "-a", tag, "-m", tag], cwd=work, env=env)
        _run_git(["push", "origin", "HEAD:main", "--force"], cwd=work)
        _run_git(["push", "origin", tag, "--force"], cwd=work)

    clone_url = f"{public_base.rstrip('/')}/{name}.git" if public_base else str(bare)
    return {
        "name": name,
        "version": semver,
        "git_url": clone_url,
        "bare_path": str(bare),
        "tag": f"v{semver}",
        "content_sha256": content_sha256,
    }


def verify_git_checkout_matches(bare: Path, tag: str, manifest: dict[str, str]) -> list[str]:
    """测试辅助：检出 tag 后比对清单（忽略 .aip-skill-meta）。"""
    with tempfile.TemporaryDirectory(prefix="aip-skill-git-v-") as tmp:
        work = Path(tmp) / "co"
        _run_git(["clone", "--branch", tag, str(bare), str(work)])
        files: dict[str, bytes] = {}
        for p in work.rglob("*"):
            if not p.is_file() or ".git" in p.parts:
                continue
            rel = p.relative_to(work).as_posix()
            if rel == ".aip-skill-meta":
                continue
            files[rel] = p.read_bytes()
        return pkg.local_verify(files, manifest)
