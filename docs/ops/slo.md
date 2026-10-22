# 非功能指标校准（M4）

architecture.md 第 1 章的初始目标：平台自身附加延迟 P95 ≤ 100ms（不含下游）、可用性 ≥ 99.9%、审计零丢失、用户停用与授权撤销 30 秒内全局生效。本文件记录 M4 实测与据此调整的口径。

## 1. 测量方法

- 工具：`python tools/loadtest.py --requests N --concurrency C`。进程内用 uvicorn 起一个数据面进程，Mock Connector（下游耗时为 0），因此测得的端到端延迟就是平台自身附加延迟的上界：认证、Principal 解析、入参校验、PDP（真实 OPA）、幂等、限流熔断、Result Guard、两条审计记录的 fsync。
- 客户端与服务端在同一台机器，客户端占用部分 CPU，结果偏保守。
- 环境：Windows 11 开发机、Python 3.12、单个 uvicorn 进程；OPA 在 Docker Desktop 容器中，经端口转发访问（生产是同 Pod 的 localhost sidecar，延迟更低）。

## 2. 结果（2026-10-20）

| 场景 | 并发 | 请求数 | 错误 | 吞吐（req/s） | P50 | P95 | P99 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| REST /invoke | 1 | 300 | 0 | 103 | 9.5 ms | **12.8 ms** | — |
| MCP tools/call（2026-07-28） | 1 | 300 | 0 | 87 | 11.3 ms | **14.7 ms** | — |
| REST /invoke | 20 | 2000 | 0 | 199 | 95 ms | 132 ms | 180 ms |
| MCP tools/call | 20 | 2000 | 0 | 167 | 109 ms | 181 ms | 233 ms |

单请求附加延迟 P95 约 13 到 15ms，满足 ≤ 100ms。并发 20 时延迟升高来自单进程饱和排队（每个进程约 170 到 200 req/s），不是单次处理变慢。

## 3. 压测中发现并修复的问题

| 问题 | 影响 | 修复 |
| --- | --- | --- |
| 审计 spool 在事件循环线程同步 fsync，每次调用两条记录 | 并发时整个进程串行等待磁盘 | 组提交：记录在内存中分配 seq 与哈希，写盘与 fsync 在线程中进行，并发请求共享一次 fsync；每条记录返回前已落盘，零丢失不变（`test_group_commit_keeps_chain_intact_under_concurrency`） |
| 缺少 `sniffio` 时 httpcore 每次请求都重新尝试 import 并扫描磁盘 | 每次 OPA 调用额外数毫秒，profile 中占约 1/3 时间 | 显式依赖 `sniffio` |
| MCP tools/call 为判断可见性评估了全部工具 | 工具越多越慢 | 只评估被调用的那个工具 |

修复前后（并发 20，REST）：P95 543ms → 132ms，吞吐 55 → 199 req/s。

## 4. 生效时效

`tests/test_m4_e2e.py`：控制面停用员工后，数据面经长轮询（LISTEN / NOTIFY）拿到新快照并拒绝该员工的令牌，实测 **0.14 秒**（目标 ≤ 30 秒）。

## 5. 调整后的指标口径与容量建议

| 指标 | 口径 | 当前结论 |
| --- | --- | --- |
| 平台附加延迟 | 单个数据面进程负载不超过其容量 70% 时，P95 ≤ 100ms | 单请求 P95 ≈ 15ms；按容量规划保证该前提 |
| 单进程容量 | Mock 下游、开发机 | 约 170 到 200 req/s；生产 Linux + localhost OPA 预计更高，上线前在目标机型复测 |
| 生效时效 | 停用、撤销、吊销到全部数据面拒绝 | 秒级（0.14s 实测），目标 ≤ 30s |
| 审计 | 每条记录返回前 fsync | 满足；spool 80% 背压、满载拒绝 |

容量建议：每个数据面 Pod 运行 4 个 uvicorn worker（或等量副本），按峰值 QPS ÷（单进程容量 × 0.7）计算进程数，至少 3 个 Pod 分布在不同节点（`deploy/k8s/base/data-plane.yaml`）。

## 6. 尚未覆盖

- 真实下游（专线到分公司系统）的端到端延迟与熔断表现：在接入首个生产只读系统时测量。

