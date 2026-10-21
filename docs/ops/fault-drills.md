# 故障演练矩阵（architecture.md 12.2）

本表对照 M7 验收「分别停掉控制面、Valkey、OpenBao、审计库，行为符合 12.2」。自动化优先用进程内夹具模拟故障，避免 CI 真停容器；人工联调步骤见第 2 节。

## 1. 自动化对照

| 故障 | 期望行为 | 自动化证据 |
| --- | --- | --- |
| 控制面宕机 | 数据面按本地快照服务；readyz 可用 | `test_m2_registry.py::test_data_plane_resolves_tools_after_control_plane_goes_down`；`test_m7_fault_drills.py::test_invoke_without_control_plane` |
| OPA / 快照缺失 | 默认拒绝（fail closed） | `test_m1_oauth.py`（POLICY_UNAVAILABLE）；篡改快照 `test_tampered_local_snapshot_is_not_restored` |
| Valkey 不可用 | jti 退化为进程内 + degraded；长 TTL 断言拒绝 | `test_m1_tokens.py::test_degraded_replay_store_only_accepts_short_lived`；`test_m7_fault_drills.py::test_valkey_down_sets_degraded` |
| OpenBao 不可用 | 已缓存下游会话到期前可继续；新取凭据失败 | `test_m7_fault_drills.py::test_openbao_down_uses_cached_credential` |
| 审计库不可用 | 继续写 spool；恢复后 ship 零丢失 | `test_m2_audit.py`（audit DB 不可用期间 spool + ship） |
| spool ≥80% / 满 | 拒绝写 / 全部拒绝 | `test_m2_audit.py::test_spool_backpressure` |
| 下游不可用 | Connector 熔断 / 明确错误码 | `test_m3_runtime.py`（超时、熔断） |
| PostgreSQL 主库切换 | 切换窗口 spool 续写；恢复后 ship 零丢失、链完整 | `test_m7_pg_failover.py`；人工主从见 [pg-failover.md](pg-failover.md) |

未纳入本矩阵：可选 zhparser；生产 Patroni/CloudNativePG；k8s 真实多副本压测（多进程扩展见 [slo.md](slo.md) §8 / `test_m7_capacity.py`）。

## 2. 本地人工演练（可选）

在 `dev.py up && serve` 正常后：

1. **停控制面**：结束控制面 uvicorn 进程，只留数据面 → `GET :18082/readyz` 仍 ok（快照已加载）；控制台登录失败属预期。
2. **停 Valkey**：`docker stop` compose 中 valkey → 新签用户断言若剩余有效期 >60s 应被拒；短 TTL 可过。
3. **停 OpenBao**：停 openbao → 已缓存的下游 login token 未到期前读类调用仍可；强制刷新/新凭据失败。
4. **停审计库**：仅断审计库连接（应用库保留）→ 调用仍成功；恢复后 `ship` 清空 spool。
5. **PostgreSQL 主从切换**：见 [pg-failover.md](pg-failover.md)（opt-in compose profile `pg-ha` + 促升从库）。

演练后 `dev.py up` 恢复依赖。
