# G6.1 CNPG + PITR 演练记录（模板）

> 在有 staging / kind 集群时按步骤填写；无 kube context 时本文件保持「待填」，冒烟见 `tools/cnpg_kind_smoke.py`。

## 环境

| 项 | 值 |
| --- | --- |
| 日期 | |
| 集群 context | |
| CNPG operator 版本 | |
| 命名空间 | |
| 对象存储 endpoint / bucket | |

## 步骤勾选

1. [ ] 安装 CNPG operator；`kubectl get crd clusters.postgresql.cnpg.io` 成功
2. [ ] 创建 Secret `aip-pg-owner` / `aip-pg-backup`；改写 `deploy/k8s/cnpg/cluster.yaml` 后 apply
3. [ ] 应用连上 `*-rw`：迁移 Job 成功；控制面/数据面 readyz
4. [ ] 造数：插入可识别行（记录 PK / 业务键）
5. [ ] 确认 ScheduledBackup / WAL 归档成功至少一次
6. [ ] 记录恢复点时间 `t0`；继续写若干行（恢复后应不存在）
7. [ ] 在空命名空间 Bootstrap from backup / PITR 到 `t0`
8. [ ] 校验：造数行在；`t0` 后写入不在；哈希链 / 关键点可选抽查

## 结果

| 项 | 结果 |
| --- | --- |
| RPO 观察 | |
| 恢复耗时 | |
| 数据校验 | |
| 证据附件路径 | `.tmp/g6-cnpg/`（日志、截图、kubectl describe） |

## 冒烟（本机）

```text
# 2026-10-30：kubectl 有客户端但无 current-context → cnpg_kind_smoke skip
.venv/Scripts/python tools/cnpg_kind_smoke.py
```
