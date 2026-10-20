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

## M2 Registry、PDP、审计（P2、P4）— 2026-10-19 通过（含已记录的延后项）

| 第 14 章 M2 验收项 | 结果 | 证据（tests/） |
| --- | --- | --- |
| 已发布版本改动被触发器拒绝 | 通过 | `test_published_version_is_immutable_at_db_level`：用 owner 账号直连数据库，离开 draft 后改 spec、content_hash、semver 与 DELETE 均被触发器拒绝；应用层同样拒绝（`..._at_app_level`），同版本号不可覆盖 |
| 停掉 control_plane 数据面仍能按快照解析 Tool | 通过 | `test_data_plane_resolves_tools_after_control_plane_goes_down`：数据面经 HTTP 拉取快照并落盘；新进程指向不存在的控制面地址，从本地磁盘恢复已验签快照，解析 Tool 成功且 /readyz 为 200；被篡改的本地快照拒绝恢复（`test_tampered_local_snapshot_is_not_restored`） |
| PDP 表驱动用例 ≥ 40 个且默认拒绝 | 通过 | `test_policy_table` 44 个用例在真实 OPA sidecar 上运行（字段缺失、类型错误、未知状态、空输入全部拒绝）；`opa test` Rego 单元测试通过；多个拒绝原因同时返回 |
| IAM 快照缺失时全部拒绝 | 通过 | `test_fail_closed_without_iam_snapshot` 返回 POLICY_UNAVAILABLE 并写审计；OPA 不可达同样拒绝（`test_fail_closed_when_opa_unreachable`） |
| 篡改一行审计后校验脚本报错并定位到 shard 与 seq | 通过 | `test_tampering_one_row_is_detected_with_shard_and_seq`：超级用户跳过触发器改写一行后，`tools/audit_verify.py` 退出码 1 并输出 `shard=dp-b-20261019 seq=3`；删除锚定记录、删除中间记录、伪造锚点签名均被发现 |
| 状态机与发布 | 通过 | 合法与非法迁移、Suspended 只能回到原状态、迁移日志；发布替代、一键回滚、回滚后快照只含旧版本；danger 不能开放 MCP |
| 授权事实 | 通过 | 本厂参数放行、他厂参数与缺失参数拒绝（by_code 映射）；无授权、上限撤销后授权失效、用户层收窄（只授予品质部）、密级审批前后对比、Suspended 拒绝 |
| 审计可靠性 | 通过 | 应用角色不能 UPDATE / DELETE / TRUNCATE；owner 也被追加保护触发器拒绝；审计库不可用时继续写 spool、恢复后补投；offset 丢失重复投递被主键去重；spool 80% 背压与满载拒绝；进程重启链不断；按月分区按需创建；数据面后台任务自动入库并锚定；审计只存参数摘要不存原值 |
| lint | 通过 | ruff、mypy --strict（50 个文件）、TLS 约束检查 |
| 测试总数 | 176 通过，0 失败，0 跳过（M0 34 + M1 63 + M2 79） | `python -m pytest -q`，约 2 分钟 |

延后项：

| 项 | 现状 | 计划 |
| --- | --- | --- |
| 平台签名密钥、TOTP 种子加密、审计锚点签名改用 OpenBao Transit | 本地密钥（prod 强制显式配置） | M4 上线前 |
| 增量快照与 LISTEN/NOTIFY 推送、30 秒传播压测 | 全量快照 + 2 秒拉取 | M4 压测时一并完成 |
| by_employee_no 首次匹配回写绑定 | 未回写 | M4 |
| 发布与授权的审批流（目前平台管理员直接操作） | 服务与接口已就绪 | M4（P7） |
| docs/specs 与 policies 打包进镜像 | 运行时从仓库路径读取 | M4（P12 部署） |

## M3 Connector 与 Runtime（P3）— 2026-10-19 通过

