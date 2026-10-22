"""Generate docs/adr/*.md from the ADR table in docs/architecture.md (16.1).

Idempotent: existing ADR files are not overwritten.
"""

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
arch = (ROOT / "docs/architecture.md").read_text(encoding="utf-8")
section = arch.split("### 16.1 初始 ADR", 1)[1].split("### 16.2", 1)[0]
rows = re.findall(r"^\| (\d{4}) \| (.+?) \| (Accepted|Proposed|Open|Superseded) \|$", section, re.M)
adr_dir = ROOT / "docs/adr"
adr_dir.mkdir(parents=True, exist_ok=True)

TEMPLATE = """# ADR-{num}：{title}

- 状态：{status}
- 日期：2026-10-19
- 关联：docs/architecture.md 第 16.1 节{extra}

## 背景

{context}

## 决策

{title}。详细设计以 docs/architecture.md 为准，本 ADR 只记录决策与取舍。

## 影响

- 修改本决策必须新建 ADR 并将本 ADR 标记为 Superseded。
- 实现与本决策冲突时，先提问，不自行改设计。
"""
CONTEXT = {
    "0003": "自研 step journal 与 DBOS Transact 在 M5 前做 2 周 spike 后二选一（8.5）。",
    "0010": "旧系统默认不改造；个别写接口缺少幂等键时，需与系统方逐个评审。",
    "0017": "旧系统存在 HTTP、内部 CA 与自签证书；见 9.6 与附录 B。",
    "0025": "集团已另行解决跨境数据合规，平台不做数据驻留标注与跨境报表。",
}
(adr_dir / "0000-template.md").write_text(
    TEMPLATE.format(
        num="NNNN",
        title="标题",
        status="Proposed | Accepted | Open | Superseded",
        extra="",
        context="为什么需要做这个决定。",
    ),
    encoding="utf-8",
)
index = ["# ADR 索引", "", "| 编号 | 主题 | 状态 |", "| --- | --- | --- |"]
for num, title, status in rows:
    slug = f"{num}.md"
    path = adr_dir / slug
    if not path.exists():
        path.write_text(
            TEMPLATE.format(
                num=num,
                title=title,
                status=status,
                extra="",
                context=CONTEXT.get(num, "见 docs/architecture.md 对应章节。"),
            ),
            encoding="utf-8",
        )
    index.append(f"| [{num}]({slug}) | {title} | {status} |")
(adr_dir / "README.md").write_text("\n".join(index) + "\n", encoding="utf-8")
print(f"{len(rows)} ADRs")
if len(rows) != 26:
    sys.exit("expected 26 ADRs")
