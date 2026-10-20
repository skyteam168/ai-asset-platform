# ADR-0003 spike：自研 step journal 与 DBOS Transact 对比（2026-10-20）

依据 [architecture.md 8.5](../architecture.md) 与 P8：M5 开始前对比两种工作流引擎实现，由负责人决策。评判标准：崩溃恢复正确性、人工任务与定时器支持、可观测、团队可维护性。

## 做法

两个原型实现同一个 `WorkflowExecutor` 抽象（start / decide / status / steps），解释同一份 DSL 子集（tool、condition、human_task、wait、end，补偿声明），调用同一组假 Tool（每次调用记 attempt；写操作按幂等键去重记 effect）。Worker 是独立进程，用 TerminateProcess（等同 kill -9）或在 Tool 内部 `os._exit(137)` 模拟崩溃。代码在 `spikes/adr0003/`（独立虚拟环境，不进入项目依赖）。

| 原型 | 实现 | 代码量（不含空行注释） |
| --- | --- | --- |
| A 自研 step journal | `wf_run`（当前节点、上下文、租约、wake_at）+ `wf_step`（journal）+ `wf_task`；`FOR UPDATE SKIP LOCKED` 认领，租约过期任何 Worker 可接手；定时器靠 wake_at 扫描 | 187 行 |
| B DBOS Transact 3.2.0 | 解释器是一个 DBOS workflow；Tool 调用是 `DBOS.step`，human_task 是 `DBOS.recv(timeout)`，wait 是 `DBOS.sleep`；控制面用 `DBOSClient` 入队、发决策、查状态 | 87 行（另加依赖约 3.3 万行） |

DBOS 选 3.2.0 而不是最新的 3.3.0：后者发布不足两周。

## 结果

`spikes/adr0003/test_spike.py`，9 个场景 × 2 个引擎，18 个全部通过（场景按各自预期断言）：

| 场景 | 自研 journal | DBOS |
| --- | --- | --- |
| 分支流程（条件走不同路径） | 通过 | 通过 |
| 写操作已生效、步骤未记完成时进程被杀，同一 Worker 重启 | 完成；已完成步骤不重做，中断步骤用同一幂等键重做一次，副作用各一次；恢复 2.1s | 同左；恢复 2.3s |
| **Worker 崩溃后不再回来**（K8s 缩容、节点故障后 Pod 名变化） | 租约到期后另一 Worker 自动接手，2.2s 完成 | **无人接手**。开源版没有心跳，恢复按 executor_id 进行；死亡 executor 的判定属于商业版 Conductor。显式调用私有 API `DBOS._recover_pending_workflows(["w1"])` 后才完成 |
| Saga 失败逆序补偿 | revoke → release，正确 | 正确 |
| 人工任务通过、重复提交决策 | 通过；**发现**：Run 尚未建任务时提交的决策会被丢弃，已改为返回“未接受” | 通过；消息先于 recv 到达会被缓冲，无此问题 |
| 等待人工期间 Worker 被杀，超时跨崩溃生效 | 走升级分支，4.6s（超时 3s） | 走升级分支，6.5s |
| wait 3s 跨重启 | 3.25s 后继续 | 3.30s 后继续 |
| **发布新版本后在途 Run 继续** | 继续（DSL 与版本钉在 Run 上，引擎代码没有版本概念） | **不继续**。恢复只认同一 application_version；默认版本是源码哈希，任何发布都会改变。必须固定 application_version，不兼容变更由我们自己管理 |
| 吞吐（100 个 Run，各 2 个 Tool 步，2 个 Worker，本机） | 31.5 run/s | 16.2 run/s |
| 步骤可观测 | 自己的表：节点、状态、尝试次数、执行者 | 内置 `list_workflow_steps`；也记录 setEvent 等内部步骤，需要过滤 |

调试中出现过一次无法解释的现象：DBOS 的 3 秒 wait 跨重启后在约 2.5 秒到达下一节点，连续 5 次，之后 13 次全部正常。代码确认 DBOS 的 sleep 一直用 Worker 本机时钟（记录截止时间、恢复后比较），没有提前触发的逻辑；最可能是那段时间 Windows 时钟被校准。持久化定时器跨进程只能用墙上时钟，两个引擎都受时钟调整影响，这一点不构成差异。生产节点需要 NTP 并告警时钟跳变。

## 对比

| 标准 | 自研 journal | DBOS |
| --- | --- | --- |
| 崩溃恢复正确性 | 本次场景全部正确；正确性由我们负责，需要把这套故障测试作为 M5 的常驻回归 | 同一进程重启的恢复成熟；**多副本下死亡 Worker 的接管要自建**（判定死亡 executor + 调私有 API），或购买 Conductor |
| 人工任务与定时器 | 自己实现（wake_at 扫描、任务表、截止时间），已验证跨崩溃 | 内置 recv / sleep，消息缓冲更省心；升级、代理人、通知仍要我们做 |
| 可观测 | 表结构由我们定，可直接加 company_id 与 RLS，控制台和审计直接查 | 系统表在 dbos schema，无 RLS；对外展示要再映射 |
| 与现有技术栈的契合 | asyncpg、我们的 RLS 与审计、我们的租户字段 | psycopg 同步驱动 + 线程，另一套连接池；3.x 一个月内有破坏性 API 变化（例如 Queue 不能再直接构造） |
| 发布与版本 | Run 钉住 DSL 与 Release 快照，引擎升级不影响在途 Run | 必须固定 application_version；不兼容的解释器变更需要按版本保留旧 Worker |
| 尚需开发的量 | 循环与并行的步骤键、长步骤租约续期、取消、重试策略、清理归档；预计比 DBOS 方案多 1.5 到 2 周 | 死亡 executor 判定与接管、版本管理、状态映射与 RLS 展示；省下的主要是 journal 与定时器本身 |

## 建议

**选自研 step journal**，保留 `WorkflowExecutor` 抽象。理由：

1. 我们的 Workflow 是 DSL 解释执行，不是把业务写成 Python 函数。DBOS 最大的价值（让普通 Python 代码变成持久化工作流）用不上，实际只用到了步骤记录与定时器。
2. 多副本部署下 Worker 消失后的接管是硬需求，开源 DBOS 不提供；自建这部分并不比自建整个 journal 省事，还要依赖私有 API。
3. 版本模型冲突：我们要求“Run 启动时钉住 Release 快照、引擎升级不影响在途 Run”（8.5、7.4），DBOS 的应用版本恢复规则需要额外绕开。
4. Run 与步骤要带 company_id 进 RLS、进审计、在控制台展示；放在自己的表里最直接。
5. 依赖面：DBOS 约 3.3 万行代码、同步驱动和线程模型，3.x 仍在快速变化。

代价与对策：引擎正确性完全由我们负责。把 spike 的 9 个故障场景扩展为 M5 的常驻测试（加循环、并行、取消、长步骤续期、时钟跳变），任何引擎改动都要通过。

如果选 DBOS，M5 需要先做：固定 application_version 与版本退役流程；用 Worker 心跳表判定死亡 executor 并调用恢复（或采购 Conductor）；dbos schema 的访问隔离与状态映射。

## 复现

```bash
cd spikes/adr0003
uv venv --python 3.12 .venv
uv pip install --python .venv/Scripts/python.exe dbos==3.2.0 asyncpg==0.30.0 "sqlalchemy[asyncio]==2.0.43" pytest==8.3.3 pytest-asyncio==0.24.0
.venv/Scripts/python -m pytest -q -s test_spike.py   # 需要 dev PostgreSQL（55432）中的 aip_spike 库
```