| 第 14 章 M3 验收项 | 结果 | 证据（tests/） |
| --- | --- | --- |
| **HTTP、自签 HTTPS 的 pinned 与 insecure 三种连接测试通过** | 通过 | `test_m3_tls_matrix.py`：T-HTTP-01、T-PIN-01、T-INS-01，均同时通过 `probe()` 连接测试判定 |
| 附录 B TLS 测试矩阵全部通过 | 通过（26 / 26） | 正向：SPKI 指纹、双指纹轮换、同密钥续签、过期证书仅告警、insecure 按 IP 校验主机名、自签证书作为 custom_ca、下游 mTLS；反向：指纹错误 / 证书过期强制 / 主机名不符 / 指纹变化阻断时**服务端请求数为 0**，system_ca 拒绝自签证书、custom_ca 主机名不符、协议不一致、HTTPS→HTTP 重定向不跟随、访问登记主机以外的地址被拒、公网地址被拒、DNS 重绑定被拒、传输例外过期禁用并发事件、出站日志字段完整且不含凭据、错误码精确映射 |
| 集成测试覆盖超时、熔断、幂等重放、沙箱拒写、unknown 对账、并发刷新只登录一次 | 通过 | `test_m3_runtime.py`：超时、读操作重试、连续 5 次失败后熔断且不再打下游、限流、同键重放不再调用下游 / 同键不同参数冲突 / 写操作缺幂等键拒绝、沙箱禁止写生产连接（服务端请求数 0）、写超时进入 unknown 且不重放 → 对账成功或确认未执行、10 个并发调用只登录 1 次且 401 后刷新一次重试成功 |
| CI 的 verify=False 检查生效 | 通过 | M0 `test_m0_tls_lint.py` 持续运行；httpclient 包是唯一豁免目录 |
| 一个声明式 Connector 从 Mock 跑通 G1 契约测试 | 通过 | `test_G1_declarative_connector_against_prism_mock`：按旧系统 OpenAPI（`tests/contracts/mes-vn01.openapi.yaml`）启动 Prism，请求经 Prism 校验、返回值符合 Tool output_schema；分页上限被映射截到 200 |
| 其他 | 通过 | 传输例外生产环境需两名不同数据 Owner 审批、创建者不能审批、到期日不超过 90 天、到期自动禁用；T-GEN-N6（例外连接上的 user_delegated Tool 发布被拒）；连接测试接口 + “固定此证书”草稿且不发 HTTP 请求；发布快照携带 Connector 实例与连接（只含凭据引用）；Postgres 唯一约束保证并发同键只执行一次；kb Connector；业务码失败不泄露下游信息；Result Guard 按 output_schema 裁剪、按密级脱敏 PII、按上限截断；PDP 拒绝与确认贯通；REST `/invoke` 与 `/executions` |
| lint | 通过 | ruff、mypy --strict（63 个文件）、TLS 约束检查 |
| 测试总数 | 227 通过，0 失败，0 跳过（M0 34 + M1 63 + M2 79 + M3 51） | `python -m pytest -q`，约 3.5 分钟 |

实现中修复的缺陷：事件回调为空列表时被当作“未提供”而丢事件（改为 `is not None`）；模板 `default` 过滤器遇到未传参数时报错（StrictUndefined 下需显式判断 Undefined）。

延后项：

| 项 | 现状 | 计划 |
| --- | --- | --- |
| 连接测试展示完整证书链 | Python 3.12 只能取叶子证书 | 升级到 Python 3.13（`get_unverified_chain`） |
| insecure 指纹记录回写控制面 | 数据面进程内记录并发事件 | M4 事件回传通道 |
| 写操作确认对象（confirmation）与确认通道 | Runtime 返回 pending_confirm，不执行 | M5（P8） |
| 对账自动调度 | 提供 `reconcile()`，由调用方触发 | M5 reconcile worker |
| Result Guard 的“不可信内容”标注 | 未加 | M4 MCP 出口一并实现 |

## M4 Gateway、门禁、审批、首期上线（P5、P6、P7、P12 部署部分）— 2026-10-20 通过（含已记录的延后项）

