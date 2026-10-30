# 可观测：OTel Collector 与看板（H13）

依据 [architecture.md](../architecture.md) §12.4。应用侧 trace 贯通见 H5（`libs/otel`）；本片提供本地汇聚与查看。

## 组件与端口

| 服务 | 地址 | 说明 |
| --- | --- | --- |
| OTel Collector OTLP HTTP | `http://127.0.0.1:14318` | 应用导出目标（`/v1/traces`） |
| OTel Collector OTLP gRPC | `127.0.0.1:14317` | 可选 |
| Tempo | `http://127.0.0.1:13200` | 追踪存储查询 API |
| Prometheus | `http://127.0.0.1:19090` | 抓取 Collector 指标 |
| Grafana | `http://127.0.0.1:13000` | 看板（匿名 Viewer；管理员 `admin` / `aip-dev-only`） |

不随 `dev.py up` 启动，避免拖慢日常开发镜像拉取。

## 启动

```bash
python tools/dev.py otel-up          # Collector + Tempo + Prometheus + Grafana
# 另开终端，启用 OTLP 导出后 serve：
set AIP_OTEL_EXPORTER=otlp
set AIP_OTEL_ENDPOINT=http://127.0.0.1:14318/v1/traces
python tools/dev.py serve
# 或一行：
python tools/dev.py serve-otel
```

停止：`python tools/dev.py otel-down`。

## 查看追踪

1. 打开 Grafana → Explore → Tempo。
2. Search：`{resource.service.name="aip-control-plane"}` 或 `aip-data-plane`。
3. 或用响应头 / 日志中的 `X-Trace-Id` / `trace_id` 精确查询。
4. 预置看板：**AIP → AIP OTel Overview**（Collector 接收/导出速率）。

## 配置文件

| 路径 | 用途 |
| --- | --- |
| `deploy/docker-compose.otel.yml` | Compose 编排 |
| `deploy/otel/otel-collector-config.yaml` | 接收 OTLP → Tempo；Prometheus exporter |
| `deploy/otel/tempo.yaml` | Tempo 单节点 |
| `deploy/otel/prometheus.yml` | 抓取 Collector |
| `deploy/otel/grafana/` | 数据源与看板自动供给 |

## GenAI / Langfuse（H50 / G4.3）

| 项 | 说明 |
| --- | --- |
| OTel GenAI | `LlmGateway.complete` 产出 span `gen_ai.chat`，属性含 `gen_ai.operation.name`、`gen_ai.request.model`、`gen_ai.usage.total_tokens`、`gen_ai.system`、`aip.model_profile`；**不含** prompt / 密钥 / 未脱敏业务字段 |
| Langfuse | 可选自托管；`AIP_LANGFUSE_HOST` / `AIP_LANGFUSE_PUBLIC_KEY` / `AIP_LANGFUSE_SECRET_KEY` 登记于 `Settings`；首期不强制进程内 SDK |
| 真调用夹具 | `pytest -m llm`：`AIP_TEST_LLM_BASE_URL` + `AIP_TEST_LLM_API_KEY` + `AIP_TEST_LLM_MODEL`（可选 `AIP_TEST_LLM_API_PATH`）；未设则 skip |
| 生产勾选 | 见 [production-checklist.md](production-checklist.md)「GenAI / Langfuse」 |

### Langfuse 接线步骤（可选）

1. 自托管或 SaaS 创建项目，记下 Host / Public Key / Secret Key。
2. 写入部署 Secret（勿进 Git）：`AIP_LANGFUSE_HOST`、`AIP_LANGFUSE_PUBLIC_KEY`、`AIP_LANGFUSE_SECRET_KEY`。
3. 推荐经 Collector 转发（与 H13 同路），避免应用直连第三方：
   - 应用：`AIP_OTEL_EXPORTER=otlp`、`AIP_OTEL_ENDPOINT` 指向本集群 Collector。
   - Collector exporter：OTLP → Langfuse OTLP/HTTP（或官方文档中的 ingestion URL）；鉴权用上述 Public/Secret。
4. 跑一次经 Gateway 的 chat（控制台选择评测 / Workflow LLM 节点 / `pytest -m llm`），在 Langfuse 中按 `gen_ai.request.model` 或 `aip.model_profile` 过滤应可见 span。
5. 若无 Langfuse：仅保留 Tempo/Grafana 查看 `gen_ai.*` 即可；清单「有则接线，无则跳过」。

控制台 LLM 配置：平台级提供方（`/api/v1/llm/provider`）写入 OpenAI 兼容 `base_url`，API Key 进 OpenBao `kv/data/aip/llm/default`；公司白名单与日 token 预算仍走 `/api/v1/companies/{id}/llm-*`。

## 不在本片

- Loki 日志流水线全量
- 生产级 Tempo 多租户与长期存储
- 应用进程内 Prometheus `/metrics` 直出（指标经 OTLP → Collector）
- 强制 Langfuse Python SDK 进程内接线（OTel 转发即可）
