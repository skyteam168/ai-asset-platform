"""G6.1 本地 PG 备份/恢复演练（无 kube 可跑；非 CNPG WAL PITR）。

对 compose 中的 aip 库：造数 → pg_dump → 再写干扰行 → 恢复到独立库 → 校验造数在、干扰不在。
真 CNPG PITR 仍见 docs/ops/evidence/g6-cnpg-pitr.md（需 staging 集群）。

用法：
  .venv/Scripts/python tools/pg_backup_restore_drill.py
环境：
  AIP_PG_DRILL=1  失败时 exit 2（默认无 docker 则 skip exit 0）
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ["docker", "compose", "-f", str(ROOT / "deploy" / "docker-compose.yml")]
MARKER = f"g61_drill_{uuid.uuid4().hex[:12]}"
NOISE = f"noise_{uuid.uuid4().hex[:8]}"
DUMP = ROOT / ".tmp" / "g6-pg-drill" / "backup.dump"


def _run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    print("$", " ".join(cmd), flush=True)
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    if check and proc.returncode != 0:
        sys.stderr.write(proc.stderr or proc.stdout or "failed\n")
        raise SystemExit(proc.returncode)
    return proc


def _psql(sql: str, *, db: str = "aip") -> str:
    proc = _run(
        [
            *COMPOSE,
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "aip",
            "-d",
            db,
            "-v",
            "ON_ERROR_STOP=1",
            "-tAc",
            sql,
        ]
    )
    return (proc.stdout or "").strip()


def main() -> int:
    force = os.environ.get("AIP_PG_DRILL") == "1"
    up = _run([*COMPOSE, "ps", "-q", "postgres"], check=False)
    if up.returncode != 0 or not (up.stdout or "").strip():
        print("skip: aip-dev postgres not running (tools/dev.py up)")
        return 2 if force else 0

    DUMP.parent.mkdir(parents=True, exist_ok=True)
    _psql(
        "CREATE TABLE IF NOT EXISTS g61_drill_marker("
        "id text PRIMARY KEY, note text NOT NULL, created_at timestamptz DEFAULT now());"
    )
    _psql(f"INSERT INTO g61_drill_marker(id, note) VALUES ('{MARKER}', 'before-dump') "
          f"ON CONFLICT (id) DO UPDATE SET note = EXCLUDED.note;")

    # dump custom format into container then docker cp
    dump_in = f"/tmp/{DUMP.name}"
    _run(
        [
            *COMPOSE,
            "exec",
            "-T",
            "postgres",
            "pg_dump",
            "-U",
            "aip",
            "-d",
            "aip",
            "-Fc",
            "-f",
            dump_in,
            "-t",
            "g61_drill_marker",
        ]
    )
    _run(["docker", "compose", "-f", str(ROOT / "deploy" / "docker-compose.yml"), "cp", f"postgres:{dump_in}", str(DUMP)])

    _psql(f"INSERT INTO g61_drill_marker(id, note) VALUES ('{NOISE}', 'after-dump');")
    assert _psql(f"SELECT note FROM g61_drill_marker WHERE id='{NOISE}'") == "after-dump"

    restore_db = "aip_g61_restore"
    _psql(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='{restore_db}' AND pid <> pg_backend_pid();", db="postgres")
    _psql(f"DROP DATABASE IF EXISTS {restore_db};", db="postgres")
    _psql(f"CREATE DATABASE {restore_db} OWNER aip;", db="postgres")

    dump_restore = f"/tmp/restore_{DUMP.name}"
    _run([*COMPOSE, "cp", str(DUMP), f"postgres:{dump_restore}"])
    _run(
        [
            *COMPOSE,
            "exec",
            "-T",
            "postgres",
            "pg_restore",
            "-U",
            "aip",
            "-d",
            restore_db,
            "--clean",
            "--if-exists",
            dump_restore,
        ]
    )

    before = _psql(f"SELECT note FROM g61_drill_marker WHERE id='{MARKER}'", db=restore_db)
    noise = _psql(f"SELECT count(*)::text FROM g61_drill_marker WHERE id='{NOISE}'", db=restore_db)
    if before != "before-dump" or noise != "0":
        print(f"FAIL: marker={before!r} noise_count={noise!r}")
        return 1

    evidence = ROOT / "docs" / "ops" / "evidence" / "g6-pg-local-restore.md"
    evidence.write_text(
        "\n".join(
            [
                "# G6.1 本地 PG dump/restore 演练记录",
                "",
                "> 本机 compose 演练，**不是** CNPG WAL PITR。staging CNPG 真恢复仍填 [g6-cnpg-pitr.md](g6-cnpg-pitr.md)。",
                "",
                f"- 日期：自动生成于 `tools/pg_backup_restore_drill.py`",
                f"- marker：`{MARKER}` 恢复后 note=`before-dump`",
                f"- 干扰行：`{NOISE}` 恢复库中 count=0",
                f"- dump：`{DUMP.as_posix()}`",
                "",
                "## 命令",
                "",
                "```bash",
                ".venv/Scripts/python tools/pg_backup_restore_drill.py",
                "```",
                "",
            ]
        ),
        encoding="utf-8",
    )
    print(f"ok: restored marker present, noise absent; wrote {evidence}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