| 第 14 章 M4 验收项 | 结果 | 证据（tests/） |
| --- | --- | --- |
| 同一调用经 MCP（两个版本）与 REST 结果与审计一致 | 通过 | `test_m4_mcp.py`：官方 MCP Python SDK 2.3 客户端分别以 legacy（2025-11-25 握手）、2026-07-28（无状态）与 auto 协商连接；三条调用返回同一数据，审计中三条决策同一用户、同一参数摘要、exposure 分别为 mcp / mcp / rest |
| 未授权工具不出现在 tools/list | 通过 | 无权（密级不足）与只开放 REST 的工具不可见；直接调用它们与调用不存在的工具返回同一个 `Unknown tool` 错误，不泄露能力是否存在 |
| 一个读类 Tool 从 Draft 走到 Active | 通过 | `test_read_tool_from_draft_to_active`（服务层）与 `test_end_to_end_publish_and_call_over_mcp`（全程 HTTP）：G0 → Test Lab G1 / G2 → 业务 Owner G5 签字 → 数据 Owner G6 → G7 灰度 → G8 全量；门禁记录依次为 G0、G1、G2、G5、G6、G7、G8 |
| 上限收缩后下游授权自动失效 | 通过 | `test_usage_request_within_ceiling_creates_grant`：经使用申请批准产生的授权，在上限撤销后的快照中 within_ceiling=false，PDP 拒绝（M2 已验证拒绝路径） |
| 端到端演示 | 通过 | 控制面与数据面以真实 HTTP 运行：越南分公司消费方以员工身份经 MCP 查询本厂工单成功、查询他厂返回 OUT_OF_SCOPE |
| 压测校准非功能指标并写入 docs/ops/slo.md | 通过 | 单请求平台附加延迟 P95：REST 12.8ms、MCP 14.7ms（目标 ≤ 100ms）；单进程并发 20 时约 200 req/s；停用传播 0.14 秒（目标 ≤ 30 秒）。详见 [slo.md](slo.md) |
| 其他 | 通过 | G0 拦截零宽字符、诱导越权措辞、外链与缺少权限用例；用例失败退回 draft 并给出原因；write 工具 G6 双人复核；验收人不能再做安全审核；审批代理人；使用申请只能在上限内且申请人不能自批；复核活动（续期 / 关闭时撤销未确认）；MCP 协议细节（401 + RFC 9728 元数据、-32022 版本不支持、-32020 路由头不一致、-32601、-32700、通知 202、cacheScope private、确定性排序、写工具幂等重放） |
| lint | 通过 | ruff、mypy --strict（75 个文件）、TLS 约束检查、spectral、链接检查 |
| 测试总数 | 252 通过，0 失败，0 跳过（M0 34 + M1 64 + M2 80 + M3 51 + M4 23） | `python -m pytest -q`，约 4.5 分钟 |

关闭的延后项：

| 来自 | 项 | 结果 |
| --- | --- | --- |
| M1 / M2 | 平台签名密钥、审计锚点签名、TOTP 种子加密改用 OpenBao Transit | `signing_backend=transit` / `secret_backend=transit`；私钥不出 OpenBao，访问令牌手工组装 ES256 JWS；轮换后旧令牌与旧快照仍可验签、只有最新版本可签名；旧本地密文可继续解密（`test_m4_transit.py`） |
| M1 / M2 | 30 秒传播与推送 | 控制面 LISTEN / NOTIFY + 长轮询，实测 0.14 秒 |
| M1 | by_employee_no 首次匹配回写绑定 | 数据面事件回传 `/api/v1/internal/events`，控制面写入绑定 |
| M1 | 导入批次回滚 | `/users/import/{id}/rollback`：未使用的删除、已有绑定或角色的停用 |
| M2 | 发布与授权的审批流 | 通用审批引擎（G5 / G6 / 使用申请），职责分离、双人复核、代理人、工作日到期 |
| M2 | docs/specs 与 policies 打包进镜像 | `deploy/Dockerfile`；镜像实测：只读根文件系统、非 root（uid 10001）下控制面健康并能生成带策略的发布快照，数据面无快照时 503（fail closed） |
| M3 | insecure 指纹回写控制面 | 同一事件通道，控制面记录首次指纹，指纹变化与传输例外到期作为风险事件并触发新快照 |
| M3 | MCP 结果的不可信内容标注 | `_meta` 中 `aip.internal/untrusted: true` 与执行 id |

