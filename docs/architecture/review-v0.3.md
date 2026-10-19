# 企业 AI 资产管理中台 v0.3 审核报告与 v0.4 变更说明

2026-10-19 · 审核对象：《企业 AI 资产管理中台 架构设计 v0.3（完全自建版）》

## 1. 总体结论

**可以落地，但 v0.3 直接交给 Cursor 开工会在 5 个地方卡住**。治理主线（三层资产模型、门禁、五方交集、控制面与数据面分离、发布快照、Connector 保护）是对的，与业界同类开源项目的思路一致，而且比大多数开源项目更严。问题集中在下面这些地方：

| # | 阻断级问题 | 影响 | v0.4 处理 |
| --- | --- | --- | --- |
| B1 | **MCP 规范已在 2026-07-28 版改为无状态**：去掉 initialize 握手和 Mcp-Session-Id，取消 HTTP GET 流、progress 之外的服务端推送，长任务移到 `io.modelcontextprotocol/tasks` 扩展，版本协商改为每个请求的 `_meta` 加 `server/discover` | v0.3 的“MCP-Protocol-Version 协商”“MCP progress 返回 Agent 结果”“按连接过滤 tools/list”按旧规范写，P5、P11 会写错 | 第 10 章重写：同时支持 2025-11-25 与 2026-07-28，tools/list 按 Principal 过滤并返回 `cacheScope: private`，异步 Agent 改用 tasks 扩展加 get\_run\_status 兜底 |
| B2 | **写操作“用户确认”没有落地流程** | confirm\_policy=用户确认 时，外部 Agent 到底怎么让用户点确认、平台怎么验证确认的是同一个人，v0.3 没写 | 新增 4.5 确认与审批流：confirmation 对象、三种确认通道（MCP input\_required、消费方回调、平台待办），确认需同一用户的新断言 |
| B3 | **审计“零丢失 + 同步写 + 哈希链 + P95 ≤ 100ms”互相冲突**：全局单链哈希要求串行写入，多副本 Gateway 下会成为吞吐瓶颈 | 压测必然不达标 | 改为“本地持久化 spool + 分片哈希链 + 每小时锚定签名”，见 12.4 |
| B4 | **Python 的 ssl 不支持“关闭证书校验但仍校验主机名”**，httpx 也没有原生证书固定；若在请求发出后才比对指纹，Authorization 已经发给了中间人 | 9.6 的 insecure+主机名 和 pinned 按直觉实现会有安全漏洞 | 已用 PoC 验证可行做法：在 httpcore 网络后端的 start\_tls 之后、写入任何 HTTP 字节之前校验指纹与主机名（见 reference/tls\_client\_factory\_poc.py，11 个用例全部符合预期，指纹错误时服务端请求数为 0） |
| B5 | **Connector 全是代码**：每接一个系统都要写 Python、发平台版本 | 几十个分公司系统 × 每个几到几十个接口，交付速度撑不住 | 新增“声明式 REST Connector”（YAML 映射，无代码覆盖 70% 到 80% 的 REST 接口），代码 Connector 只用于复杂系统；Connector 独立打包与版本化 |

另外还有 3 个**合规与选型风险**需要开工前拍板：

| # | 风险 | 建议 |
| --- | --- | --- |
| R1 | 平台在中国总部，通过专线调用越南、柬埔寨系统并把含个人数据的结果交给模型，构成跨境数据传输。越南《个人数据保护法》已于 2026-01-01 起施行；v0.3 把“数据出境合规”列为非目标 | **已关闭（2026-10-19）**：集团已另行解决跨境合规，平台不做相关设计（ADR-0025） |
| R2 | MinIO 社区版仓库已归档（GitHub 显示 archived），Vault 为 BSL 许可，Redis 新版本许可变化 | 与“自主可控”目标一致地替换为 SeaweedFS 或 Ceph RGW、OpenBao、Valkey，接口兼容，迁移成本低 |
| R3 | 自研工作流引擎风险高（持久化、恢复、补偿、人工任务、定时器全部自己写） | 首期仍不上 Temporal，但先做 2 周 spike 比较“自研 step journal”与 DBOS Transact（纯 Python 库、只依赖 PostgreSQL，契合现有约束）；ADR-0003 依结果定 |

