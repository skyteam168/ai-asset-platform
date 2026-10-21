"""H33：corp-skill --target 适配表。"""

from __future__ import annotations

from pathlib import Path

from aip.cli.corp_skill import DEFAULT_TARGET, TARGET_PRESETS, resolve_target


def test_resolve_target_presets() -> None:
    assert resolve_target("generic") == DEFAULT_TARGET
    assert resolve_target("CURSOR") == TARGET_PRESETS["cursor"]
    assert resolve_target("claude") == TARGET_PRESETS["claude"]
    assert "skills" in str(resolve_target("claude-desktop"))


def test_resolve_target_path() -> None:
    p = resolve_target("~/my-skills-root")
    assert p == Path("~/my-skills-root").expanduser()
