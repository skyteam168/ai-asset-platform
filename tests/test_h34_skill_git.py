"""H34：Skill Git bare 镜像与 zip 内容哈希一致。"""

from __future__ import annotations

from pathlib import Path

import pytest

from aip.control_plane.db import Database, DbScope
from aip.control_plane.gates import service as gates
from aip.control_plane.registry import service as registry
from aip.control_plane.skill import git_export
from aip.control_plane.skill import package as pkg
from aip.control_plane.skill import service as skill
from aip.libs.common.config import get_settings
from aip.libs.tokens.keys import SigningKey
from iam_seed import Seed
from test_m4_gates import SECRETS, _people
from test_m6_skill import _force_active, _zip_skill

pytestmark = pytest.mark.infra
KEY = SigningKey.generate()


async def test_publish_git_matches_zip_manifest(db: Database, seeded: Seed, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AIP_SKILL_GIT_ROOT", str(tmp_path / "git-data"))
    get_settings.cache_clear()
    p = await _people(db, seeded)
    raw = _zip_skill(name="git-demo-skill", tools=None)
    async with db.tx(DbScope.system()) as conn:
        aid = await registry.create_asset(
            conn, p.dev, type_="skill", name="skill.git_demo", owner_org_id=seeded.hq, classification=2
        )
        vid = await registry.create_version(
            conn,
            p.dev,
            aid,
            "1.0.0",
            {"name": "git-demo-skill", "description": "Git 分发演示 Skill 描述足够十个字符。"},
        )
        await gates.set_acceptance(conn, p.dev, vid, {"scenarios": ["git"]})
        attached = await skill.attach_package(conn, p.dev, vid, raw, key=KEY)
        assert (await gates.submit(conn, p.dev, vid))["passed"]
        lab = await gates.run_lab(conn, p.dev, vid, secrets=SECRETS)
        assert lab["passed"], lab
        await _force_active(conn, p.dev, vid)
        published = await skill.publish_git(conn, p.dev, vid, git_root=str(tmp_path / "git-data"))
        assert published["git_url"]
        assert published["tag"] == "v1.0.0"
        bare = Path(published["bare_path"])
        problems = git_export.verify_git_checkout_matches(bare, "v1.0.0", attached["manifest"])
        assert problems == []
        url = await skill.get_git_url(conn, vid)
        assert url["git_url"] == published["git_url"]
    get_settings.cache_clear()


def test_publish_skill_to_git_unit(tmp_path: Path) -> None:
    files = {
        "SKILL.md": (
            b"---\nname: unit-git\ndescription: unit test skill description long enough.\n---\n\n# hi\n"
        )
    }
    raw = pkg.make_zip(files)
    digest = __import__("hashlib").sha256(raw).hexdigest()
    out = git_export.publish_skill_to_git(
        git_root=tmp_path,
        name="unit-git",
        semver="0.1.0",
        zip_bytes=raw,
        content_sha256=digest,
        public_base="https://git.example/skills",
    )
    assert out["git_url"] == "https://git.example/skills/unit-git.git"
    problems = git_export.verify_git_checkout_matches(Path(out["bare_path"]), "v0.1.0", pkg.build_manifest(files))
    assert problems == []
