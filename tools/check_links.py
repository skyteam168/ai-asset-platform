"""检查 docs/ 与 README 中的相对链接都指向存在的文件。"""

from __future__ import annotations

import pathlib
import re
import sys
import urllib.parse

ROOT = pathlib.Path(__file__).resolve().parents[1]
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")


def main() -> int:
    files = [*sorted((ROOT / "docs").rglob("*.md")), ROOT / "README.md"]
    broken: list[str] = []
    for f in files:
        if not f.exists() or "archive" in f.parts:
            continue
        for m in LINK.finditer(f.read_text(encoding="utf-8")):
            target = m.group(1)
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            path = urllib.parse.unquote(target.split("#", 1)[0])
            if path and not (f.parent / path).exists():
                broken.append(f"{f.relative_to(ROOT)} -> {target}")
    for b in broken:
        print("broken link:", b)
    print("links ok" if not broken else f"{len(broken)} broken links")
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
