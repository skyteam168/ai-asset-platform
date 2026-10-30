# 生产上线清单（H42）

本地自动化：`python tools/dev.py prod-check`（kustomize + 本章节标题存在性 + `Settings` 环境变量名对照）。  
**不**代替真实集群部署与演练。

执行顺序与任务拆分见企业级上线总计划：[2026-10-30-enterprise-go-live.md](../superpowers/plans/2026-10-30-enterprise-go-live.md)（Wave G0→G6）。本清单为 **G6 出口验收表**，须全部勾选方可宣布上线。

## Secrets

- [ ] 创建 K8s Secret `aip-secrets`（库连接、OpenBao token、服务令牌、签名/机密后端配置）；**勿**写入 Git。见 [k8s.md](k8s.md)。
- [ ] `AIP_ENV=prod`（或 staging）下无任何 `dev-only` 默认口令；`signing_backend` / `secret_backend` 按生产策略显式配置。

## 迁移 Job

- [ ] 先跑 Job `aip-migrate`（kustomize base），再扩控制面 / 数据面。见 [k8s.md](k8s.md)。
- [ ] 迁移账号为 owner；应用运行时为 `aip_app`（RLS）。

## kustomize prod

- [ ] `kubectl kustomize deploy/k8s/overlays/prod` 无错；镜像 tag 已改写。
- [ ] `AIP_RUNTIME_MODE=live`；数据面副本与 PDB 符合容量规划。见 [k8s.md](k8s.md)。

## CNPG

- [ ] 按需应用 `deploy/k8s/cnpg/` 示例；读写服务写入 `AIP_DATABASE_URL` / `AIP_APP_DATABASE_URL`。见 [pg-ha-prod.md](pg-ha-prod.md)。
- [ ] PITR / 备份对象存储已配置；恢复演练在 staging。

## OpenBao / Valkey / OPA / SeaweedFS

- [ ] OpenBao Transit（签名与机密）可达；token 经 Secret 注入。
- [ ] Valkey（`AIP_VALKEY_URL`）高可用或可接受故障窗。
- [ ] OPA sidecar / `AIP_OPA_URL` 与策略包版本锁定。
- [ ] SeaweedFS / S3（`AIP_S3_*`）用于 Skill / WORM 等对象；桶与凭据就绪。

## OTel

- [ ] `AIP_OTEL_EXPORTER=otlp` 与 Collector 端点；看板对照 [otel](../architecture.md) 与 H13 运维说明。
- [ ] 控制面 / 数据面 trace 无 PII / 明文凭据。

## 审计 WORM

- [ ] 需要时启用 `AIP_WORM_ENABLED` + `AIP_WORM_BUCKET`；对象锁策略符合合规。见 [audit-worm.md](audit-worm.md)。
- [ ] 审计库与 spool 容量；故障窗行为见 [fault-drills.md](fault-drills.md)。

## Notifier 环境变量

- [ ] 邮件：未配 SMTP 时 `LogMailChannel`；生产设 `AIP_SMTP_HOST` / `AIP_SMTP_PORT` / `AIP_SMTP_USER` / `AIP_SMTP_PASSWORD` / `AIP_SMTP_FROM` / `AIP_SMTP_STARTTLS`。
- [ ] IM：`AIP_NOTIFY_IM_WEBHOOK_URL`（通用 Webhook JSON）；或飞书 `AIP_NOTIFY_FEISHU_WEBHOOK_URL`（优先）。
- [ ] `AIP_NOTIFY_FLUSH_INTERVAL_S`（默认 2）；本地可用 `POST /api/v1/notify/flush`（platform_admin）。

## SLO / 故障演练

- [ ] 对照 [slo.md](slo.md) 回归门槛与容量表。
- [ ] 按 [fault-drills.md](fault-drills.md) 完成停控制面 / Valkey / OpenBao / 审计库演练记录。

## 服务间认证

- [ ] 生产 `AIP_SERVICE_AUTH_MODE=jwt`（或 `both` 过渡）；`AIP_SERVICE_TOKEN_TTL_S` 合理。
- [ ] 入口/sidecar 终止 mTLS 时配置 `AIP_SERVICE_MTLS_CA_PEM`（或等价信任包）。见 [task.md](task.md) H44。

## GenAI / Langfuse

- [ ] LLM 调用使用 OTel GenAI 语义（见 [observability.md](observability.md)）；可选自托管 Langfuse。
- [ ] 登记 `AIP_LANGFUSE_HOST` / `AIP_LANGFUSE_PUBLIC_KEY` / `AIP_LANGFUSE_SECRET_KEY`（有则接线，无则跳过）。

## Object Lock / WORM 全量

- [ ] 桶开启 Object Lock（合规模式）与保留期；对照 [audit-worm.md](audit-worm.md)。
- [ ] 定期校验锚点与对象一致性；故障演练含审计库不可用窗。

## CNPG / Patroni / PITR 实装

- [ ] staging 按 [pg-ha-prod.md](pg-ha-prod.md) 完成 CNPG（或 Patroni）安装与 PITR 恢复演练。
- [ ] 真集群多副本压测结果写入 [slo.md](slo.md)（Wave B / H55）。

## SMTP / 飞书

- [ ] 生产 SMTP 连通性与发件人域名；密钥进 OpenBao / Secret，不进镜像。
- [ ] 飞书机器人 Webhook 仅内网或签名校验策略；对照 [wave-c-backlog.md](wave-c-backlog.md) H56。

## Connector worker

- [ ] 默认 `AIP_CONNECTOR_WORKER_MODE=inline`；拆池时用 `rpc` + `AIP_CONNECTOR_WORKER_URL`（见 [connector-worker.md](connector-worker.md)）。
- [ ] RPA 类连接器首期不做；若启用须独立池且沙箱拒 live 写。

## pgvector / 双机房

- [ ] 需要语义检索时再装 pgvector（见 [search-index.md](search-index.md)）；默认不强制。
- [ ] 双机房 / 跨机架副本与数据合规由集团策略；平台侧多副本见 K8s base。
