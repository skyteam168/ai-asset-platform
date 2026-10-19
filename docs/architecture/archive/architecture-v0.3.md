# 企业 AI 资产管理中台 架构设计 v0.3（完全自建版）

Oct 16, 2026 · @blue

## 0. 修订说明与结论速览

v0.3 只改一件事：平台自己拥有身份、组织、密级和授权，不读取知识库、Keycloak、企微、钉钉或任何旧系统的部门与权限结构。资产治理主线不变。

| 变更项 | v0.2 | v0.3 |
| --- | --- | --- |
| 身份源 | Keycloak 为唯一身份源，共享 realm 与 claim | 平台内置账号体系；OIDC 只是可选登录方式，只证明“你是谁”，不带组织或权限 |
| 组织树 | Keycloak group path，org\_unit 是只读缓存 | 平台自建 org\_unit 树，平台是唯一权威，管理员在控制台维护 |
| 密级 clearance | 来自 token claim | 平台内维护，按人设置，缺省取所属组织默认值，提升需安全岗审批 |
| 终端用户身份（OBO） | 透传 Keycloak 用户 JWT | 消费方用注册公钥签发短期“用户断言”，平台经绑定表映射到内部用户 |
| 知识库 | 共享身份域，下游用其自身权限判定 | 与 MES、ERP 一样是普通旧系统，经 Connector 接入，不共享任何身份 |
| corp\_identity 共享包 | 与知识库共用 | 取消，改为平台内部的 iam 模块 |
| 外部目录同步 | 每日对账 Keycloak | 取消实时同步，只提供管理员批量导入与管理 API |

**保留不变**

- Asset / AssetVersion / Release 三层模型、版本不可变、状态机与 G0 至 G8 门禁。
- 控制面与数据面分离、发布快照、统一执行链（认证 → PDP → 限流 → Runtime → Result Guard → 审计）。
- Tool 契约与执行语义（幂等、对账、补偿、错误分类）、Connector 体系、MCP / REST / Skill / Agent 暴露、发现模式。
- 总部集中治理，分公司只调用并在授权上限内下放。

**删除**：corp\_identity 共享包、org\_unit.kc\_group\_id、Keycloak 约定与组织同步、知识库共享身份域的三阶段演进、v0.2 第 11 节中关于 Keycloak 与知识库的待决事项。

**重写**：第 2 至 4 章（组织、身份、授权）、数据模型中的 IAM 表、里程碑 M0、Cursor 提示词 P1。

## 1. 背景、目标与原则

本平台是旧系统与各类 Agent 之间的“转换插头”加资产治理底座，部署在总部信息中心机房，是一个与现有任何系统都不共享账号、部门、权限和数据库的独立系统。

**背景**：集团有多个分公司与工厂，各自运行 MES、ERP、WMS、HR、OA 等系统，多数不支持 MCP，也不再投入开发。各分公司有自己的 AI 工作平台，总部也需要跨公司的 Agent。资产（Tool、Skill、Workflow Agent）由总部 IT 与开发集中创建和治理，分公司只调用，并在总部授予的上限内下放权限。

**目标**

- G1 统一资产：Tool、Skill、Workflow Agent 统一建模、版本化、发布、下线。
- G2 质量先行：测试与业务验收通过才能上架，上架后行为稳定、流程不变。
- G3 严格管控：发布要审核，开放要审批，使用要申请，按组织与密级隔离。
- G4 统一执行：内部调用与 MCP 对外暴露走同一条执行、权限、审计链路。
- G5 可观测可恢复：全链路追踪、风险检测、熔断、补偿与人工介入。
- G6 可扩展：新系统、新节点类型、新审批策略以插件方式接入，不改核心。
- G7 自主可控（新增）：身份、组织、密级、授权全部由本平台定义和维护，不依赖外部系统的组织与权限模型。

**非目标（首期）**：不做通用低代码平台；不替代分公司的 Agent 平台；不改造旧系统（确需加幂等键等小接口，走 ADR-0010 评审）；不做模型训练与托管；不做数据出境合规设计；不接管任何旧系统的用户与权限管理；不与外部组织架构做实时同步。

**约束**：Python 后端；PostgreSQL；平台部署在总部机房，经专线直连分公司机房里的系统；旧系统默认不改造；跨时区（中国、越南、柬埔寨）。

**非功能目标（初始建议值，需压测校准）**：网关可用性 ≥ 99.9%；平台自身附加延迟 P95 ≤ 100ms（不含下游）；审计零丢失；Gateway 与 Worker 无状态，可水平扩展。

**设计原则**

1. 平台是身份与权限的唯一权威：任何外部系统声称的组织、角色、密级都不被信任，也不被读取。
2. 控制面与数据面分离：治理故障不影响调用，治理变更不阻塞调用。
3. 已发布版本不可变：任何变更都发新版本并重新过门禁。
4. 流程由引擎强制执行，不靠提示词约束。
5. 默认拒绝，权限取多方交集。
6. 一切可审计、可追溯、可回放。
7. 面向接口扩展，核心保持小。
8. 旧系统是脆弱资源，必须被保护（限流、熔断、隔离）。

## 2. 自建组织与用户模型

平台用一棵自己维护的 org\_unit 树表达集团、分公司与部门，用户、角色、密级、数据范围都挂在这棵树上，所有编码由本平台定义，不引用任何外部系统的部门或员工编码。

### 2.1 组织树 org\_unit

| 字段 | 说明 |
| --- | --- |
| id | 内部 UUID，永不改变；授权、数据范围一律按 id 引用，不按名称或路径 |
| type | group（集团根，唯一）/ company（分公司或工厂）/ department / team |
| code | 平台内唯一的业务编码，由管理员命名，如 VN01、VN01-MFG；仅用于展示与接口，可改名 |
| parent\_id、path | 父节点与物化路径，用于子树查询；建议深度不超过 6 层 |
| default\_clearance | 该节点下成员的默认密级，缺省沿树向上继承 |
| company\_id | 所属 company 节点的 id，冗余存储，用于租户隔离与 RLS |
| status、valid\_from、valid\_to | 启用、停用、有效期 |

公司就是 type=company 的节点，company\_id 即它的 id。节点迁移、合并、停用都写领域事件与审计；授权按节点 id 记录，迁移后自动随子树生效，停用节点后其下授权失效。

### 2.2 用户 user

同一张 user 表覆盖两类人：能登录控制台的人（管理员、开发者、审批人、验收人），以及仅被消费方代表调用的员工（终端用户）。由 login\_enabled 区分，后者不需要密码。

- 基本字段：login\_name、display\_name、employee\_no（平台内可选唯一，由管理员录入，不对应任何外部系统）、status（active / disabled / left）、valid\_from、valid\_to。
- 归属：一个主归属节点 home\_org\_id，可有多个兼任（user\_org\_membership）。
- 创建方式只有两种：控制台手工创建，或批量导入（CSV、管理 API）。导入是“推送”，导入后平台数据为准，不回读、不对账、不覆盖已修改字段。
- 员工离职、调动由公司管理员在平台内操作；平台不感知外部 HR 变更，这是自建方案的有意取舍（见 16 章待决事项）。

### 2.3 数据范围 data\_scope

用户的有效数据范围 = 其所有归属节点的子树并集，再加管理员额外授予的节点。Tool 通过 scope\_binding 声明哪些入参对应组织维度（如 factory\_id 对应 company，dept\_id 对应 org 节点），PDP 在调用前校验入参值是否落在用户的数据范围内。

建议默认值：终端用户默认范围为其主归属部门子树；公司管理员可扩大到本公司；跨公司范围只有总部可授予。

### 2.4 密级 clearance

四级：L1 公开、L2 内部、L3 敏感、L4 机密，资产的 data\_classification 使用同一刻度。

- 用户有效密级 = 个人覆盖值 clearance\_override；没有则取最近祖先节点的 default\_clearance；都没有则为 L1。
- 提升密级必须走申请，由数据 Owner / 安全岗审批，带到期日，全程审计。分公司管理员不能修改密级。
- 消费方应用同样有密级上限 clearance\_cap，调用时有效密级取用户密级与应用上限的较小值。

### 2.5 角色

| 角色 | 所在 | 能做什么 |
| --- | --- | --- |
| 平台管理员 | 总部 | 全局策略、授权上限、组织树、熔断、平台运维 |
| 资产开发者 | 总部 | 创建与测试资产、维护 Connector；不能审批自己提交的资产 |
| 数据 Owner / 安全 | 总部 | 审核密级、开放范围、写操作风险；审批用户密级提升 |
| 业务 Owner | 总部或分公司 | 定义验收标准并签字验收（G5） |
| 分公司管理员 | 分公司 | 仅限本公司子树：维护部门与用户、绑定消费方用户、在授权上限内下放授权、到期复核 |
| 审计员 | 总部 | 只读审计与报表 |

角色分配记录在 user\_role（用户、角色、生效节点），分公司管理员的生效节点就是其公司节点，所有管理操作都按生效节点做范围检查。

### 2.6 租户隔离