## 7. PostgreSQL 切换与审计（M7k）

| 项 | 结论 | 证据 |
| --- | --- | --- |
| 切换窗口审计零丢失 | spool 续写 + ship 幂等恢复；链可 verify | `tests/test_m7_pg_failover.py`；[pg-failover.md](pg-failover.md) |
| 本地主从 | opt-in compose profile `pg-ha`；默认 `dev.py up` 仍为单节点 | `deploy/docker-compose.pg-ha.yml` |
| 生产自动切换 / PITR | 示例清单已落地；未在真实集群部署 | [pg-ha-prod.md](pg-ha-prod.md)、`deploy/k8s/cnpg/` |

## 8. 多进程线性扩展（M7l）

工具：`python tools/loadtest.py --workers 1,2,4 --concurrency 20 --requests 2000`（每档独立 spawn 进程 + 客户端轮询；Mock 下游）。  
口径：`scaling = rps_N / (rps_1 × N)`（1.0 = 理想线性）。

环境：Windows 开发机、Python 3.12、共享 Docker OPA（与 §2 相同局限）。

| workers | REST rps | REST P95 | MCP rps | MCP P95 | scaling REST | scaling MCP |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 294 | 87 ms | 176 | 192 ms | 1.00 | 1.00 |
| 2 | 500 | 57 ms | 383 | 83 ms | **0.85** | **1.09** |
| 4 | 525 | 53 ms | 427 | 64 ms | 0.45 | 0.61 |

结论：

- **2 进程**接近线性（REST scaling ≈ 0.85；MCP ≥ 1 因单进程时 MCP 更吃事件循环）。
- **4 进程**收益递减：客户端并发仍为 20，且 OPA / 本机 CPU 成为共享瓶颈；生产应按「每 Pod 多 worker + 多 Pod」扩展，并提高入口并发与 sidecar 本地 OPA。
- CI：`tests/test_m7_capacity.py` 轻量冒烟（200 请求，`rps(2) ≥ rps(1)×1.15`）。

## 9. 回归门槛（H40）

| 层级 | 命令 | 通过条件 |
| --- | --- | --- |
| CI 冒烟 | `pytest -q tests/test_m7_capacity.py` | REST：workers=2 的 rps ≥ workers=1 × 1.15，且 errors=0；MCP：单进程 100 请求 errors=0 |
| 手工短跑 | `python tools/dev.py loadtest --workers 1,2 --concurrency 20 --requests 400 --json-out .tmp/loadtest.json` | JSON 落盘；REST/MCP errors=0；2 进程 REST rps 明显高于 1 进程 |
| 全量校准 | `python tools/dev.py loadtest --workers 1,2,4 --concurrency 20 --requests 2000 --json-out .tmp/loadtest-full.json` | 更新本节上表；2 进程 REST scaling 宜 ≥ 0.7（开发机参考） |

更新结果表流程：跑全量校准 → 将 `.tmp/loadtest-full.json` 的 series/scaling 填入 §8 → 注明日期与机器。入口统一 `tools/dev.py loadtest`（透传参数给 `tools/loadtest.py`）。

## 10. 多副本真实压测结果表（H55）

本机多进程见 §8；**k8s 多副本**在 staging 填写下表（不阻塞 CI）。

### 参数模板

```bash
# 入口：tools/dev.py loadtest（透传 tools/loadtest.py）
# 对集群：把 BASE_URL 指到 Ingress / Service，关闭进程内起服（见 loadtest --help）
python tools/dev.py loadtest \
  --concurrency 40 --requests 2000 \
  --json-out .tmp/loadtest-k8s-4replicas.json
```

建议拓扑：数据面 Deployment `replicas: 4`，每 Pod 1–4 worker；OPA sidecar；控制面可单独扩。

### 结果表（staging 填写）

| 日期 | 集群 | 副本×worker | 并发 | 请求 | 错误 | REST rps | REST P95 | MCP rps | MCP P95 | 备注 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| _待填_ | | 4×? | 40 | 2000 | | | | | | |

验收参考：errors=0；REST P95（平台附加，下游 mock）在容量余量下尽量 ≤ 100ms；相对单副本吞吐有可见提升。
