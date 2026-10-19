# 里程碑验收记录

每个里程碑结束时对照 architecture.md 第 14 章逐条记录。命令均在仓库根目录执行。

## M0 基础与文档（P0a、P0）— 2026-10-19 通过

环境：Windows 11、Python 3.12.0、Docker 29.8.2、uv 0.11.14、Node 24。

| 验收项 | 结果 | 证据 |
| --- | --- | --- |
| docs 体系：architecture.md、ADR 0001 至 0025 与模板、conventions、error-codes、5 份 JSON Schema、2 份 OpenAPI | 通过 | `tests/test_m0_docs.py` 15 项通过 |
| JSON Schema 元校验与正反例（只读 Tool 通过；write 缺对账、danger 自动确认、未知字段被拒；http / pinned / insecure 连接配置正反例） | 通过 | 同上 |
| OpenAPI 3.1 校验、operationId 唯一、无 YAML 别名 | 通过 | openapi-spec-validator；spectral 0 error |
| openapi-generator 生成 Python 与 TypeScript client | 通过 | v7.10.0，两份契约 × 两种语言 4/4 成功 |
| 链接检查 | 通过 | `tools/check_links.py` |
| `python tools/dev.py up`：5 个基础设施容器启动，baseline 迁移到 head | 通过 | `alembic current` = 0001 (head)；`tests/test_m0_infra.py` 5 项通过 |
| 两个服务 /healthz 正常 | 通过 | uvicorn 实跑：控制面、数据面均 200 |
| 数据面无快照与策略时 /readyz 返回 503（fail closed） | 通过 | `tests/test_m0_apps.py` |
| 实现的路由都在 OpenAPI 契约中 | 通过 | `test_implemented_routes_are_in_contract` |
| 业务代码出现 verify=False / CERT_NONE 时 CI 失败 | 通过 | `tests/test_m0_tls_lint.py` 4 项通过（AST 检查 + ruff banned-api，httpclient 包豁免） |
| ruff、mypy --strict | 通过 | ruff 全部通过；mypy 15 个文件无问题 |
| 测试总数 | 34 通过，0 失败，0 跳过 | `python -m pytest -q` |

偏差与说明：

- Python 根包命名为 `aip`（`platform` 与标准库冲突），architecture.md 第 13 章已同步。
- 资源动作路径改为子路径动词（`/connections/{id}/test`），architecture.md 与 conventions.md 已同步。
- 本机无 make，提供 `tools/dev.py` 作为跨平台入口；Makefile 为薄封装。
- CI 工作流（`.github/workflows/ci.yml`）已编写，尚未在 GitHub 上实际运行；openapi-diff 只在 PR 上执行。

## M1 自建 IAM（P1）— 2026-10-19 通过（含已记录的延后项）

测试库：每次测试运行重建独立库 `aip_test` 并执行全部迁移；应用连接使用非超级用户 `aip_app`（`rolsuper=false, rolbypassrls=false`），RLS 对它生效。

| 第 14 章 M1 验收项 | 结果 | 证据（tests/） |
| --- | --- | --- |
| 伪造、过期、重放、错误 aud 的断言全部被拒 | 通过 | `test_m1_tokens.py`：伪造签名、未知 kid、过期、有效期 > 5 分钟、重放、错 aud / iss、缺 jti / sub、iat 在未来、HS256 与 none 算法、降级模式只收短期断言；`test_m1_oauth.py::test_bad_assertions_rejected_at_token_endpoint` 4 种在令牌端点被拒 |
| 令牌交换签发的令牌只含认证信息 | 通过 | `test_token_exchange_issues_auth_only_user_token`、`test_platform_token_has_only_auth_claims`：claim ⊆ {iss, sub, aud, iat, exp, jti, act, kind, agent_profile}，无组织 / 角色 / 密级 |
| 未绑定用户按 bind_policy 处理 | 通过 | strict 未绑定 → USER_NOT_BOUND；by_employee_no 只在消费方所属公司内匹配工号；app_only 不能代表用户 |
| 分公司管理员只能改本公司子树 | 通过 | `test_company_admin_confined_to_own_subtree`、`test_company_admin_cannot_move_across_companies_even_with_visibility`（即使事务可见全部数据，RBAC 仍按生效节点拒绝）、HTTP 层 `test_http_scope_and_error_contract` |
| 密级提升必须经审批 | 通过 | 分公司管理员只能发起申请、不能审批；数据 Owner 审批后生效；不能审批自己发起或针对自己的申请；覆盖值过期后回落到组织默认 |
| 组织迁移后数据范围自动更新 | 通过 | `test_org_move_updates_data_scope_automatically`：L1 迁到品质部后，品质部用户范围扩大、L1 员工的组织链更新；跨公司迁移同步改写用户所属公司 |
| A 公司身份查不到 B 公司数据（每张表一个 RLS 用例） | 通过 | `test_rls_company_isolation` 对 iam 下 15 张业务表逐表验证；跨公司写入被 RLS 拒绝、跨公司 UPDATE 影响 0 行 |
| 断言调试器可用 | 通过 | `test_full_console_flow`：验签、声明、绑定、Principal；不消耗 jti，同一断言可重复调试；篡改签名给出错误原因 |
| 其他 | 通过 | 登录强制 TOTP、5 次失败锁定、用户名不存在与口令错误返回相同信息、停用用户会话立即失效；CSV 导入 dry-run 不落库、越权行与缺字段行报错；快照被篡改或被他人密钥签名时数据面拒绝加载；无快照时数据面返回 POLICY_UNAVAILABLE；停用用户、吊销静态令牌在下一份快照后即被拒；staging / prod 拒绝沿用 dev 默认密钥 |
| lint | 通过 | ruff 全部通过；mypy --strict 34 个文件无问题；TLS 约束检查通过 |
| 测试总数 | 97 通过，0 失败，0 跳过（M0 34 + M1 63） | `python -m pytest -q`，约 44 秒 |

延后项（不影响 M1 验收，已排入后续里程碑）：

| 项 | 现状 | 计划 |
| --- | --- | --- |
| 平台签名密钥与 TOTP 种子加密 | 本地密钥（prod 环境强制显式配置） | M2 接 OpenBao Transit |
| IAM 快照 | 全量快照 + 数据面 2 秒拉取 | M2 增量快照与 LISTEN/NOTIFY 推送，压测 30 秒传播目标 |
| by_employee_no 首次匹配自动写入绑定 | 匹配成功即可调用，但不回写绑定表 | M2 由数据面发事件、控制面落库 |
| 批量密级提升双人复核、启用 by_employee_no 双人复核 | 目前仅限平台管理员操作 | M4 随审批流（P7）实现 |
| 导入批次回滚 | 记录 batch_id，未提供回滚接口 | M4 控制台 |
| 控制面响应体 | 字段与 OpenAPI 一致，尚未用 Pydantic 响应模型强校验 | M4 补响应模型与契约测试 |
| break-glass 告警 | 写 critical 日志与领域事件 | M2 接入审计与告警通道 |
| 服务间认证 | dev 共享服务令牌 | M7（P12）mTLS + 短期服务令牌 |