一个平台实例对应一个集团。业务表带 company\_id 并启用 PostgreSQL RLS；跨公司可见性只通过 Release 的开放范围与 Grant 表达。总部资产的 company\_id 为总部公司节点。

## 3. 身份与认证

平台自己签发和校验全部凭据，识别三类主体：控制台用户、消费方应用、被消费方代表的终端用户。认证只回答“你是谁”，你能做什么永远由平台的授权数据决定。

| 主体 | 认证方式 | 要点 |
| --- | --- | --- |
| 控制台用户 | 用户名 + 密码 + TOTP（默认）；可选 OIDC / SAML 登录插件 | 会话令牌短期有效；外部登录只经 external\_login 表绑定到内部 user，不读取外部返回的组织、角色、密级 |
| 消费方应用 | API Token（Authorization: Bearer）；可选 mTLS 或 HMAC 签名 | 令牌哈希存储，仅创建时显示一次，带有效期、scope、可选来源 IP 绑定，可轮换、可吊销 |
| 终端用户（OBO） | 消费方在 X-User-Assertion 头带短期用户断言 | 断言是 JWT，由该消费方登记的公钥签名，平台验签后经绑定表映射为内部用户 |

### 3.1 用户断言

消费方的 AI 平台自己认识它的员工，平台不认识。解决办法是让消费方为每次调用声明“代表谁”，并用它登记的私钥签名，平台验签后再去绑定表查内部用户。

- 必填 claim：iss（消费方 client\_id）、sub（该员工在消费方侧的标识）、aud（本平台）、exp（有效期不超过 5 分钟）、jti（防重放，平台缓存已用 jti 至过期）。
- 公钥在消费方注册时登记，支持多把并行以便轮换；断言只走 HTTPS，不写日志。
- 绑定表 user\_binding（consumer\_id、external\_subject → user\_id）由分公司管理员维护，支持 CSV 批量导入。

绑定策略 bind\_policy 由总部按消费方设置：

| 策略 | 行为 |
| --- | --- |
| strict（默认） | 必须预先绑定，未绑定的 sub 一律拒绝 |
| by\_employee\_no | 用 sub 匹配本公司内的 employee\_no，需总部开启，适合消费方直接使用工号作标识 |
| app\_only | 不接受用户断言，所有调用以应用身份处理 |

不带断言的调用按应用身份处理：只能命中授予“消费方”的 Grant，数据范围限于消费方所属公司，有效密级为其 clearance\_cap。写操作与 danger 级别是否允许应用身份调用，由 Tool 的 identity 策略决定。

### 3.2 Principal

认证完成后，网关在服务端解析出统一的 Principal，后续 PDP 与审计都只认它：

```json
{
  "identity_kind": "user | app",
  "user_id": "u_123",
  "company_id": "c_VN01",
  "org_path": ["group", "VN01", "VN01-MFG"],
  "roles": ["business_owner"],
  "clearance": 2,
  "data_scope": ["org_vn01_mfg"],
  "client_id": "vn-ai-platform",
  "agent_profile_id": null
}
```

令牌里只放 sub、client\_id、过期时间等认证信息，不放组织、角色、密级，因此组织调整与密级变更立即生效，不存在“令牌里的权限滞后”的问题。

### 3.3 登录插件与服务间认证

- AuthProvider 是扩展点：local 为内置实现；OIDC、SAML 为可选插件，只负责验证身份并返回外部唯一标识，由 external\_login 映射到内部用户，找不到映射就拒绝登录。
- 控制面、数据面、Worker 之间使用 mTLS 加短期服务令牌；平台内部令牌的 audience 固定，不得转发给任何下游。
- 调用旧系统时由 Credential Proxy 从 Vault 取下游短期凭据，模型与 Tool 都看不到明文。
- 审计同时记录消费方 client\_id 与被代表的 end\_user\_id。

### 3.4 账号安全基线

密码哈希使用 argon2id；控制台管理员与审批人强制 TOTP；连续失败锁定并告警；会话令牌 30 分钟滑动过期；API Token 默认 90 天有效期；所有身份相关操作进入审计。

## 4. 授权模型

调用权限是五方交集，默认拒绝；管理权限与调用权限分开建模；五方判定所需的全部数据都来自本平台自己的表。

```
allow = Identity ∩ Grant ∩ ReleaseScope ∩ Classification ∩ RiskPolicy
```

| 方 | 判定内容 | 数据来源 |
| --- | --- | --- |
| Identity | 角色满足 required\_scopes；调用参数落在用户 data\_scope 内（scope\_binding） | user、user\_role、org\_unit |
| Grant | 消费方（或其 Agent Profile）对该资产有未过期授权，且落在所属公司授权上限之内 | grant、grant\_ceiling |
| ReleaseScope | 被调用版本在对应环境、渠道、开放范围内已发布且未 Suspended | release、release\_snapshot |
| Classification | 有效密级 ≥ 资产 data\_classification | user、org\_unit、consumer |
| RiskPolicy | write / danger 按 confirm\_policy 触发确认或审批 | Tool Spec、risk\_policy |

### 4.1 授权上限与下放

总部给每个分公司设授权上限 grant\_ceiling，分公司管理员只能在上限内下放，不能超出，也不能转授给其他公司。

- 授权上限 = 可用资产清单 + 最大密级 + 配额，由总部按公司设定。
- 下放的授权写入 grant 表，记录 grantor 与 ceiling\_id；PDP 校验“授权 ⊆ 上限”，总部收缩或撤销上限时，下游授权自动失效。
- 到期复核由分公司管理员执行，总部抽查，全部操作可审计。
- 分公司只能授予“可调用”；write 与 danger 是否需要确认或审批由 Tool 的 confirm\_policy 决定，分公司不能降低。

### 4.2 授权对象与两层判定

Grant 的对象（grantee）有四种：consumer、agent\_profile、org\_unit（含子树）、user。判定分两层：

1. **应用层**：必须存在授予该消费方或其 Agent Profile 的未过期 Grant，决定“这个应用能不能调这个资产”。
2. **用户层（可选收窄）**：若分公司管理员对该资产设置了授予 org\_unit 或 user 的 Grant，终端用户必须命中其中之一；没有设置则不收窄。

应用身份（无用户断言）只经应用层判定。

### 4.3 控制面 RBAC

管理权限按第 2.5 节角色收敛，所有控制面接口按“角色 + 生效节点”做范围检查。强制职责分离：开发者不能审批自己提交的资产，同一人不能同时担任同一资产的提交人与验收人；danger 级别资产的发布需双人复核。

### 4.4 PDP 部署与数据分发

- PDP 用 OPA，以 sidecar 方式部署在数据面，策略 rego 与表驱动测试一起进仓库。
- 身份与授权数据（用户摘要、组织树、绑定、Grant、上限）由控制面打成带版本号的 IAM 快照，经事件推送给数据面；数据面不直接读控制面的业务表。
- 控制面宕机时数据面按已有快照继续服务；快照缺失或损坏时 PDP 默认拒绝（fail closed）。
- 用户停用、授权撤销、上限收缩属于紧急变更，推送优先级最高，目标传播时间建议不超过 30 秒（需压测校准）。

PDP 输入示例：

```json
{
  "principal": {"identity_kind": "user", "user_id": "u1", "company_id": "c_VN01", "org_path": ["group", "VN01", "VN01-MFG"], "clearance": 2, "data_scope": ["org_vn01_mfg"], "client_id": "vn-ai-platform"},
  "resource": {"capability_id": "production.mes.query_work_orders", "version": "1.2.0", "classification": 2, "risk": "read", "release_scope": ["c_VN01"], "scope_binding": {"factory_id": "company"}},
  "request": {"args": {"factory_id": "c_VN01"}},
  "context": {"env": "prod", "channel": "stable"}
}
```

## 5. 总体架构

&#91;embedded content: 总体架构 · 接入层、控制面、数据面、连接层\]

控制面负责治理，数据面负责运行，二者只通过发布快照与 IAM 快照这一条单向事件通道协作；数据面不读控制面的业务表，控制面宕机时数据面按已有快照继续服务。

### 5.1 控制面与数据面的协作

每次发布生成 Release Snapshot（资产版本、Spec、依赖锁、开放范围、策略版本），身份与授权变更生成 IAM 快照，二者经事件推送给数据面并带递增 seq。数据面在本地缓存并校验 seq，发现缺口时主动拉取；快照缺失或损坏时按第 4.4 节默认拒绝。

### 5.2 模块职责

