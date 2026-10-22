# Wave C 后做项说明（H56–H58）

与 [task.md](task.md) Wave C 对齐；本仓库交付骨架或文档，真环境另验。

## H56 SMTP / 飞书

| 项 | 说明 |
| --- | --- |
| SMTP | `AIP_SMTP_HOST` 等非空时用 `SmtpMailChannel`；否则 `LogMailChannel` |
| 飞书 | `AIP_NOTIFY_FEISHU_WEBHOOK_URL` → `FeishuChannel`（`msg_type=text`）；优先于通用 IM Webhook |
| 出站 | IM/飞书经 `libs/httpclient` |

## H57 Connector worker / RPA

见 [connector-worker.md](connector-worker.md)。

## H58 pgvector / 成本分摊 / 双机房

| 主题 | 本仓库 | 真环境 |
| --- | --- | --- |
| pgvector | 可选扩展说明见 [search-index.md](search-index.md)；默认栈不强制装 | staging 装扩展 + 向量索引 |
| 成本分摊 | 用量已有 `usage_counter`；分摊报表未做 | 按消费方/公司出账规则 |
| 双机房 | K8s 多副本 + CNPG；跨机房流量与数据驻留由集团策略 | 双活/灾备演练 |

## 生产清单

`prod-check` 增章节：SMTP / 飞书、Connector worker、pgvector / 双机房。
