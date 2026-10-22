"""Generate console/src/locales/km-KH.ts from en-US + km_overlays.json (H45)."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EN = ROOT / "console" / "src" / "locales" / "en-US.ts"
OVERLAYS = ROOT / "tools" / "km_overlays.json"
OUT = ROOT / "console" / "src" / "locales" / "km-KH.ts"


def main() -> None:
    km = json.loads(OVERLAYS.read_text(encoding="utf-8"))
    text = EN.read_text(encoding="utf-8")
    pairs = re.findall(r'"((?:\\.|[^"\\])*)"\s*:\s*"((?:\\.|[^"\\])*)"', text)
    if len(pairs) < 200:
        raise SystemExit(f"too few pairs: {len(pairs)}")
    lines = [
        "/** km-KH (H45): nav/login Khmer; other keys fall back to English until translated. */",
        "export const kmKH: Record<string, string> = {",
    ]
    overlays = 0
    for k_esc, v_esc in pairs:
        k = json.loads(f'"{k_esc}"')
        v = json.loads(f'"{v_esc}"')
        if k in km:
            overlays += 1
            out_v = km[k]
        else:
            out_v = v
        lines.append(f"  {json.dumps(k, ensure_ascii=False)}: {json.dumps(out_v, ensure_ascii=False)},")
    lines.append("};")
    lines.append("")
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {OUT} keys={len(pairs)} overlays={overlays}")


if __name__ == "__main__":
    main()