| 模块 | 面 | 职责 |
| --- | --- | --- |
| API / MCP Gateway | 接入 | 认证、解析 Principal、路由、按调用方动态暴露 MCP 工具、限流 |
| IAM | 控制 | 组织树、用户、角色、密级、消费方、用户绑定、授权上限；身份与授权数据的唯一写入方 |
| Registry | 控制 | 资产、版本、依赖、发布快照 |
| Importer | 控制 | 辅助起草：解析 OpenAPI / Postman，AI 生成候选描述；主路径是接入台账 |
| Test Lab | 控制 | 用例库、沙箱、回放、稳定性测试、评测 |
| Release & Approval | 控制 | 状态机、质量门禁、审批流、灰度、回滚 |
| Grant | 控制 | 开放范围、授权上限、使用申请、授权下放与到期复核 |
| Catalog / Skill Registry | 控制 | 目录发现、Skill 包与套件分发 |
| PDP | 数据 | OPA 统一策略决策 |
| Tool Runtime | 数据 | 校验、限流熔断、重试幂等、结果处理 |
| Workflow Engine | 数据 | 按 DSL 执行流程、人工任务、补偿 |
| LLM Gateway | 数据 | 统一模型调用、版本锁定、预算、脱敏 |
| Credential Proxy | 数据 | 从 Vault 取短期下游凭据 |
| Connector | 连接 | 对接旧系统，插件化 |
| Audit & Risk | 横切 | 审计、风险检测、告警、熔断 |

## 6. 资产模型

Tool、Skill、Workflow Agent 三类资产共用一套抽象：Asset → AssetVersion → Release，这一层与 v0.2 完全一致，只是归属与密级引用第 2 章的自建组织与密级。

- **Asset**：稳定标识、类型、归属组织节点、负责人、数据密级。
- **AssetVersion**：不可变，语义化版本号、内容哈希、规范文档（Spec）、依赖清单锁定。
- **Release**：某个版本在“环境 × 范围 × 渠道”上的发布实例。环境为 test / staging / prod；范围为部门 / 公司 / 集团 / 指定消费方（均按 org\_unit id 或 consumer id 引用）；渠道为 beta / stable。Release 另有 exposure（rest / mcp / a2a 的子集，默认只有 rest）。

### 6.1 Tool Spec

| 字段 | 说明 |
| --- | --- |
| capability\_id / domain | 全局唯一标识 {domain}.{system}.{action} 与业务域，如 production.mes.query\_work\_orders |
| name / description | 面向模型的名称与描述，是审核对象，直接影响选工具的准确率 |
| usage\_notes / not\_for | 用途、必需条件、返回内容；不适用场景 |
| input\_schema / output\_schema | JSON Schema，入参出参契约 |
| connector\_ref + 映射 | 绑定的 Connector，请求参数映射、响应裁剪、classify(response) 规则 |
| risk\_level | read / write / danger |
| idempotency | 是否幂等，幂等键生成规则 |
| timeout | connect / read / deadline 三层 |
| retry\_class、rate\_limit、breaker | 错误类别到重试策略的映射，限流与熔断 |
| reconcile / verify | 写操作结果未知时的只读对账方式；执行后确认业务结果的方式 |
| recovery、compensation\_ref | none / retry / compensate / manual；补偿 Tool（写操作必填，不可逆须标记） |
| identity\_mode | service\_account / user\_delegated / hybrid，见 9.3 |
| scope\_binding | 入参与组织节点或公司的映射，如 factory\_id 对应 company |
| data\_classification、required\_scopes | 数据密级、调用所需角色范围 |
| confirm\_policy | 自动 / 用户确认 / 审批 |

### 6.2 Skill Spec

指令正文、引用的 Tool 清单（版本锁定）、适用边界与示例、知识引用，以标准 Agent Skills 目录交付（SKILL.md 加可选 references/、assets/）。平台私有信息（Tool@版本、验收单编号、渠道）放在 metadata 或同目录 skill.lock.json。Skill 不直接执行，只指导 Agent 使用工具，所以验收重点是“指导效果”，用评测集衡量。默认只发指令型 Skill，不带脚本。

### 6.3 Workflow Agent Spec（DSL）

- 输入与输出 Schema、触发方式。
- 节点类型：tool（锁定版本）、llm（受限节点：固定模型、温度、提示模板版本、结构化输出，只产出数据，不能改变流程）、condition（基于数据的确定性分支）、parallel、loop（必须有上限）、human\_task（确认 / 审批 / 补录）、sub\_workflow、wait、end。
- 全局约束：总超时、预算（最大步数、LLM 调用数、token）、错误处理（重试 → 补偿 → 升级人工）。
- 依赖锁：所有 Tool、子流程、提示模板、模型版本全部固定。

“流程固定”的含义：节点图与分支由 DSL 决定，LLM 只在节点内部做理解、抽取、生成，无权决定下一步去哪里。DSL 示例：

```json
{
  "name": "purchase_order_review", "version": "1.0.0",
  "budget": {"max_steps": 30, "max_llm_calls": 3, "timeout_s": 600},
  "nodes": [
    {"id": "n1", "type": "tool", "ref": "erp.get_po@1.2.0", "next": "n2"},
    {"id": "n2", "type": "llm", "model": "pinned-v1", "temperature": 0,
     "prompt": "po_risk_extract@3", "output_schema": "RiskFlags", "next": "n3"},
    {"id": "n3", "type": "condition", "expr": "n2.risk_level == 'high'", "then": "n4", "else": "n5"},
    {"id": "n4", "type": "human_task", "kind": "approve", "assignee": "role:purchase_manager", "next": "n5"},
    {"id": "n5", "type": "tool", "ref": "erp.approve_po@1.0.1",
     "compensation": "erp.revoke_approval@1.0.0", "next": "end"}
  ]
}
```

静态校验：无环、可达、loop 有上限、预算有界、所有 ref 版本已锁定、写节点带补偿或标记不可逆。DSL 是内部格式，不对消费方暴露。

### 6.4 依赖图与影响分析

Workflow → Tool@版本，Skill → Tool@版本。某个 Tool 发新版本时，系统自动列出受影响的上层资产，并触发它们的回归测试。

## 7. 生命周期与质量门禁

&#91;embedded content: 资产状态机 · 8 个状态、两条回退边、一个熔断态\]

状态迁移只能由门禁结果驱动，非法迁移抛错；任何已发布状态在异常或风控触发时都可进入 Suspended，修复并复核后回到原状态。

### 7.1 质量门禁

| 门 | 内容 | 通过条件 | 执行 |
| --- | --- | --- | --- |
| G0 静态检查 | Schema 合法、依赖可解析、敏感字段识别、DSL 无环且可达、预算有界；Skill 用 skills-ref validate | 全部通过 | 自动 |
| G1 契约测试 | Tool 调用测试环境，校验入参出参与错误码映射 | 100% | 自动 |
| G2 功能测试 | Golden Set 用例逐条运行并断言 | 关键用例 100% | 自动 |
| G3 稳定性测试 | 同一用例重复运行 N 次（建议 ≥ 20），比对结果与执行路径 | 确定性节点 100% 一致，LLM 节点达评测阈值 | 自动 |
| G4 回归与影响 | 依赖变更触发上层资产全量回归 | 全部通过 | 自动 |
| G5 业务验收 | 业务 Owner 用真实业务样例验收并签署验收单 | 签字 | 人工 |
| G6 安全审核 | 密级、开放范围、写操作与补偿、身份模式与数据范围绑定 | 审批通过 | 人工 |
| G7 灰度 | 小范围运行，观察错误率、人工介入率、耗时 | 指标达标 | 自动 + 人工 |
| G8 全量 | 转 Active，设观察期与回滚预案 | 观察期无异常 | 自动 |

业务验收标准在 Draft 阶段就写入验收单模板：目标场景、准确率、任务完成率、耗时、人工介入率，由业务 Owner 确认。开发者不能审批自己提交的资产。

### 7.2 Test Lab

- **三种测试环境**：沙箱模式（Connector 指向各系统测试环境或 Mock，写操作只在沙箱执行，禁止测试流量触达生产写接口）；真实读模式（仅限生产只读接口，限流加脱敏，仅用于验收）；回放模式（用脱敏后的历史调用记录回放，后做）。
- **用例属于资产的一部分**，随版本管理；每条用例含输入、前置数据、断言（精确、Schema、语义评估）和期望执行路径。
- **权限用例**：用例可声明测试用的 Principal（公司、部门、密级、是否应用身份），用于验证“该拒绝的确实被拒绝”，属于 G2 关键用例。
- **LLM 节点评估**：固定模型版本与温度，使用结构化输出，断言优先；语义质量用规则加评估模型辅助打分，评估模型只作参考，最终以业务验收为准。
- **稳定性指标**：结果一致率、路径一致率、延迟分布、失败率。

### 7.3 如何保证“上架后流程不变”

1. 版本不可变加内容哈希，Release 只引用版本，不引用“最新”。
2. 引擎按 DSL 强制执行，运行时校验每一次节点转移是否合法。
3. 消费方绑定 stable 渠道或固定版本，新版本不会静默替换；升级需重新过门禁并发出通知。
4. 模型与提示模板版本锁在 DSL 中，更换视为新版本，重新测试。
5. 运行时漂移监测：线上抽样与验收基线比对，指标劣化时告警（首期只告警，自动回退后做）。

### 7.4 回滚与弃用

- Release 保留上一个稳定版本的指针，可一键回退；已在运行的执行实例钉住启动时的版本并跑完，新调用走回退后的版本。
- Deprecated 版本建议至少公告 30 天，目录标记并通知订阅方；仍有调用的版本不得直接 Retired，要等调用量归零，或由总部强制下线并记录原因。