本里程碑发现并修复的问题：

- 审计 spool 在事件循环上同步 fsync，并发下整个进程排队 → 组提交（每条记录返回前仍已落盘）。
- 运行环境缺少 `sniffio` 时 httpcore 每次请求重新尝试 import 并扫描磁盘 → 显式依赖。
- `jsonschema` 只在开发依赖里，镜像启动即失败 → 改为运行时依赖，并新增“src 的第三方导入必须是运行时依赖”的测试。
- 资产开发者不是跨公司角色，Test Lab 看不到分公司组织 → 资产开发者列为总部全局角色（architecture.md 2.5 已同步）。
- 分公司在 RLS 下看不到总部资产，无法发起使用申请 → 迁移 0006：上限内的总部资产对该公司只读可见。
- 请求头断言模式（模式 B）不适用于 MCP 客户端（同一组头被复用，第二个请求即被判重放）→ architecture.md 3.1 明确 MCP 只用令牌交换。

仍未完成：

| 项 | 现状 | 计划 |
| --- | --- | --- |
| 控制台前端（最小页面集） | 已完成，见下节 | - |
| 控制面响应体 Pydantic 模型 | 字段与契约一致，未用响应模型强校验 | 随控制台一起补 |
| 批量密级提升、启用 by_employee_no 的双人复核 | 仅限平台管理员 | 接入审批引擎（M5） |
| Kubernetes 清单 | 已编写并通过 YAML 校验，未在真实集群部署 | 首个 staging 集群上线时验证 |
| CI 工作流 | 已编写，未在 GitHub 上运行 | 推送远端仓库后启用 |
| 多进程 / 多副本线性扩展 | 只测了单进程 | M7 故障演练与容量测试 |

## 控制台最小页面集 — 2026-10-20 通过

React 18 + Ant Design 5 + Vite，构建产物由控制面挂在 `/console/`（同源，无 CORS）；会话令牌只放 sessionStorage，401 自动回登录页。菜单按角色裁剪，权限与数据范围仍以后端为准。

| 页面 | 功能 | 用到的接口 |
| --- | --- | --- |
| 登录 | 用户名 + 口令 + TOTP | `/auth/login`、`/me`、`/auth/logout` |
| 组织与用户 | 组织树、按子树过滤用户、新建下级与停用、新建用户（一次性展示 TOTP）、CSV 导入预览与确认 | `/orgs`、`/users`、`/users/import` |
| 消费方与绑定 | 注册消费方、用户绑定、签发静态 Token（只显示一次）、登记 JWK、断言调试器 | `/consumers`、`/consumers/{id}/bindings`、`/credentials`、`/jwks`、`/assertion-debug` |
| 系统连接 | 登记连接（system_ca / custom_ca / pinned / insecure / http，例外理由与 ≤ 90 天到期）、审批例外、连接测试（解析地址、白名单、证书链、两种指纹）、生成 pinned 草稿 | `/connections`、`/test`、`/approve-exception`、`/trust-draft` |
| 资产与门禁 | 资产与版本、Spec 模板、门禁用例、验收单模板、提交 G0、运行 G1 / G2、门禁记录、灰度与全量 | `/assets`、`/assets/{id}/versions`、`/versions/{id}`、`/test-cases`、`/acceptance`、`/submit`、`/run-tests`、`/gates`、`/canary`、`/promote` |
| 审批待办 | G5 / G6 / 使用申请，显示“资产@版本”或“资产（环境）”，G5 填写实测结果，驳回必须写理由 | `/approvals`、`/approvals/{id}/decide` |
| 授权 | 授权上限、授权（只能选上限内资产）、使用申请、访问复核发起与关闭 | `/grant-ceilings`、`/grants`、`/usage-requests`、`/access-reviews` |
| 审计 | 按类型、trace_id、时间、条数检索；展开原始记录；校验哈希链与签名锚点 | `/audit-logs`、`/audit-logs/verify` |

