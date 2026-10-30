# Connector Worker 与 RPA（H57 / G5.1）

对照 [architecture.md](../architecture.md) §9.5、部署隔离说明。

## 模式

| `AIP_CONNECTOR_WORKER_MODE` | 行为 |
| --- | --- |
| `inline`（默认） | Connector 与 Runtime 同进程；asyncio + 每实例信号量 |
| `rpc` | 独立 worker 池；须配 `AIP_CONNECTOR_WORKER_URL`（如 `http://connector-worker:18090`） |

代码：

- 客户端：`aip.libs.connector_worker`（`InlineConnectorWorker` / `RpcConnectorWorker`，出站经 `build_client`）
- 服务端：`python -m aip.workers.connector_main`（`POST /v1/connector-worker/execute`，`GET /healthz`）
- K8s 草稿：`deploy/k8s/base/connector-worker.yaml`（默认未列入 `kustomization.yaml`）

## RPC 协议（内部）

请求 `POST /v1/connector-worker/execute`：

```json
{
  "connector_kind": "rest",
  "op": "ping",
  "params": {},
  "connection_id": "c1",
  "idempotency_key": "k1"
}
```

响应：

```json
{ "ok": true, "data": { "...": "..." }, "error": null }
```

worker 不可达 / 5xx → 平台错误码 `DOWNSTREAM_UNAVAILABLE` 或 `DOWNSTREAM_TIMEOUT`（不悬挂调用方）。

本地冒烟：

```bash
# 终端 A
AIP_CONNECTOR_WORKER_PORT=18090 .venv/Scripts/python -m aip.workers.connector_main
# 终端 B：pytest 或把 AIP_CONNECTOR_WORKER_MODE=rpc URL 指到 :18090
.venv/Scripts/python -m pytest tests/test_h57_connector_worker.py -q
```

## 何时拆池

- 阻塞第三方 SDK、长连接、CPU 密集解析
- **RPA / 浏览器自动化**：架构明确首期不做；若启用，必须进独立池且沙箱硬拒 live 写

## 生产接线

1. 应用 `connector-worker.yaml`（或等价 Deployment），Service 暴露 18090。
2. 数据面：`AIP_CONNECTOR_WORKER_MODE=rpc`、`AIP_CONNECTOR_WORKER_URL=http://connector-worker.<ns>.svc:18090`。
3. 生产 handler 可替换默认 echo，在 worker 进程加载 Connector 快照后执行真实 `op`（本片默认 echo 证明 RPC 通路）。

## 不在本片

- gRPC / 消息队列
- RPA 连接器实现
- 将草稿 Deployment 强制并入 prod overlay