## 8. 执行引擎与 Tool Runtime

分工是：Agent 决定目标与下一步，工作流引擎可靠推进多步流程，Tool Runtime 负责单步安全执行；重试预算由 Runtime 控制，Agent 不自行无限重试。本章所有身份判断统一使用第 3 章解析出的 Principal。

### 8.1 统一执行链

认证 → 解析 Principal → PDP → 限流与配额 → Tool Runtime → Result Guard → 审计。MCP 与 REST 两个出口共用这条链，自研 Agent OS 与控制台走 REST / SDK，不绕自己的 MCP。

### 8.2 统一执行结果与错误分类

```json
{
  "execution_id": "exec_20261016_001",
  "status": "succeeded | failed | unknown | pending_confirm | running | cancelled | compensated",
  "data": {},
  "error": {"code": "AUTH_TOKEN_EXPIRED", "message": "MES 认证已过期", "retryable": true},
  "next_action": {"type": "refresh_auth | fix_args | request_permission | wait | reconcile | ask_human", "message": "..."}
}
```

| 类别 | 重试 | 处理 |
| --- | --- | --- |
| 参数错误 | 否 | 返回字段级原因，修正后再调 |
| 下游认证过期（401） | 刷新凭据后最多 1 次 | Credential Manager 刷新 |
| 权限不足（PDP 拒绝或下游 403） | 否 | 拒绝并提示申请授权 |
| 限流（429） | 有条件 | 遵守 Retry-After，退避 |
| 临时故障（5xx、连接失败） | 读可重试；写仅幂等时 | 指数退避，受总预算限制 |
| 业务规则冲突 | 通常否 | 返回业务原因，由 Agent 重新决策 |
| 写操作结果未知 | 不直接重试 | 进入 unknown，对账 |
| 熔断或维护窗口 | 否 | 返回明确错误与预计恢复时间 |

### 8.3 超时、幂等与对账

- 超时分三层：连接超时、读取超时、总期限（deadline 随调用上下文向下传递），数值按系统 SLA 配置，支持取消；执行超时必须小于调用方超时。
- 写类 Tool 必须带幂等键：调用方提供，或由 Runtime 按 task / step 派生。唯一约束放在 PostgreSQL（消费方 + capability + 幂等键），Redis 锁只做优化；同键不同参数返回冲突。
- 写操作超时或连接中断时状态记为 unknown，不自动重放。Runtime 按 Tool 声明的 reconcile（用幂等键或业务单号做只读查询）确认：已成功则补记结果；确认未执行则按幂等策略重试；仍不确定则保持待确认并升级人工。
- 请求成功不等于业务成功：Connector 的 classify(response) 把“HTTP 200 但业务码失败”归为失败；Tool 可声明 verify。
- 每次执行写入 tool\_execution，输入只存脱敏快照或加密引用。

### 8.4 恢复层次

重试 → 补偿（用新的业务操作抵消，如取消、冲销、撤回，不是删除）→ 人工恢复。单系统内的事务交给系统自己，平台不绕过业务逻辑去写库；跨系统用 Workflow Saga。

### 8.5 Workflow Engine

- 持久化执行：workflow\_run、step\_run（step journal）存 PostgreSQL；Worker 用 FOR UPDATE SKIP LOCKED 拉取步骤；每步带幂等键，崩溃后按 journal 恢复。
- 预计 30 秒内的同步返回，其余返回 run\_id，通过轮询、回调或 MCP progress 获取结果。
- human\_task 持久化，支持确认、审批、补录，带超时与升级；通知渠道用 Notifier 插件（邮件加一个 IM）。
- Saga：写步骤声明 compensation\_ref，失败后逆序补偿；不可逆步骤必须前置人工确认。
- Run 启动时钉住 Release 快照，运行中不受新版本影响。
- 首期自研轻量引擎，接口抽象为 WorkflowExecutor，后续可替换为 Temporal。

### 8.6 LLM Gateway 与 Result Guard

- 所有 llm 节点与导入生成都经统一网关：固定模型版本与温度、结构化输出校验、token 预算、输入脱敏、全量审计。模型或提示模板变更等于新资产版本。
- Result Guard：按 output\_schema 裁剪字段、按密级脱敏、按 scope\_binding 过滤超范围数据、限制大小、标记为“不可信数据”后再交给模型（防提示注入）；错误信息统一映射，不泄露旧系统内部细节。

## 9. Connector 与旧系统接入

所有 Connector 部署在总部机房，经专线直连各分公司机房的系统；每个旧系统（包括知识库）都只是一个被保护的下游，平台不读取它的用户、部门或权限结构，也不要求它做任何改动。

### 9.1 连接器类型

| 类型 | 适用 | 说明 |
| --- | --- | --- |
| REST / SDK 连接器 | 有正式 API 的系统 | 首选，开发者按 API 文档写参数映射与错误码映射 |
| 数据库只读连接器 | 无 API 的读需求 | 只读副本或视图，固定参数化查询，不开放任意 SQL |
| 上游 MCP 连接器 | 导入第三方或已有 MCP 的能力 | 视为不可信来源：描述重新审核，固定版本与内容哈希，上游变更视为新版本 |
| 浏览器自动化（RPA） | 只有网页、没有 API | 最后手段，首期不做 |

写操作一律走 API 或存储过程，不直接写业务库；需要补偿的写操作，开发者同时交付补偿逻辑并在沙箱验证。每个 Connector 附“接入台账”：API 文档、库表说明、测试环境、负责人、已知限制，作为 G1 契约测试与评审依据。

### 9.2 旧系统保护与连接配置

- 按 Connector 实例设并发上限、令牌桶、熔断、短 TTL 读缓存、写操作串行队列。
- 专线抖动由超时、熔断与读缓存兜底，并按“公司 × 系统”监测连通性。
- system\_connection 记录：系统与环境、连接方式、认证模式、凭据引用（Vault）、Token 策略、超时与并发、健康检查、维护窗口、负责人。
- Credential Manager：不每次调用都登录；Token 缓存并续期；刷新用 single-flight；遇 401 刷新后最多重试一次；无法自动刷新则告警转人工；Token 永不进入模型上下文与日志。

| 老系统认证方式 | 处理 |
| --- | --- |
| 固定 API Key | Vault 存放，调用时注入 |
| Bearer Token | 缓存并按有效期续期 |
| OAuth 2.0 | 按授权模式获取与刷新，校验 scope |
| 用户名密码换 Token | 后端登录，按系统机制续期 |
| Cookie / Session | 专用会话管理器，处理过期与重新登录 |
| 验证码、动态验证 | 要求系统方提供服务账号或正式 API |

### 9.3 身份模式与下游账号映射

连接账号能查到，不等于当前用户有权看到。平台自己判定“谁能调”，下游系统只负责执行。

| 模式 | 适用 | 说明 |
| --- | --- | --- |
| service\_account | 后台自动化、低风险读（默认） | 系统方建专用最小权限账号；平台侧仍按第 4 章校验用户与数据范围 |
| user\_delegated | 个人数据、审批、高风险写 | 以用户身份调用目标系统，要求目标系统支持 |
| hybrid | 读用服务账号，写用用户身份 | 两者结合 |

user\_delegated 需要平台知道“这个用户在下游系统里是谁”，由 system\_account\_binding（user\_id、system\_connection\_id、external\_account）维护，由总部或公司管理员录入。涉及个人敏感数据或 danger 级别的 Tool，不得仅凭服务账号放行，必须叠加用户授权或审批；目标系统不支持用户委托时，用服务账号加更严格的审批与审计。

### 9.4 知识库作为普通系统接入

知识库按普通旧系统处理：写 kb-connector，调用它现有的开放接口，暴露为 kb.search、kb.get\_document 等 read 类 Tool，不共享身份，也不要求它改造。

- **可见范围由服务账号决定**：知识库只认它自己的 API Key 及其绑定的访问范围，平台无法按终端用户收窄它返回的文档。因此 kb Tool 的 identity\_mode 固定为 service\_account，数据 Owner 评审该账号的可见范围，并按其可见内容的最高密级定级。
- **按公司隔离**：每个分公司的消费方使用独立的知识库账号，范围只覆盖该公司应当开放的内容，不使用跨公司或根部门范围的账号。
- **平台侧再收一道**：Grant、数据范围、密级和 Result Guard 仍然生效，平台授权不通过的调用到不了知识库。
- 若将来需要按人过滤，与知识库团队另行约定，不属于本项目范围。

### 9.5 Connector SDK 契约（Python）

```python
class Connector(Protocol):
    name: str
    def capabilities(self) -> list[OperationSpec]: ...
    async def execute(self, op: str, params: dict, ctx: ExecContext) -> ConnectorResult: ...
    async def health(self) -> HealthStatus: ...
    def map_error(self, exc: Exception) -> PlatformError: ...
    def classify(self, response) -> Outcome: ...            # HTTP 200 但业务码失败 → 失败
    async def reconcile(self, op, idempotency_key): ...     # 可选，对账查询

# ExecContext: principal, trace_id, idempotency_key, deadline, credential(短期), mode(sandbox|live_read|replay)
```