为控制台补充的后端接口（契约已更新）：版本列表与详情（含用例与验收单）、授权上限列表、使用申请列表、审计检索过滤（分公司管理员只看本公司，最多 500 条）、审计链校验（仅审计员与平台管理员）、审批待办的可读摘要。

| 验收项 | 结果 | 证据 |
| --- | --- | --- |
| 类型检查与构建 | 通过 | `npm run typecheck`、`npm run build` |
| 组件测试 | 14 通过 | `console/src/**/*.test.ts(x)`：RFC 9457 错误解析、401 回调、登录（错误动态码提示、成功后进入首页）、菜单按角色裁剪、传输标签、审计筛选与校验按钮的角色可见性、驳回必须写理由 |
| 后端接口测试 | 4 通过 | `tests/test_m4_console_api.py`：版本列表与详情、上限与审批待办的 RLS 隔离与摘要、审计检索过滤与公司范围、校验权限、`/console/` 静态托管 |
| 全量后端测试 | 256 通过，0 失败，0 跳过 | `python -m pytest -q`，约 4.6 分钟；ruff、mypy --strict（76 个文件）、TLS 约束、spectral、链接检查均通过 |
| 浏览器端到端 | 6 通过 | `console/e2e/console.spec.ts`（Playwright + Chromium，对 `tools/dev.py serve` 的真实控制面）：TOTP 登录（先错后对）、组织树、登记明文 HTTP 连接并执行连接测试、新建资产与版本并由 G0 列出缺失项、下发授权上限、审计链校验、退出 |
| 镜像内置控制台 | 通过 | `deploy/Dockerfile` 增加 Node 22 构建阶段，产物放在 `/app/console/dist`（`AIP_CONSOLE_DIST`）；以 `--read-only`、uid 10001 运行控制面容器，`GET /console/` 与入口脚本均返回 200 |
| 按路由分包 | 通过 | 除登录页外各页面 `React.lazy` 按需加载；入口 749 KB（gzip 243 KB），原单包 1.27 MB（gzip 398 KB） |

本次发现并修复的问题：

- 组织树数据异步到达时 `defaultExpandAll` 不生效，树只显示根节点 → 数据到达后再挂载。
- 连接测试对明文 HTTP / 不校验证书的通过文案写着“已批准的传输例外”，而连接实际仍待审批 → 改为“传输例外：须经数据 Owner 审批生效”。

仍未完成：

| 项 | 现状 | 计划 |
| --- | --- | --- |
| 控制面响应体 Pydantic 模型 | 前端类型按契约手写 | 引入响应模型后由 OpenAPI 生成前端类型 |

## M5 写操作与工作流（P8）— 2026-10-20 通过（含已记录的延后项）

ADR-0003 先做 spike（[对比记录](spike-adr-0003-workflow-engine.md)），决策为自研 step journal（ADR-0003 已改为 Accepted）。