## 2. 逐章审核

### 2.1 第 2 至 4 章 身份、组织、授权

做得好的：平台是唯一权威、令牌不带权限、IAM 快照 fail closed、授权上限与下放，这些都是对的，而且比 IBM ContextForge、Archestra 这类开源方案“直接信任 IdP 组映射”更适合集团多公司场景。

需要修改：

1. **表名 `user` 是 PostgreSQL 保留字**，SQLAlchemy 和手写 SQL 都会踩坑，改为 `iam_user`。
2. **用户断言只有自定义头一种形态**。第三方 MCP 客户端普遍只支持 OAuth 或静态头。v0.4 把断言改为两种用法：推荐用 RFC 7523 JWT Bearer 授权换取 5 到 15 分钟的平台访问令牌（与 MCP 官方 Enterprise-Managed Authorization 扩展同一套标准），兼容保留 X-User-Assertion 每次调用携带。平台 MCP 端点同时发布 RFC 9728 受保护资源元数据，标准 MCP 客户端可以自动发现。
3. **jti 防重放依赖 Redis，但 Redis 被定义为“可丢”**。Redis 不可用时防重放失效。v0.4：jti 存 Valkey，不可用时退化为本地缓存加“断言有效期收紧到 60 秒”，写操作在退化期间拒绝用户断言直连模式（令牌交换模式不受影响）。
4. **Grant 表 `consumer_id 或 grantee` 含义模糊**，改为显式 `grantee_type + grantee_id`，并给出唯一约束。
5. **OPA 拿全量 IAM 数据会越来越重**（几万员工 × 绑定 × 授权）。v0.4 改为：数据面的 PrincipalResolver 在 Python 侧把组织闭包、数据范围、命中的 Grant 预先算好放进 input，OPA 只保留策略和少量静态数据，策略用 OPA bundle 分发；OPA decision log 进审计。
6. **缺紧急访问（break-glass）账号**、缺高危 IAM 变更的双人复核（如批量提升密级、修改授权上限）、缺用户时区与语言。v0.4 补齐。
7. **离职处置**：v0.3 依赖分公司管理员手工操作，没有兜底。v0.4 增加“绑定到期”和“长期未调用自动停用”两条规则。

### 2.2 第 5 至 7 章 架构、资产、门禁

1. 原文三张图（总体架构、状态机、路线图）是嵌入内容，导出成 Markdown 后丢失，Cursor 看不到。v0.4 用 Mermaid 重画，状态名写死。
2. **Prompt 模板和模型配置不是资产**，但 DSL 里引用了 `po_risk_extract@3` 和 `pinned-v1`。v0.4 增加两类内部资产：`prompt`、`model_profile`，同样版本化、走门禁，不对外暴露。
3. **缺“工具选择准确率”评测**。Tool 描述是审核对象，但没有量化手段。v0.4 在 G2 增加选择评测：自然语言问题 → 期望命中的 Tool，按套件跑，阈值进验收单。
4. **缺上线后的契约漂移探测**。旧系统升级常常不通知。v0.4 为每个 Tool 增加合成探针（只读、定时、低频），失败自动标记“契约漂移”并触发回归。
5. **G0 缺工具描述与 Skill 安全扫描**（隐藏指令、Unicode 走私、越权措辞）。v0.4 在 G0 加入扫描器（可集成 snyk/agent-scan 或自研规则）。

### 2.3 第 8 章 执行引擎

1. 统一执行链里没有“确认”环节，补 4.5。
2. LLM Gateway 没写模型来源。v0.4 明确：只接 OpenAI 兼容协议，后端可以是国内云模型或本地 vLLM，按公司配置模型白名单与预算。
3. **提示注入只做了“标记不可信”**。外部 Agent 的行为平台控制不了，但平台能控制两件事：套件组合（不把“读敏感数据”和“对外发送”放进同一个套件）和平台内工作流的数据流标记（Run 中一旦引入不可信内容，后续写节点强制人工确认）。v0.4 新增 8.7 数据流护栏。

