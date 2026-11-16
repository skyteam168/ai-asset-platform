# ADR 索引

| 编号 | 主题 | 状态 |
| --- | --- | --- |
| [0001](0001.md) | 平台自建身份、组织、密级与授权；知识库作为普通旧系统接入 | Accepted |
| [0002](0002.md) | 首期模块化单体加独立数据面进程 | Accepted |
| [0003](0003.md) | 工作流引擎：自研 step journal（spike 后选定，DBOS Transact 未采用），保留 WorkflowExecutor 抽象 | Accepted |
| [0004](0004.md) | 授权用 OPA 五方交集；PrincipalResolver 预计算事实，OPA 只持策略；快照缺失即拒绝 | Accepted |
| [0005](0005.md) | 认证：控制台本地账号加 TOTP；消费方 client\_credentials（private\_key\_jwt）与静态令牌；OIDC 为可选插件 | Accepted |
| [0006](0006.md) | 终端用户身份：消费方签名的用户断言，推荐 RFC 7523 换平台令牌，兼容请求头模式；bind\_policy 默认 strict | Proposed |
| [0007](0007.md) | Skill 用标准 Agent Skills 目录，私有信息放 metadata 或 skill.lock.json | Accepted |
| [0008](0008.md) | 不部署 Connector Agent，专线直连 | Accepted |
| [0009](0009.md) | 写操作只走 API 或存储过程，读可走只读副本 | Proposed |
| [0010](0010.md) | 是否给旧系统加幂等键等小接口 | Open |
| [0011](0011.md) | REST 与 MCP 共用同一执行链 | Accepted |
| [0012](0012.md) | 身份模式：默认 service\_account，高风险叠加用户授权或审批 | Proposed |
| [0013](0013.md) | 能力发现：直连套件加发现模式，danger 不进发现模式 | Proposed |
| [0014](0014.md) | 上游 MCP 视为不可信来源，固定版本与内容哈希 | Accepted |
| [0015](0015.md) | 审计仅追加、分片哈希链、定时锚定签名、按月分区 | Accepted |
| [0016](0016.md) | 密级维护：个人覆盖加组织默认，提升需安全岗审批并带到期日 | Proposed |
| [0017](0017.md) | 下游传输与 TLS：策略挂在连接上；默认 custom\_ca 或 pinned；insecure 与 http 为例外（内网、审批、到期日、写类双人复核）；检查在请求前完成；对外统一 HTTPS | Accepted |
| [0018](0018.md) | MCP 同时支持 2025-11-25 与 2026-07-28；异步用 tasks 扩展并保留 get\_run\_status | Proposed |
| [0019](0019.md) | 事件通道首期用 PostgreSQL outbox + LISTEN/NOTIFY + 拉取兜底，不引入消息队列 | Proposed |
| [0020](0020.md) | 声明式 REST Connector 为首选接入方式；Connector 独立打包与版本化 | Proposed |
| [0021](0021.md) | 写操作确认流：confirmation 对象，同一用户再签名，参数哈希一致才执行 | Proposed |
| [0022](0022.md) | 审计写入：本地持久化 spool 后异步入库 | Proposed |
| [0023](0023.md) | 基础组件：OpenBao、Valkey、SeaweedFS 或 Ceph RGW 替代 Vault、Redis、MinIO | Proposed |
| [0024](0024.md) | 数据流护栏：套件组合约束与 Run 污点标记 | Proposed |
| [0025](0025.md) | 跨境数据合规由集团另行解决，平台不做数据驻留与跨境报表 | Accepted |
| [0026](0026.md) | sql_readonly：扩展 system_connection.scheme（postgres/mysql）；密码仅 OpenBao；模板化只读 SQL | Accepted |
| [0027](0027.md) | Credential Manager 声明式登录握手（prepare / RSA transform / jwt_exp） | Accepted |
| [0028](0028.md) | Credential Manager Cookie/Session（form 登录 + PHPSESSID）与简易接入向导 | Accepted |