| 第 14 章 M5 验收项 | 结果 | 证据（tests/） |
| --- | --- | --- |
| 杀掉 worker 后 Run 可恢复 | 通过 | `test_m5_crash.py`：数据面作为独立进程运行，在写请求“下游已生效、平台未记结果”时 TerminateProcess（等同 kill -9）；另一个实例在租约过期后接手，执行记录判定陈旧转 unknown，用幂等键对账确认已执行，Run 成功，下游只收到一次写请求；接手到完成约 2.6 秒（租约 2 秒、陈旧阈值 1 秒），连续 4 次稳定。`test_m5_workflow.py::test_lease_takeover_after_worker_dies`：进程内同样场景，对账确认未执行时按幂等策略重试 |
| 补偿逆序执行 | 通过 | `test_saga_compensates_in_reverse_order`：预算预留 → 审批 → 记账失败，补偿顺序为撤回审批、释放预算；人工驳回同样触发补偿 |
| LLM 节点无法改变分支 | 通过 | 静态：LLM 节点带 then / else / on_timeout / on_reject / branches 时 G0 拒绝；运行：LLM 输出夹带 `next`、`goto`、`decision` 等字段被按 output_schema 裁掉，流程只由 condition 决定 |
| 确认参数被篡改时拒绝执行 | 通过 | `test_m5_confirmations.py`：消费方声明的确认参数与冻结内容不一致、存储中的参数哈希被改动，都返回 CONFIRMATION_MISMATCH 且不执行 |
| 同一 confirmation 只执行一次 | 通过 | 重复确认返回同一执行记录；6 个并发确认只有一个执行；下游幂等键 = confirmation_id |

实现要点：

| 部分 | 内容 |
| --- | --- |
| 确认对象（4.5） | `exec.confirmation`：参数加密存放 + 规范化 JSON 的 SHA-256，执行前解密核对；文案只由 Tool Spec 的 `confirm_template`（沙箱 Jinja）渲染。user_confirm 默认 10 分钟、approval 1 天过期 |
| 确认通道 | 消费方回调 `POST /confirmations/{id}/confirm`：同一用户的新断言（jti 未用过、iat 不早于确认创建、用户一致），审批类不能走回调；平台待办：控制台 `GET /todo` 与 `POST /todo/confirmations/{id}/decide`，approval 需具备 approver_role 且角色范围覆盖该公司、不能是被代表用户本人，user_confirm 只能本人。MCP 交互式确认（elicitation / MRTR）按计划在 M7 |
| DSL 静态校验（6.3） | `aip.libs.workflow.dsl`（控制面 G0 与数据面启动共用）：JSON Schema + 节点 id 唯一、跳转目标存在、可达、无环、表达式只引用一定先执行的节点、写节点有补偿或不可逆且每条路径先经人工确认、assignee 可解析、引用的 Tool 版本可解析 |
| 条件表达式 | CEL 子集：比较、`&&` `||` `!`、`in`、列表、字面量、数据路径；不能调用函数或写入；路径缺失为 null |
| 引擎（8.5） | `exec.wf_run / wf_step / human_task`；FOR UPDATE SKIP LOCKED + 租约；节点完成与指针前移同一事务；wait、人工任务、确认、对账退避、Run 总期限都落在 `wake_at`；预算（步数、LLM 调用、token、总期限）；Run 钉住 DSL、版本与 Principal |
| Tool 节点 | 经统一执行链（PDP、确认、幂等、重试、审计），以 Run 的 Principal、`exposure=internal` 判定；写操作与需确认的调用带幂等键 `wf:{run}:{node}`，只读调用崩溃后直接重做；结果未知按指数退避对账，5 次仍不确定生成平台管理员补录任务 |
| 污点（8.7） | Tool Spec 新增 `untrusted_output`；用到后 Run 标记 tainted，之后 write / danger / egress=external 节点强制人工确认（发起人本人，应用身份则数据 Owner） |
| G3（7.1） | Test Lab 每个用例重复 20 次（`AIP_G3_REPEATS`），比对状态、错误码与返回内容哈希；不一致则 G3 失败、退回 draft。M4 的门禁序列相应变为 G0、G1、G2、G3、G5…… |
| Workflow 门禁 | G0 跑 DSL 静态校验；G1–G3 为组合门禁：引用的每个 Tool 版本必须已是 approved 及以后状态 |
| 控制台 | 审批待办增加“调用确认”“工作流任务”两页：通过 / 驳回 / 补录（按 input_schema），可展开 Run 的节点轨迹、预算用量与污点来源 |