### 2.4 第 9 章 Connector 与 TLS

1. 新增声明式 REST Connector 与 Connector 打包、隔离、发布规则。
2. 数据库只读连接器补护栏：模板化 SQL、强制 LIMIT、语句超时、只读账号、EXPLAIN 成本上限。
3. TLS：保留 v0.3 的五种模式与护栏，补齐实现细节（指纹在握手后、请求前校验；insecure 的主机名检查用 cryptography 手动解析 SAN；目标地址在连接时按解析后的 IP 检查，防 DNS 重绑定；httpx 会把自定义异常包进 ConnectError，错误映射要取 `__cause__`），新增下游 mTLS 客户端证书，新增 pinned 的证书过期策略。
4. 连接测试从“展示信息”升级为可执行的测试矩阵，作为 P3 与 M3 的验收依据（见 v0.4 附录 B）。

### 2.5 第 10 章 对外暴露

见 B1。另外：A2A 已发布 1.0.x，可以按计划放第二批；Skill 分发可以参考 iFlytek SkillHub（自托管、命名空间、审核、CLI），它与 v0.3 的设计高度一致，可直接借鉴其命名空间与审核模型。

### 2.6 第 11 至 13 章 数据、部署、工程

1. RLS 与连接池：asyncpg + PgBouncer 事务模式下必须在每个事务里 `SET LOCAL app.company_id`，总部跨公司查询走单独的 BYPASSRLS 只读角色。v0.3 没写，第一周就会踩。
2. 加密：tool\_execution 的输入输出快照用按公司的数据密钥做信封加密（OpenBao Transit），删除密钥即可实现按公司销毁。
3. 选型替换见 R2。

### 2.7 第 14 至 16 章 里程碑与提示词

1. 路线图图片丢失，M0 到 M7 的内容和验收没有文字版。v0.4 写成表格，每个里程碑对应提示词和验收。
2. **P3 依赖 PDP，但 PDP 在 P4 才做**。v0.4 明确 P3 先用 PolicyService 接口加测试桩，P4 替换为 OPA，接口不变。
3. **P3 与 M3 的 TLS 验收不够具体**。已按要求改为“HTTP、自签 HTTPS 的 pinned 与 insecure 三种连接测试通过”，并列出必须同时通过的反向用例（指纹错误零请求、公网地址拒绝、协议不一致拒绝、HTTPS 到 HTTP 重定向不跟随、例外过期自动禁用等）。
4. 前后不一致：P0a 只建 ADR 0001 至 0016，但正文新增了 0017；非功能指标在 14.2 说 M3 后校准，在 16.3 说 M2 后校准。v0.4 统一为 M4 后校准（M4 才有完整调用链），ADR 统一到 0001 至 0024。
5. 缺人力与工期估算。v0.4 给出参考：6 到 8 人团队，首期 M0 至 M4 约 24 到 28 周。

## 3. 参考的开源项目与借鉴点

