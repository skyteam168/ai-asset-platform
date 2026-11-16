# G6.1 本地 PG dump/restore 演练记录

> 本机 compose 演练，**不是** CNPG WAL PITR。staging CNPG 真恢复仍填 [g6-cnpg-pitr.md](g6-cnpg-pitr.md)。

- 日期：自动生成于 `tools/pg_backup_restore_drill.py`
- marker：`g61_drill_6d9bfe1559ce` 恢复后 note=`before-dump`
- 干扰行：`noise_99cbf248` 恢复库中 count=0
- dump：`D:/code/ai-asset-platform/.tmp/g6-pg-drill/backup.dump`

## 命令

```bash
.venv/Scripts/python tools/pg_backup_restore_drill.py
```