| 其他验收 | 结果 | 证据 |
| --- | --- | --- |
| 引擎场景 | 11 通过 | `test_m5_workflow.py`：人工审批、低风险跳过、补偿、驳回、Run 内确认只执行一次、污点强制确认、步数 / LLM 调用 / 总期限预算、结果未知对账、租约接手、REST 幂等启动与 Run 可见性（应用身份启动时写步骤被 PDP 拒绝） |
| 确认流 | 6 通过 | `test_m5_confirmations.py`：上表两项之外，还有断言规则（缺断言、换人、重放、早于创建）、平台待办审批执行、拒绝与过期 |
| 门禁与待办 | 3 通过 | `test_m5_gates_todo.py`：G3 拒绝不确定的 Tool；Workflow G0（环、引用不存在的版本）与组合门禁；待办授权（角色范围、职责分离、本人确认、指定人任务） |
| 崩溃恢复 | 1 通过 | `test_m5_crash.py` |
| 控制台 | 15 通过 | 新增待办组件测试：审批调用确认、补录任务提交 JSON |
| 全量后端测试 | 277 通过，0 失败，0 跳过（新增 21） | `python -m pytest -q`，约 5.6 分钟；ruff、mypy --strict（85 个文件）、TLS 约束、spectral、链接检查 |

本里程碑发现并修复的问题：

- 契约测试一直没有检查经 `include_router` 挂载的路由（FastAPI 0.146 起包装为 `_IncludedRouter`，没有 path 属性，被静默跳过）→ 展开后检查；此前的路由全部已登记，只有本次新增的需要补。
- 只有 company_admin 的角色生效范围被展开，审批与待办无法按角色范围判断 → 所有角色都展开。
- 架构文档 6.3 示例的 `role:purchase_manager@org_of(input.company)` 写法 M0 测试里被简化掉了 → Schema、校验器、引擎都支持 `org_of(input.x)`，测试与文档一致。
- `JsonInput` 没有把表单项 id 传给输入框，标签与输入框不关联（读屏与按标签定位都受影响）→ 透传 id。

仍未完成：

| 项 | 现状 | 计划 |
| --- | --- | --- |
| loop、parallel、sub_workflow 节点 | Schema 已有，G0 明确拒绝（“M5 引擎暂不支持”） | M6 |
| Workflow 在 Test Lab 沙箱整体回放 | 组合门禁代替；沙箱 Run 需要与生产运行表隔离，否则会被生产 Worker 认领 | M6：独立的沙箱运行 schema 与 Worker |
| 业务角色（如 purchase_manager） | 人工任务只能分派给平台角色 | 随 IAM 业务角色一起做 |
| LLM Gateway（8.6） | 引擎只依赖接口；未配置时 LLM 节点失败 | 单独模块 |
| 通知（Notifier：邮件 + IM） | 待办只在控制台与 API 可见 | M6 |
| MCP 交互式确认（elicitation / MRTR）、tasks 扩展 | 未做 | M7 |
| 人工任务的升级与代理人 | 超时走 on_timeout 或失败；未接审批代理人 | M6 |
| Run 上下文与执行结果加密存放（12.6 信封加密） | 与 tool_execution 一样明文 JSON | 与 KMS 信封加密一起做 |

## 控制台：品牌、多语言与主题 — 2026-10-20

平台名称定为“申洲集团AI数字资产管理中台”，侧栏、页头、登录页与浏览器标签使用集团标志（深色底用白色标志，浅色底用申洲蓝着色版本，favicon 取标志图形）。

| 能力 | 实现 |
| --- | --- |
| 多语言 | 简体中文、English、Tiếng Việt。界面文案以中文原文为键（`t("…")`，`{0}` 按位置传参），英文与越南文字典在 `console/src/locales/`；Ant Design 组件与日期（dayjs）跟随语言。切换语言写入 localStorage 并重新加载页面。服务端返回的错误详情仍是中文（同时显示错误码） |
| 主题 | Ant Design 5 的 algorithm 与 token：浅色 / 深色（首次跟随系统）、四种主色（申洲蓝、青绿、紫、橙）、紧凑模式，即时生效并记住选择；侧栏固定为深色品牌底 |
| 测试 | 字典覆盖测试（源码中每个键都有英文与越南文译文，占位符一致）、英文登录页渲染、主题切换与持久化；控制台组件测试 20 通过，浏览器端到端 6 通过 |