沙箱模式下 live 写操作必须在 Connector 层硬拒绝；所有出站请求携带 trace\_id 与幂等键。

### 9.6 内部自签证书与 TLS 信任

旧系统既有 HTTPS（含内部 CA 与自签证书），也有只支持 HTTP 的，平台两种都能接入：下游按连接配置传输方式与 TLS 策略，允许明文 HTTP 和关闭证书校验；对外则统一只提供 HTTPS。放开不等于不管，每个例外都要审批、限定范围、带到期日并全程审计。

**策略挂在连接上，不挂在 Tool 上。** Tool 版本不可变且跨环境复用，证书属于部署环境：同一个 Tool 在测试与生产连的是不同系统，证书也不同。所以 TLS 配置放在 system\_connection（Connector 下游）和 upstream\_mcp\_source（上游 MCP）上，Tool 通过 connector\_ref 继承，Tool Spec 里没有 TLS 字段。

| 传输与 tls\_mode | 做法 | 适用 |
| --- | --- | --- |
| https + system\_ca（默认） | 用系统信任库校验 | 证书由公共 CA 签发 |
| https + custom\_ca | 信任管理员上传的内部根 CA，一份 CA 可被多个系统复用 | 内部 CA 签发的证书，首选 |
| https + pinned | 固定服务端证书或公钥的 SHA-256 指纹，只信任这一张 | 单个自签证书，推荐；证书轮换时更新指纹 |
| https + insecure | 不校验证书链与有效期，可选是否仍校验主机名 | 无法拿到 CA 或指纹的自签证书系统 |
| http | 明文 HTTP，不做 TLS | 只支持 HTTP 的旧系统 |

推荐顺序是 custom\_ca、pinned、insecure、http。自签证书用 pinned 同样能连通，而且仍能防中间人，只是首次要登记指纹；http 只用于确实不支持 TLS 的系统，能升级为 HTTPS 的由系统方排期升级。

**insecure 与 http 的护栏**

- 只能对单个 system\_connection 或 upstream\_mcp\_source 实例开启，不能全局开启，也不能按 Tool 开启。
- 必须填写理由，由数据 Owner / 安全审批，带到期日（默认 90 天，到期前 14 天提醒，到期未复审则禁用该连接并告警）。
- 目标地址必须在内网网段白名单内，公网地址不允许 insecure 与 http。
- 允许绑定 write 与 danger 级别 Tool，但视为高风险例外：G6 必须明确标注“传输不安全”，需数据 Owner 与安全双人复核，并建议在专线或网段上做访问控制，只放行平台出站地址。
- 凭据会在 http 与 insecure 连接上暴露给中间人，因此这类连接只使用该系统专用的最小权限服务账号（identity\_mode 限定为 service\_account），不使用个人账号，也不用 user\_delegated。
- https + insecure 首次连接记录服务端证书指纹，之后指纹变化触发告警。
- 控制台在该连接上显示“不校验证书”或“明文 HTTP”标记；每次出站日志带 transport 与 tls\_verified 字段；所有变更写审计。
- 沙箱与 staging 连接可由开发者申请；生产连接需双人复核。

**实现约束**：Connector SDK 提供统一的 HTTP 客户端工厂，按连接的传输方式与 TLS 策略构造客户端；所有 Connector 必须通过它发起出站请求，CI 用 lint 规则禁止代码里直接写 verify=False 或等价的关闭校验写法。Connector 配置里的 base\_url 协议必须与连接设置一致，不一致则拒绝；不跟随 https 到 http 的重定向，防止被悄悄降级。

**连接测试页**：控制台的“连接测试”展示证书链、颁发者、有效期、指纹、主机名是否匹配和失败原因，并提供“信任此 CA”和“固定此证书”按钮，生成 custom\_ca 或 pinned 配置草稿，经审批后生效。图中“The SSL connection could not be established”这类错误，会在这里直接看到是证书不受信任、已过期还是主机名不符。

**对外统一 HTTPS**：平台的 MCP、REST 和控制台只监听 HTTPS（TLS 1.2 及以上，建议 1.3），不开放 HTTP 端口，也不做 http 到 https 的跳转，避免令牌第一次以明文发出；开启 HSTS。对外证书建议由集团内部 CA 签发，分公司平台连接时若报证书不受信任，应让客户端安装根 CA，而不是在客户端关闭校验；控制台提供根 CA 下载、指纹和各常见客户端的配置说明。旧系统用 HTTP 只发生在平台出站方向，不影响对外。

## 10. 对外暴露与分发

对外只用公开标准，不造私有协议：Tool 用 MCP，Skill 用 Agent Skills 目录，Workflow Agent 默认是 MCP 异步 Tool，A2A 排第二批。执行留在平台，只有 Skill 会安装到分公司平台。

| 资产 | 执行位置 | 分公司怎么用 |
| --- | --- | --- |
| Tool | 平台 | 配置 MCP 地址，自动发现并调用 |
| Skill | 分公司平台自己的模型 | 按版本安装或同步，纯指令，不含凭据 |
| Workflow Agent | 平台 | 作为异步 Tool 调用，或挂载为远程 Agent |

### 10.1 MCP 接入

- 端点 https://mcp.<域名>/mcp，Streamable HTTP；认证用 HTTPS + Bearer Token（第 3 章），用户身份用 X-User-Assertion。网关按 MCP-Protocol-Version 协商版本，至少两个季度兼容上一个规范版本，规范升级前先跑兼容测试。
- tools/list 按 Principal 动态过滤，不同分公司连同一个地址，看到的工具不同；命名 {domain}.{system}.{action}，大版本变化时另起名称。
- 套件（bundle）：按场景打包，端点 /mcp/bundles/{name}，单个套件建议不超过 30 个工具，套件本身是版本化资产并走发布门禁。
- 工具注解（只读、破坏性）由 risk\_level 自动生成，尽量返回结构化输出；Agent 异步 Tool 始终配 get\_run\_status 兜底工具。
- 申请流程：分公司管理员在控制台申请消费方，总部审批后生成 client\_id 与 API Token（存 Vault，仅创建时显示一次，支持轮换），并登记用户断言公钥。
- 兼容性测试：官方 MCP Inspector 加至少 3 个主流客户端冒烟测试进 CI，维护客户端兼容矩阵 docs/ops/client-compat.md。

### 10.2 暴露策略 exposure

Tool 决定平台有什么能力，exposure 决定从哪个出口可用。

| 风险级别 | 默认出口 | 放开条件 |
| --- | --- | --- |
| read | rest | 通过 G6 后可开 mcp |
| write | rest | 默认不对 MCP 开放，需数据 Owner 明确批准 |
| danger | 仅内部工作流 | 默认不对外，需双人复核 |

### 10.3 能力发现

| 模式 | 做法 | 适用 |
| --- | --- | --- |
| 直连套件 | 端点按套件暴露具体 Tool，每个工具有独立注解 | 工具集稳定，客户端可逐个审批 |
| 发现模式 | 只暴露 search\_capabilities、get\_capability、invoke\_capability 三个元工具 | 能力很多、客户端会把全部工具塞进上下文 |

发现模式下客户端无法逐个批准，所以 danger 级别不进入发现模式；检索先按 Principal 过滤再检索，首期用 PostgreSQL 全文检索，之后按需加 pgvector。同一业务能力不论来自原生 Connector 还是上游 MCP，目录只保留一个主条目，来源优先级为原生 > 上游 MCP。

### 10.4 Skill 分发

- Skill 包 = 标准 Agent Skills 目录，带 sha256 与签名，分 stable、beta 渠道；G0 加入 skills-ref validate。
- 命令行 corp-skill：login、search、install name@ver、update、verify、list；安装时校验该消费方是否已获得所引用 Tool 的授权，缺失则提示去申请。--target 的目录由适配表配置，不写死。
- 同时提供 Git 仓库与 zip 下载两种不用 CLI 的渠道。
- verify 比对哈希，本地改过的 Skill 标记为“本地修改”，不享受平台质量保证。Skill 被 Suspended 或 Retired 时，CLI 与 API 返回撤回标记（尽力而为，平台无法强制远端停用）。
- 带脚本的 Skill 需额外安全审查、签名并进入白名单。

### 10.5 Agent、Profile 与 SDK

- Agent 默认是异步 Tool agent.{name}：返回 run\_id，结果经 MCP progress、GET /runs/{id} 或 webhook 获取；人工任务在平台待办页处理，并经 Notifier 回调。
- 同一消费方下可有生产、采购、财务等多个 Agent：agent\_profile 声明所属业务域，Grant 可授予 Profile；薪资类高权限能力只授予对应 Profile。
- 官方 Python 与 TypeScript SDK 封装 REST（目录、调用、异步状态）；Python SDK 带 LangGraph / LangChain 适配器，把目录动态注册为框架工具。
- GET /catalog 按 Principal 过滤；catalog.changed 事件经带签名、可重试的 webhook 通知新版本、废弃与撤回。

