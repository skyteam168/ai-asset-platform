# PostgreSQL 主从切换演练（architecture.md 12.2 / M7）

对照 M7 验收「**PostgreSQL 切换后审计零丢失**」。生产拓扑用 Patroni / CloudNativePG + PITR（见 architecture 12.1）；本文件只覆盖**本地 opt-in 主从**与应用侧 spool 行为。

默认 `tools/dev.py up` **不**启用从库，仍为单节点 `postgres:16-alpine`。

## 1. 期望行为（12.2）

| 阶段 | 期望 |
| --- | --- |
| 主库可写 | 调用先 `AuditSpool.append`（fsync），后台 `ship` 入库 |
| 主库不可用 / 切换窗口 | 调用继续写本地 spool；`ship` 失败只告警；spool ≥80% 拒写（`POLICY_UNAVAILABLE`） |
| 促升从库并改连 | `ship` 幂等续传；哈希链可 `verify`；审计 RPO=0（相对 spool） |
| 业务写请求 | 进行中失败靠幂等重试；切换期写操作可返回平台不可用类错误（下游/库） |

自动化（CI，不启真实主从）：[`tests/test_m7_pg_failover.py`](../../tests/test_m7_pg_failover.py)。  
人工编排脚本：[`tools/pg_failover_drill.py`](../../tools/pg_failover_drill.py)。

## 2. 启动本地主从（可选）

在仓库根目录，**建议使用空数据卷**（避免与日常单节点 pgdata 混用）：

```bash
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.pg-ha.yml --profile pg-ha down -v
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.pg-ha.yml --profile pg-ha up -d
# 主库 127.0.0.1:55432 ；从库 127.0.0.1:55433（只读）
```

初始化仅在**首次**建卷时执行：创建 `replicator` 复制角色，并放宽 `pg_hba` 复制行（见 `deploy/pg-ha/primary-init/`）。

迁移与 bootstrap 仍对**主库**执行：

```bash
.venv/Scripts/python tools/dev.py migrate   # Windows；Linux 用 .venv/bin/python
```

确认复制（主库）：

```bash
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.pg-ha.yml exec postgres \
  psql -U aip -d aip -c "SELECT client_addr, state FROM pg_stat_replication;"
```

## 3. 切换步骤（人工）

1. 基线：对主库产生审计（跑业务或 `python tools/pg_failover_drill.py --phase baseline`）。
2. 模拟主挂：`docker stop` 主库容器（compose 服务名 `postgres`）。
3. 促升从库：
   ```bash
   docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.pg-ha.yml exec postgres-replica \
     pg_ctl promote -D /var/lib/postgresql/data
   ```
   （若镜像无 `pg_ctl` 在 PATH：`su-exec postgres pg_ctl promote -D "$PGDATA"`。）
4. 应用改连到新主（端口 `55433`，或把 compose 端口映射切回 `55432`）。数据面 `audit_database_url` / 业务库 URL 一并更新后重启进程。
5. 恢复后：`ship` 清空 spool；`verify` 无缺口（脚本 `--phase verify`）。

演练结束恢复日常栈：

```bash
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.pg-ha.yml --profile pg-ha down -v
.venv/Scripts/python tools/dev.py up
```

## 4. 与故障演练矩阵的关系

见 [fault-drills.md](fault-drills.md)。审计库不可用期间的 spool 行为已由 M2 覆盖；本片补上「切换窗口续写 + 恢复入库」与本地主从操作手册。

## 5. 生产路径

见 [pg-ha-prod.md](pg-ha-prod.md)（CloudNativePG 示例 + PITR）。本地 pg-ha 不替代生产自动切换。

## 6. 不在本片

- 真实集群安装 CNPG operator / 恢复演练
- 多副本 uvicorn 容量压测
- zhparser 镜像（见 [search-index.md](search-index.md)）