仍未完成：状态码（如 `pending_approval`）在表格中按原值显示，未做多语言映射。

## M6 风控、分发与目录（P9 风控部分、P10）— 进行中

按切片交付，每片单独测试与提交：M6a 风控与配额 → M6b 套件、目录与 webhook → M6c Skill Registry 与 corp-skill → M6d 合成探针与契约漂移 → M6e M5 延后项与控制台页面。

### M6a 风险规则、自动熔断与复核恢复、配额与计量 — 2026-10-20

| 验收项 | 结果 | 证据（tests/） |
| --- | --- | --- |
| 触发规则后自动熔断并告警，恢复需复核 | 通过 | `test_m6_risk.py::test_bulk_write_suspends_and_resume_needs_review`：批量写超过阈值 → 能力当前对外版本自动 Suspended（发布快照中状态为 suspended，PDP 拒绝调用）、告警通知；同一窗口重复扫描不重复处置；恢复须写明修复情况，发起人不能自批，另一位数据 Owner 批准后回到 active |

| 部分 | 内容 |
| --- | --- |
| 规则（`risk.rule`） | 可配置阈值规则：数据来源审计库或领域事件，过滤（等于 / 数组包含）、分组、窗口、阈值或比例（带最小调用数）、非工作时段（按公司时区）。字段名白名单后才进 SQL，取值全部绑定 |
| 内置规则（12.4） | 调用频率异常、批量写（自动熔断）、越权尝试、密级异常、断言验签失败激增（数据面新增 `auth.assertion_failed` 审计）、证书指纹变化、错误率熔断（自动熔断）、非工作时段写操作；阈值是起始值，平台管理员可在控制台调整 |
| 事件（`risk.event`） | 每条规则每个分组每个窗口最多一条（唯一约束，多副本并发扫描也只处置一次）；告警类可确认关闭，熔断类只能走复核恢复（审批 kind=resume） |
| 扫描 | 控制面后台每 30 秒（`AIP_RISK_SCAN_INTERVAL_S`），平台管理员可手动触发 |
| 配额 | 消费方每日调用 / 写操作（`iam.consumer.quota`）、授权上限内全公司共享的每日调用（`grant_ceiling.quota`）；数据面按 UTC 日在 PostgreSQL 条件自增，同一次调用的所有计数同一事务，被拒的调用不消耗额度，幂等重试不重复计数；超限返回 RATE_LIMITED |
| 计量 | `GET /usage`：按日、公司、消费方、能力汇总调用、失败、写操作（来源审计库）；分公司管理员只看本公司 |
| 控制台 | 新增“风控与计量”：风险事件（确认、申请恢复、立即扫描）、风险规则（窗口、阈值、启停）、计量；中英越三语 |

| 其他验收 | 结果 | 证据 |
| --- | --- | --- |
| 风控 | 6 通过 | `test_m6_risk.py`：比例规则、告警规则（越权、密级、断言失败、指纹变化）、确认与熔断类不能直接关闭、非工作时段按越南时区、规则校验（防注入、熔断规则必须按能力分组、时区合法）、HTTP 接口与公司隔离、数据面记录断言验签失败 |
| 配额与计量 | 4 通过 | `test_m6_quota.py`：每日调用上限且超限不到达下游、跨副本共享计数、写配额与幂等重试、上限共享额度在 9 个并发里恰好放行 5 个、配额接口校验、快照携带配额、计量与公司范围 |
| 全量 | 287 通过，0 失败（新增 10） | `python -m pytest -q`；ruff、mypy --strict（89 个文件）、TLS 约束、spectral、链接检查；控制台组件 20、浏览器端到端 6 |

本切片发现并修复：Windows 与精简镜像没有系统时区库 → 运行时依赖加入 `tzdata`。