### 10.6 标准跟进

每季度巡检 MCP、Agent Skills、A2A 的版本变化，先跑兼容测试再灰度升级；标准变化快，不默认向后兼容。

## 11. 核心数据模型

数据模型分为身份与组织（本版新增与重写）、资产与发布、执行与审计三组；所有业务表含 company\_id 与 created\_at（UTC），启用 RLS，表与字段在第一阶段按最终形态建好，功能分批开启。

### 11.1 身份与组织（iam）

| 表 | 关键字段 |
| --- | --- |
| org\_unit | id, type, code, parent\_id, path, company\_id, default\_clearance, status, valid\_from, valid\_to |
| user | id, login\_name, display\_name, employee\_no, home\_org\_id, login\_enabled, password\_hash, totp\_secret\_ref, status, clearance\_override, clearance\_expires\_at |
| user\_org\_membership | user\_id, org\_id, kind（primary / secondary） |
| user\_role | user\_id, role, scope\_org\_id（生效节点）, granted\_by, expires\_at |
| user\_data\_scope | user\_id, org\_id（额外授予的数据范围节点）, granted\_by, expires\_at |
| clearance\_request | id, user\_id, requested\_level, reason, status, approver, expires\_at |
| external\_login | provider, external\_id, user\_id（可选登录插件的绑定） |
| consumer | id, client\_id, company\_id, clearance\_cap, bind\_policy, quota, mcp\_allowlist, status, contact |
| consumer\_credential | consumer\_id, token\_hash, scope, expires\_at, ip\_binding, status |
| consumer\_public\_key | consumer\_id, key\_id, public\_key, status, expires\_at（验签用户断言） |
| user\_binding | consumer\_id, external\_subject, user\_id, created\_by（唯一：consumer + external\_subject） |
| system\_account\_binding | user\_id, system\_connection\_id, external\_account |
| agent\_profile | id, consumer\_id, name, domain\_ids, roles |
| iam\_snapshot | seq, payload（增量或全量）, created\_at（事件推送给数据面） |

### 11.2 资产与发布

| 表 | 关键字段 |
| --- | --- |
| asset | id, type（tool / skill / workflow）, name, owner\_org\_id, owner\_user\_id, classification |
| asset\_version | id, asset\_id, semver, content\_hash, spec（jsonb）, dep\_lock（jsonb）, state；发布后不可变（触发器强制） |
| release | id, version\_id, env, scope（jsonb）, channel, exposure, status, prev\_release\_id |
| release\_snapshot | id, release\_id, payload（jsonb）, policy\_version, seq |
| test\_case / test\_run / test\_result | 用例属于资产版本；运行含环境、模型版本、路径、断言结果 |
| gate\_result | version\_id, gate（G0 至 G8）, status, evidence, operator |
| approval / acceptance\_sheet | 审批流实例、验收单模板与签字 |
| grant | id, consumer\_id 或 grantee（type + id）, asset\_id, scope, expires\_at, reviewer, grantor, ceiling\_id, agent\_profile\_id（可空） |
| grant\_ceiling | company\_id, asset\_set, max\_classification, quota |
| bundle / bundle\_item / skill\_package / subscription / webhook\_endpoint | 套件、Skill 包、订阅与回调 |
| capability\_domain / capability\_index / upstream\_mcp\_source | 业务域树、检索索引、上游 MCP 来源 |

### 11.3 执行与审计

| 表 | 关键字段 |
| --- | --- |
| connector / connector\_instance / system\_connection / credential\_session | 连接器类型、公司与系统、地址、并发与限流、凭据引用、会话元数据（不含明文） |
| tool\_execution | id, capability\_id, version, consumer\_id, end\_user\_id, agent\_id, idempotency\_key（唯一：consumer + capability + key）, status（含 unknown）, input\_ref, result\_ref, error\_code, retry\_count, deadline, reconcile\_status |
| workflow\_run / step\_run / human\_task | 钉住的 release\_id、状态、幂等键、journal、补偿记录 |
| audit\_log | 按月分区，prev\_hash, hash；记录谁（consumer + end\_user）、什么版本、参数摘要、结果摘要、决策原因 |
| risk\_event / usage\_meter | 风险事件、计量聚合 |

约定：数据库迁移先加后删；已发布版本不可变；审计表独立权限，仅追加。

TLS 信任相关：新增 tls\_trust\_profile（id、name、kind（ca\_bundle / pin）、pem、fingerprint\_sha256、expires\_at、owner、status）；system\_connection 与 upstream\_mcp\_source 增加 scheme（https / http）、tls\_mode、tls\_profile\_id、tls\_check\_hostname、transport\_exception\_reason、transport\_exception\_expires\_at、transport\_approved\_by、tls\_last\_seen\_fingerprint。

## 12. 部署、可靠性与安全

平台部署在总部信息中心机房，控制面与数据面分命名空间、分资源池；控制面故障不影响调用，身份与授权故障一律默认拒绝。

### 12.1 部署

- Kubernetes（或同等容器编排）；Gateway 与 Worker 无状态，至少 2 副本，有条件时跨机架或双机房。
- PostgreSQL 主从并自动故障切换，开启 PITR；审计库使用独立实例或独立 schema 与独立权限。Redis 只做缓存和限流，不存唯一数据。对象存储存测试数据、Skill 包和验收单附件。
- Vault 高可用；OPA 以 sidecar 部署，策略与数据包由控制面分发并带版本号。
- 环境分 dev、test、staging、prod 四套；Test Lab 沙箱与生产网络隔离，沙箱 Connector 只能连测试环境。
- 统一入口：同一域名，WAF 与限流保护，MCP 与 REST 走同一个 Gateway；平台自身蓝绿或滚动发布。

对外入口只提供 HTTPS（TLS 1.2 及以上），不监听 HTTP 端口；与旧系统之间允许 HTTP 或关闭证书校验，只发生在出站方向，规则见 9.6。

### 12.2 可靠性与降级

| 故障 | 行为 |
| --- | --- |
| 控制面宕机 | 数据面按本地快照继续服务，发布、审批、用户与授权变更暂停 |
| OPA 不可用或 IAM 快照缺失 | 默认拒绝（fail closed） |
| Redis 不可用 | 退化为本地限流，告警 |
| Vault 不可用 | 已缓存的短期凭据到期前继续；到期后写操作拒绝，读操作按策略处理 |
| 旧系统不可用或维护 | Connector 熔断并返回明确错误，维护窗口提前登记 |
| 审计库不可用 | 写操作拒绝，读操作进入本地缓冲并告警（审计零丢失优先） |

建议值（需业务确认）：业务库 RPO ≤ 5 分钟，RTO ≤ 30 分钟；审计同步写入并有副本。

### 12.3 安全威胁与对策

| 威胁 | 对策 |
| --- | --- |
| 提示注入：旧系统返回内容含恶意指令 | Result Guard 裁剪并标记不可信；LLM 节点只产出数据，不能改流程 |
| 工具描述投毒 | 描述是审核对象，改描述等于新版本，重新过门禁 |
| 越权与混淆代理 | 用户断言验签、五方交集、令牌 audience 校验、令牌不转发下游 |
| 伪造或重放用户断言 | 消费方私钥签名、有效期 ≤ 5 分钟、jti 防重放、公钥可轮换 |
| 管理员滥用身份权限 | 密级提升需安全岗审批；分公司管理员只能在本公司子树内操作；身份变更全审计，职责分离 |
| 凭据泄露 | 短期凭据、日志脱敏、密钥轮换、令牌仅显示一次 |
| 恶意或被篡改的 Skill | 默认只发指令型、签名与哈希校验、脚本额外审查并走白名单 |
| 打爆旧系统 | 配额、令牌桶、熔断、读缓存、写操作串行 |
| 重放与重复写 | 幂等键；webhook 带签名、时间戳与 nonce |
| 审计篡改 | 仅追加、哈希链、独立权限 |
| 内部人员作恶 | 职责分离；danger 级别操作需双人复核 |
| LLM 输出异常 | 结构化输出校验、预算上限、降级到人工 |

### 12.4 可观测、审计与风控

- OpenTelemetry：trace\_id 从 Gateway 贯穿到 Connector 与下游日志，并支持从上游透传。
- 指标：调用量、错误率、P50 / P95 / P99（区分平台自身与下游）、熔断次数、人工介入率、Run 成功率与补偿率、漂移指标、Skill 版本分布、各公司配额使用率；告警分级并明确值班人。
- 风险规则：异常频率、批量写、越权尝试、非工作时段、密级异常、用户断言验签失败激增、漂移指标劣化，触发告警或自动 Suspended。
- 数据保留（建议值）：审计在线 1 年后归档；Run 明细保留 90 天；聚合指标长期保留。

### 12.5 运维

手册覆盖熔断与 Suspended 处置、一键回滚、旧系统维护窗口登记。旧系统升级前通知，自动触发相关 Tool 回归测试。每季度演练：控制面宕机、PostgreSQL 切换、Vault 不可用、批量越权告警、断言密钥泄露应急轮换。

