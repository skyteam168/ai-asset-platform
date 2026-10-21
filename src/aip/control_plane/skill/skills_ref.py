"""G0：调用官方 skills-ref 校验 Agent Skills 目录（architecture.md 7.1 / 10.4）。"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from aip.control_plane.skill import package as skill_pkg


def validate_skill_files(files: dict[str, bytes]) -> list[str]:
    """对已解压的 path→bytes 跑 skills-ref validate；返回人类可读问题列表（空=通过）。

    官方校验要求目录名与 frontmatter.name 一致，故写入临时目录 ``{name}/``。
    未安装 ``skills-ref`` 时返回明确错误（不得静默跳过）。
    """
    try:
        from skills_ref.validator import validate as skills_ref_validate
    except ImportError:
        return ["未安装 skills-ref（请执行 uv sync，或 uv sync --extra skills）"]

    if skill_pkg.SKILL_MD not in files:
        return ["缺少 SKILL.md，无法执行 skills-ref validate"]
    try:
        meta, _ = skill_pkg.parse_frontmatter(files[skill_pkg.SKILL_MD].decode("utf-8"))
    except Exception as e:  # noqa: BLE001 — 交给 G0 汇总
        return [f"skills-ref：无法解析 SKILL.md（{e}）"]
    name = (meta.get("name") or "").strip()
    if not name:
        return ["skills-ref：SKILL.md 缺少 name"]

    with tempfile.TemporaryDirectory(prefix="aip-skills-ref-") as tmp:
        root = Path(tmp) / name
        root.mkdir(parents=True)
        for path, data in files.items():
            dest = root / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        raw: Any = skills_ref_validate(root)
    # skills-ref 返回 list[str]；防御其它返回形
    if not raw:
        return []
    if isinstance(raw, list):
        return [f"skills-ref：{x}" for x in raw]
    return [f"skills-ref：{raw}"]