| 项目 | 定位 | 借鉴到 v0.4 的点 |
| --- | --- | --- |
| [IBM/mcp-context-forge](https://github.com/IBM/mcp-context-forge)（约 4.6k star，Python） | MCP / A2A / REST 网关与注册中心 | “虚拟服务器”= 我们的套件；REST 转 MCP 的声明式映射；插件化护栏；OTel 贯穿 |
| [agentgateway/agentgateway](https://github.com/agentgateway/agentgateway)（约 5.4k star，Rust，Linux 基金会） | MCP / A2A 代理 | 工具级授权、多上游合并；后续若 Python Gateway 性能不够，可作为前置代理 |
| [archestra-ai/archestra](https://github.com/archestra-ai/archestra)（约 4.4k star） | 企业 AI 平台 | “致命三要素”（私有数据 + 不可信内容 + 对外通道）护栏，催生 8.7；按环境的出站策略 |
| [preloop/preloop](https://github.com/preloop/preloop) | MCP 防火墙与审批 | 工具调用 allow / deny / require\_approval 三态策略，多渠道审批，用于 4.5 |
| [iflytek/skillhub](https://github.com/iflytek/skillhub)（约 5.2k star） | 自托管企业 Skill 注册中心 | 命名空间、团队内审核、CLI 优先、可插拔存储，用于 10.4 |
| [agentic-community/mcp-gateway-registry](https://github.com/agentic-community/mcp-gateway-registry) | 企业 MCP 网关与注册 | 注册中心与网关分离的部署方式 |
| [modelcontextprotocol/registry](https://github.com/modelcontextprotocol/registry) | 官方 MCP 注册中心 | server.json 元数据格式，可作为目录导出格式 |
| [modelcontextprotocol/ext-auth](https://github.com/modelcontextprotocol/ext-auth) | MCP 官方认证扩展 | Enterprise-Managed Authorization（RFC 8693 + RFC 7523），用于 3.1 |
| [permitio/opal](https://github.com/permitio/opal) | OPA 数据实时分发 | IAM 快照推送的另一种实现，若自研分发复杂可替换 |
| [dbos-inc/dbos-transact-py](https://github.com/dbos-inc/dbos-transact-py) | 基于 PostgreSQL 的持久化工作流库 | ADR-0003 的候选 |
| [stoplightio/prism](https://github.com/stoplightio/prism) | OpenAPI Mock | Test Lab 沙箱 Mock |
| [python-trio/trustme](https://github.com/python-trio/trustme) | 测试证书生成 | TLS 测试夹具 |
| [snyk/agent-scan](https://github.com/snyk/agent-scan) | MCP 与 Skill 安全扫描 | G0 扫描 |
| [promptfoo/promptfoo](https://github.com/promptfoo/promptfoo)、[langfuse/langfuse](https://github.com/langfuse/langfuse) | 评测与 LLM 可观测 | Skill 评测、选择评测、LLM 节点追踪（可选自托管） |
| [openbao/openbao](https://github.com/openbao/openbao)、[valkey-io/valkey](https://github.com/valkey-io/valkey)、[seaweedfs/seaweedfs](https://github.com/seaweedfs/seaweedfs) | Vault、Redis、MinIO 的开源替代 | 见 R2 |

结论：**没有一个开源项目能直接替代本平台**。它们大多是“网关 + 注册表 + 信任外部 IdP”，缺本方案最有价值的部分（自建组织与密级、授权上限下放、发布门禁与验收签字、写操作对账补偿、旧系统保护）。正确做法是自建治理核心，在协议层严格对齐标准，在局部借鉴它们的实现。

## 4. 本次验证

`docs/architecture/reference/tls_client_factory_poc.py`（Python 3.11、httpx 0.28、cryptography 43）在本机运行结果：

| 用例 | 结果 |
| --- | --- |
| HTTP 明文 | 200 |
| base\_url 协议与连接设置不一致 | 拒绝 SCHEME\_MISMATCH |
| 自签证书 + system\_ca（反向对照） | 拒绝 CERTIFICATE\_VERIFY\_FAILED |
| 自签证书作为 custom\_ca 信任锚 | 200 |
| pinned（整证书 SHA-256） | 200 |
| pinned（SPKI 公钥 SHA-256） | 200 |
| pinned 指纹错误 | 拒绝 TLS\_PIN\_MISMATCH，**服务端收到请求数 0** |
| insecure，不校验主机名 | 200，并记录首次指纹 |
| insecure + 校验主机名（IP 在 SAN 内） | 200 |
| insecure + 校验主机名（主机名不在 SAN 内） | 拒绝 TLS\_HOSTNAME\_MISMATCH |
| insecure 指纹变化 | 200 并产生 TLS\_FINGERPRINT\_CHANGED 事件 |

未验证：生产规模压测、MCP 客户端兼容矩阵、OPA 性能，这些按 v0.4 里程碑在 M4 后完成。