## 13. 工程结构与技术选型

首期做模块化单体加独立数据面进程，不做微服务；身份组织权限作为控制面内的 iam 模块自建，不引入外部身份服务。

**选型**：Python 3.12、FastAPI、SQLAlchemy 2.0（async）、Alembic、Pydantic v2、PostgreSQL、Redis、MinIO / S3、Vault、OPA、OpenTelemetry、官方 MCP Python SDK、argon2-cffi、pytest、ruff、mypy --strict；前端 React + TypeScript。Keycloak 不再是必选组件，只在启用 OIDC 登录插件时才对接。

```
platform/
  libs/                      # 无业务依赖的小包：tokens（令牌与断言验签）、common
  apps/control_plane/        # iam, registry, importer, testlab, release, approval, grant, console_bff
  apps/data_plane/           # gateway(mcp+rest), principal_resolver, pdp_client, runtime, workflow, credential_proxy, result_guard
  apps/workers/              # workflow worker, testlab worker, connector worker
  connectors/                # mock, kb, mes_*, erp_*, ...
  policies/                  # OPA rego + 表驱动测试
  migrations/  deploy/  docs/
```

**约束**

- 模块间只通过 service 接口与领域事件通信，禁止跨模块读表；数据面只读快照与自己的运行表。
- iam 模块是身份、组织、授权数据的唯一写入方；其他模块只能读 IAM 快照或调用 iam 的 service。
- asset\_version 一旦发布不可变：数据库触发器加应用层双重保护。
- 所有调用必须经 PolicyService.decide()，默认拒绝，决策原因写入审计。
- 日志与审计禁止出现明文凭据与未脱敏业务数据。

**扩展点**（新增不改核心，用接口加注册表实现）：新连接器类型、新认证模式、新登录插件（AuthProvider）、新节点类型、新门禁插件、新评测器、新通知渠道、新出口、新策略维度。

**稳定契约**（改动必须走 ADR 与主版本）：三层资产模型与状态机；Principal 与 PDP 输入；Tool Spec 字段集与 Workflow DSL 节点类型；统一执行结果与错误码；Connector 接口；对外出口（MCP、REST、A2A）；事件契约 `release.*`、`grant.*`、`policy.*`、`iam.*`、`catalog.*`；迁移规则。契约只增不改，加字段、加枚举值必须向后兼容。

## 14. 里程碑、首期范围与验收

&#91;embedded content: 里程碑路线图 · 8 个阶段、每阶段一个验收门\]

里程碑是交付顺序，不是设计取舍：所有表、字段、接口的最终形态在第一阶段就按本文档建好（例如 exposure、identity\_mode、scope\_binding、agent\_profile，首期只用默认值也要建好），功能分批开启，避免后期改表。路线图相比 v0.2 的变化是：IAM 从“对接 Keycloak”变成独立的 M1，首期从 M0 至 M3 扩展到 M0 至 M4。

### 14.1 范围

| 范围 | 内容 |
| --- | --- |
| 首期（M0 至 M4） | 自建 IAM（组织、用户、密级、消费方、用户断言、绑定）；Registry、版本不可变、快照；Connector SDK、kb-connector 与 1 到 2 个读类系统；Tool Runtime、PDP、审计；MCP / REST Gateway；状态机与 G0 至 G2、G5 签字、G6 审批；授权上限与下放；控制台最小集（资产目录、组织与用户、消费方与绑定、审批待办、授权、审计检索）；目录 API；统一执行结果、错误分类与幂等；数据范围校验；目录全文检索 |
| 第二批（M5 至 M7） | 写类 Tool 与补偿；Workflow 引擎与人工任务；G3 稳定性测试；Skill Registry 与 corp-skill；套件；配额与计量；webhook；发现模式元工具与官方 SDK；上游 MCP 连接器；Agent Profile；灰度与回滚；风控 |
| 后做或按需 | A2A；漂移监测的自动回退（先只告警）；OIDC / SAML 登录插件与 OAuth 2.1 + PKCE；双机房；资产市场页；Test Lab 回放模式；AI 生成 Importer；成本分摄；pgvector 语义检索；浏览器自动化（RPA）连接器 |
| 不做 | 低代码流程画布（DSL 用 JSON / YAML 编辑加校验）；自研 LLM 评估平台；多区域数据面；Temporal；微服务拆分；与外部组织架构的实时同步 |

### 14.2 测试策略与种子数据

- 测试：单元测试、契约测试（对 OpenAPI 与 Connector）、集成测试（testcontainers 起 PostgreSQL、Redis、OPA）、少量端到端烟雾。PDP 策略用表驱动测试；不追求覆盖率数字，关键路径（权限、用户断言验签、版本不可变、补偿、审计）必须有测试。
- 种子数据（seed-data.md）：首个平台管理员、示例公司与部门、一个 Mock 系统、一个 kb Tool、一个带用户断言的示例消费方、一个完整走完门禁的示例资产，便于联调与演示。
- 非功能指标（可用性 99.9%、附加延迟 P95 100ms）在 M3 后压测校准。

## 15. Cursor 开发指南

开发按 P0a 到 P13 的顺序推进，每次只发一个提示词，让 Cursor 先给计划再写代码，跑通测试再进入下一步；每个提示词都引用本文档章节，避免 Cursor 自行发挥设计。

### 15.1 使用方式

1. 新建独立仓库，按第 16 章的目录清单建好 docs/，把本文档存为 docs/architecture.md，把 15.2 存为 .cursor/rules/platform.mdc（alwaysApply）。
2. 先执行 P0a，再按 P0 至 P13 顺序推进。
3. 设计冲突先向我提问，不允许 Cursor 自行改设计；身份、授权相关改动必须同步更新 docs/architecture.md 与对应 ADR。

### 15.2 项目规则（.cursor/rules）

```
- 以 docs/architecture.md 为唯一设计依据；设计冲突先提问，不自行改设计。
- Python 3.12, FastAPI, SQLAlchemy 2.0 async, Alembic, Pydantic v2, pytest, ruff, mypy --strict。
- 模块化单体：模块间仅经 service 接口与领域事件，禁止跨模块直接读写他人表。
- 身份、组织、密级、授权数据只由 iam 模块写入；不读取、不同步、不信任任何外部系统的部门、角色、权限或 token 中的组织声明。
- asset_version 一旦发布不可变：数据库触发器 + 应用层双重保护。
- 所有业务表含 company_id、created_at(UTC)，启用 RLS。
- 所有调用必须经 PolicyService.decide()，默认拒绝，决策原因写入审计。
- 日志与审计禁止出现明文凭据、用户断言与未脱敏业务数据。
- 沙箱模式下 Connector 必须硬拒绝 live 写操作。
- 每次变更：先输出计划 → 迁移脚本 → 代码 → 单测/集成测试 → OpenAPI 更新。
```

### 15.3 分阶段提示词

