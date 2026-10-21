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

## 不在本片

- Loki 日志流水线、Langfuse / GenAI 语义约定
- 生产级 Tempo 多租户与长期存储
- 应用进程内 Prometheus `/metrics` 直出（指标经 OTLP → Collector）
