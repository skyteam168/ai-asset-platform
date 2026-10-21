# 生产 PostgreSQL 高可用与 PITR（H17）

对照 [architecture.md](../architecture.md) §12.1。本地主从演练见 [pg-failover.md](pg-failover.md)；本文件描述**生产路径**清单与仓库内示例。

## 选型

| 方案 | 说明 |
| --- | --- |
| **CloudNativePG（推荐）** | 示例清单 `deploy/k8s/cnpg/cluster.yaml`（Cluster ×3 + ScheduledBackup → S3/SeaweedFS） |
| Patroni | 自建 StatefulSet + etcd/Consul；本仓库不附完整 chart |
| 审计 | 独立实例或独立 schema/角色；RPO=0 依赖 spool + 同步复制（见故障演练） |

## CloudNativePG 启用步骤（摘要）

1. 安装 CNPG operator（集群级，一次）。
2. 创建 Secret：`aip-pg-owner`（库主账号）、`aip-pg-backup`（对象存储密钥）。
3. 按环境改写 `destinationPath` / `endpointURL` / `storage.size`。
4. `kubectl apply -f deploy/k8s/cnpg/cluster.yaml`（**不要**并入 `deploy/k8s/base`）。
5. 应用侧 `AIP_DATABASE_URL` / `AIP_APP_DATABASE_URL` 指向 CNPG 读写服务（通常 `*-rw`）。

校验示例 YAML 可解析：`tests/test_h17_pg_fts_ha.py`。

## PITR

CNPG + barmanObjectStore 提供 WAL 归档与定时全备；恢复演练在 staging 用 `Bootstrap from backup`，勿在生产直接试。

## 与本地 pg-ha 的关系

| 环境 | 拓扑 |
| --- | --- |
| 开发默认 | 单节点 `postgres:16-alpine`（`dev.py up`） |
| 本地演练 | compose profile `pg-ha`（[pg-failover.md](pg-failover.md)） |
| 生产 | CNPG / Patroni + 对象存储 PITR（本文件） |

## 不在本片

- 真实集群安装 operator 与恢复演练
- Patroni 完整 Helm
- 把 CNPG 并进默认 kustomize base
