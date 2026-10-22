# Connector Worker 与 RPA（H57）

对照 [architecture.md](../architecture.md) §9.5、§后做。

## 模式

| `AIP_CONNECTOR_WORKER_MODE` | 行为 |
| --- | --- |
| `inline`（默认） | Connector 与 Runtime 同进程；asyncio + 每实例信号量 |
| `rpc` | 独立 worker 池骨架；须配 `AIP_CONNECTOR_WORKER_URL`；真 RPC 未接线 |

代码：`aip.libs.connector_worker`（`InlineConnectorWorker` / `RpcConnectorWorker`）。

## 何时拆池

- 阻塞第三方 SDK、长连接、CPU 密集解析
- **RPA / 浏览器自动化**：架构明确首期不做；若启用，必须进独立池且沙箱硬拒 live 写

## 不在本片

- 真实内部 RPC / gRPC 协议
- RPA 连接器实现
- K8s Deployment 多池清单