| 阶段 | 提示词 | 验收 |
| --- | --- | --- |
| P0a docs 初始化 | 创建 docs/ 目录（见第 16 章）：放入 architecture.md（内容由我提供，不要改写）；创建 ADR 模板与 0001 至 0016 初始 ADR（状态照第 16 章表）；创建控制面与数据面两份 OpenAPI 骨架并按本文档第 3、4、10 章补全；写 api/conventions.md（RFC 9457 错误、游标分页、Idempotency-Key、UTC 时间）；从第 6、4 章生成 tool-spec、workflow-dsl、policy-input 三份 JSON Schema。CI 加 spectral lint、openapi-diff、JSON Schema 校验、链接检查。不要写业务代码。 | CI 全绿；openapi-generator 能生成 Python 与 TypeScript client |
| P0 脚手架 | 读第 13 章。创建 monorepo、pyproject、control\_plane 与 data\_plane 两个 FastAPI 骨架、Alembic、docker-compose（Postgres、Redis、Vault dev、OPA、MinIO；不含 Keycloak）、ruff / mypy / pytest 与 CI。先给计划。 | make up && make test 全绿，两个服务 /healthz 正常 |
| P1 IAM | 读第 2、3、11.1 章。实现 iam 模块：org\_unit 树（迁移、停用、事件）、user 与归属、user\_role、user\_data\_scope、密级与 clearance\_request 审批、本地登录（argon2id + TOTP）、会话令牌、AuthProvider 插件接口（只实现 local）、consumer 与 consumer\_credential（哈希、轮换、吊销）、consumer\_public\_key 与用户断言验签（exp、aud、jti 防重放）、user\_binding 与 bind\_policy、批量导入与管理 API、IAM 快照与事件。实现 PrincipalResolver，并补 RLS 与测试。 | 伪造、过期、重放的断言被拒；未绑定用户按 bind\_policy 处理；分公司管理员只能改本公司子树；密级提升必须经审批；组织迁移后数据范围自动更新 |
| P2 Registry 与快照 | 读第 6、11.2 章。实现 asset、asset\_version、release、release\_snapshot；版本发布后不可变（触发器加测试证明）；发布时生成快照，经 outbox 事件推送数据面，数据面本地缓存并校验 seq。 | 停掉 control\_plane，数据面仍能按快照解析 Tool |
| P3 Connector 与 Runtime | 读第 8、9 章。实现 Connector Protocol、MockConnector、kb-connector（只调知识库开放接口，服务账号，配置化）；Tool Runtime：入参校验、限流、熔断、超时、重试与幂等、Result Guard、统一执行结果与错误分类、tool\_execution（幂等键唯一约束）、unknown 与 reconcile 对账、Credential Manager（single-flight，401 后最多重试一次）；Connector SDK 提供统一 HTTP 客户端工厂，按连接的 scheme 与 tls\_mode（system\_ca、custom\_ca、pinned、insecure、http）构造客户端，CI 用 lint 禁止 verify=False；实现连接测试（证书链、指纹、失败原因）。 | 集成测试覆盖超时、熔断、幂等重放、沙箱拒写、unknown 对账、并发刷新只登录一次、HTTP 与自签 HTTPS（pinned、insecure）连接测试通过 |
| P4 PDP 与审计 | 读第 4 章。用 OPA 实现五方交集，rego 加表驱动测试（至少 30 个用例：数据范围越界、跨公司、密级、过期 Grant、超出授权上限、Suspended、未绑定用户、应用身份、用户层收窄）；实现 append-only audit\_log（按月分区、哈希链）与校验脚本。 | 默认拒绝；篡改一行审计校验失败；IAM 快照缺失时全部拒绝 |
| P5 Gateway | 读第 10.1 章。用官方 MCP SDK 实现 Streamable HTTP，tools/list 按 Principal 动态过滤，tools/call 与 REST invoke 走同一条执行链；支持 Bearer Token 与 X-User-Assertion。 | 同一调用经 MCP 与 REST 结果与审计一致；未授权工具不出现在 tools/list |
| P6 门禁与 Test Lab | 读第 7 章。实现状态机与 G0 至 G4 自动门禁插件；用例模型、沙箱 / 真实读 / 回放三种模式、稳定性测试；状态迁移必须由门禁结果驱动，非法迁移抛错。 | 一个读类 Tool 从 Draft 自动走到 InReview |
| P7 审批、授权与发布 | 读第 4、7 章。验收单与签字、审批流（提交人不能审批）、grant\_ceiling 与授权下放、两层判定、到期复核、灰度与一键回滚。 | 上限收缩后下游授权自动失效；回滚后新调用走旧版本，运行中实例不受影响 |
| P8 Workflow Engine | 读第 6.3、8.5 章。DSL 静态校验、PostgreSQL step journal 引擎（SKIP LOCKED）、human\_task、Saga、预算与超时、WorkflowExecutor 抽象。 | 杀掉 worker 后可恢复；补偿逆序执行；LLM 节点无法改变分支 |
| P9 风控与控制台 | 读第 12.4 章。风险规则引擎、自动 Suspended 与复核恢复、漂移比对、配额与计量；控制台首批页面（资产目录、审批待办、组织与用户、消费方与绑定、授权、Run 详情、审计检索）。 | 触发规则后自动熔断并告警，恢复需复核 |
| P10 分发与目录 | 读第 10.1、10.4、10.5 章。bundle 管理与套件端点、Catalog API、Skill Registry、corp-skill CLI、webhook 订阅。 | 未授权工具不出现在套件与目录；篡改 Skill 后 verify 失败；密钥轮换后旧密钥立即失效 |
| P11 开放标准 | 读第 10 章。MCP-Protocol-Version 协商（当前与上一版本）、工具注解自动生成、get\_run\_status、Skill 标准目录与 skills-ref validate、A2A 端点（第二批）。 | MCP Inspector 与至少 3 个客户端连通；同一 Skill 在至少 3 个平台可识别 |
| P12 部署与安全 | 读第 12 章。Helm 或 Kustomize 清单、探针、降级策略、OpenTelemetry 与看板、备份恢复脚本、故障注入。 | 分别停掉控制面、Redis、Vault，行为符合 12.2；PostgreSQL 切换后审计零丢失 |
| P13 发现与 SDK | 读第 10.3、10.5 章。capability\_domain、全文检索、三个元工具（danger 不进发现模式）、exposure 策略、上游 MCP 连接器、Python 与 TypeScript SDK、兼容矩阵与冒烟用例。 | 发现模式下未授权能力搜不到；SDK 能把目录注册为 LangGraph 工具并调用 |

## 16. ADR、docs 目录与待决事项

新项目的 ADR 全部重新编号，其中 0001 是本版的核心决策；开工前必须先确认 16.3 表中前五项待决事项，它们直接决定 IAM 模块的实现。

### 16.1 初始 ADR

| 编号 | 主题 | 状态 |
| --- | --- | --- |
| 0001 | 平台自建身份、组织、密级与授权，不读取任何外部系统的组织与权限；知识库作为普通旧系统接入 | Accepted |
| 0002 | 首期模块化单体加独立数据面进程 | Accepted |
| 0003 | 首期自研 PostgreSQL 工作流引擎，保留 WorkflowExecutor 抽象 | Proposed |
| 0004 | 授权用 OPA，采用五方交集；身份数据经 IAM 快照分发，缺失即拒绝 | Accepted |
| 0005 | 认证：控制台本地账号加 TOTP，消费方 API Token；OIDC 与 OAuth 2.1 为可选插件 | Proposed |
| 0006 | 终端用户身份用消费方签名的用户断言，经 user\_binding 映射，bind\_policy 默认 strict | Proposed |
| 0007 | Skill 用标准 Agent Skills 目录，私有信息放 metadata 或 skill.lock.json | Accepted |
| 0008 | 不部署 Connector Agent，专线直连 | Accepted |
| 0009 | 写操作只走 API 或存储过程，读可走只读副本 | Proposed |
| 0010 | 是否给旧系统加幂等键等小接口 | Open |
| 0011 | REST 与 MCP 共用同一执行链，自研 Agent OS 不绕自己的 MCP | Accepted |
| 0012 | 身份模式：默认 service\_account，高风险叠加用户授权或审批 | Proposed |
| 0013 | 能力发现：直连套件加发现模式，danger 不进发现模式 | Proposed |
| 0014 | 上游 MCP 视为不可信来源，固定版本与内容哈希 | Accepted |
| 0015 | 审计仅追加、哈希链、按月分区 | Accepted |
| 0016 | 密级维护：个人覆盖加组织默认，提升需安全岗审批并带到期日 | Proposed |

新增：ADR-0017（Proposed）下游传输与 TLS：旧系统可用 HTTP 或 HTTPS（含自签证书），策略挂在连接上；默认 custom\_ca 或 pinned，insecure 与 http 为例外，仅限内网、需审批与到期日，write 与 danger 级别需双人复核；对外统一 HTTPS。

### 16.2 docs/ 目录

```
docs/
  architecture.md                  # 本文档，唯一设计依据
  architecture/archive/            # v0.1、v0.2 仅归档，不再引用
  adr/                             # 0000-template.md、0001 至 0016、README.md 索引
  api/                             # control-plane.openapi.yaml、data-plane.openapi.yaml、conventions.md
  specs/                           # tool-spec.schema.json、workflow-dsl.schema.json、policy-input.schema.json、skill-package.md
  onboarding/                      # connector-ledger.md（接入台账）、acceptance-sheet.md（验收单）、org-bootstrap.md（组织与用户初始化）
  ops/                             # runbook.md、testing.md、seed-data.md、client-compat.md
  glossary.md                      # Principal、org_unit、clearance、用户断言、user_binding、授权上限、IAM 快照、套件、门禁
```

### 16.3 待决事项（开工前确认）

| 序号 | 问题 | 为什么重要 | 建议默认 |
| --- | --- | --- | --- |
| 1 | 终端用户数量级与来源：每个分公司大约多少员工需要被平台识别？ | 决定是否需要批量导入、管理 API 与性能目标 | 先导入需要使用 Agent 的员工，其余走应用身份 |
| 2 | 分公司消费方能否稳定提供员工标识并持有签名私钥？ | 决定用户断言能否落地，否则只能用 app\_only | 要求提供，不能提供的消费方按应用身份接入 |
| 3 | 密级由谁维护、按人还是按部门默认？ | PDP 第四方交集的数据来源 | 部门默认加个人覆盖，安全岗审批 |
| 4 | 员工入离职、调动由谁在平台内维护？ | 自建身份没有外部同步，数据会过期 | 分公司管理员负责，季度复核，离职员工的绑定自动失效 |
| 5 | 数据范围默认粒度：主归属部门子树还是整个公司？ | 决定 scope\_binding 的默认拒绝面 | 主归属部门子树，公司管理员可扩大 |
| 6 | 通知渠道（企微、钉钉、邮件）与审批 SLA 日历 | 审批与人工任务的时效 | 邮件加一个 IM，按分公司工作日历 |
| 7 | 执行引擎自研还是 Temporal | 取决于团队运维能力 | 首期自研 |
| 8 | 非功能指标（99.9%、P95 100ms）需在 M2 后压测校准 | 避免空头承诺 | M2 后校准 |
