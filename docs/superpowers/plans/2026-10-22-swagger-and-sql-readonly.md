# Swagger 兼容与数据库只读 Connector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 Swagger 2.0 文档可走现有 Importer；并按 architecture.md §9.5 落地「登记 SQL 模板 → 只读 Tool」能力（先 PostgreSQL，再 MySQL），与声明式 REST 共用门禁/授权/Runtime 出口。

**Architecture:** Importer 仅多一步「Swagger2→OpenAPI3」再复用 H2；数据库不开放自由 SQL，而是 `connector_instance.kind=sql_readonly` + 管理员登记的参数化模板，Runtime 经专用驱动池执行，结果按 `output_schema` 裁剪。写库仍禁止。

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy 2 async / Alembic / pytest；Swagger 转换用 `openapi-spec-validator` 已有栈 + `swagger2openapi`（或等价纯 Python：`prance`/`openapi-schema-validator` 生态中的 `swagger-spec-validator` + 自研最小转换 / 依赖 `openapi-core` 不强制）；PG 用 asyncpg；MySQL 用 asyncmy（可选 extra）。

**Spec:** `docs/architecture.md` §9.1、§9.2、§9.5、§9.7、§9.8；既有 H2/H3 Importer；`docs/specs/system-connection.schema.json`（需扩展时先 ADR）。

## Global Constraints

- 设计唯一依据 `docs/architecture.md`；与字面冲突先停（尤其是 `system_connection.scheme` 仅 http/https 时，见 Task 3 ADR）。
- 错误码只用 `docs/api/error-codes.md`；新增先登记。
- 新表：`company_id` + RLS + `aip_app` 授权 + `tests/iam_seed.py` truncate。
- 每切片：迁移 → 服务 → API → 测试 → `tools/gen_openapi.py` → `docs/ops/milestones.md` → 单独提交（用户要求时）。
- 控制台文案 `t("…")` + `en-US.ts` / `vi-VN.ts`。
- 沙箱：`sql_readonly` 不得执行非 SELECT/WITH…SELECT；live 写一律拒绝。
- 日志/审计禁止明文 SQL 参数中的凭据与未脱敏行数据（可记 template_id、row_count、耗时）。

## Review Focus

- Swagger2 含 `in: body` / `formData` / 外部 `$ref`：转换后仍能 preview，失败时错误可读（不得 500）。
- SQL 注入：模板只允许命名绑定参数（`$1`/`:name`），拒绝字符串拼接与多语句。
- 只读会话：连接后 `SET default_transaction_read_only = on`（PG）/ 等价；超时与 LIMIT 生效。
- EXPLAIN 超成本：登记/发布失败，不得 silent 上线。
- 跨公司：模板与连接带 `company_id`，RLS 下不可串读他司库。

## 范围裁剪

| 项 | 本计划 | 理由 |
| --- | --- | --- |
| Swagger 2.0 → OpenAPI 3 自动升级 | **做** H38 | 成本低，覆盖大量旧文档 |
| OpenAPI 3.x / Postman | 已有，不改契约 | H2/H3 |
| PG 只读模板 Connector | **做** H39a–H39e | architecture 9.5 |
| MySQL 只读 | **做** H39f | 分公司常见 |
| SQL Server / Mongo | **不做**（另开） | 先验证模型 |
| 元数据扫库自动生成全表 Tool | **不做** | 易失控；可后做「从视图清单生成草稿」 |
| 写库 / 存储过程写 | **不做** | 架构禁止直连写 |
| RPA | **不做** | 架构后做 |

## 里程碑编号建议

| 切片 | 名称 |
| --- | --- |
| **H38** | Swagger 2.0 Importer 兼容 |
| **H39a** | ADR：DB 连接挂载与 scheme |
| **H39b** | 迁移：`sql_query_template` + kind |
| **H39c** | `SqlReadonlyConnector`（PostgreSQL）+ Runtime |
| **H39d** | 控制面 API：模板 CRUD / 试跑 / EXPLAIN |
| **H39e** | 控制台 + Tool 映射约定 |
| **H39f** | MySQL 引擎适配 |

## 文件地图

| 切片 | 主要文件 |
| --- | --- |
| H38 | `importer/openapi.py`、`importer/swagger2.py`（新）、`tests/test_h38_swagger2.py`、`console` ApiImport 文案、`gen_openapi` 若有描述更新 |
| H39a | `docs/adr/00xx-sql-readonly-connection.md`（或 `tools/gen_adrs` 流程）、`docs/specs/system-connection.schema.json`、`declarative-connector` 旁新 schema `sql-query-template.schema.json` |
| H39b | `migrations/versions/20261022_0025_sql_readonly.py`、`conn/schemas.py` kind 枚举 |
| H39c | `src/aip/connectors/sql_readonly.py`、`libs/dbclient/`（池工厂）、`data_plane/runtime.py` |
| H39d | `control_plane/sql_templates/`（service+api）、错误码、`tests/test_h39_sql_readonly.py` |
| H39e | `console` Connections/Assets 页、i18n |
| H39f | `sql_readonly.py` mysql 分支、`pyproject` optional、测试 |

---

### Task 1: H38 Swagger 2.0 → OpenAPI 3 再导入

**Files:**
- Create: `src/aip/control_plane/importer/swagger2.py`
- Modify: `src/aip/control_plane/importer/openapi.py`（`load_document`）
- Test: `tests/test_h38_swagger2.py`
- Modify: `console/src/pages/ApiImport.tsx`（文案）、locales

**Interfaces:**
- Produces: `upgrade_swagger2(doc: dict[str, Any]) -> dict[str, Any]`（返回合法 OpenAPI 3.x dict）
- Consumes: 现有 `openapi.to_operations` / `importer.import_openapi`

- [ ] **Step 1: 放一份最小 Swagger2 fixture**

路径：`tests/fixtures/swagger2-pet-mini.yaml`（含 `swagger: "2.0"`、`paths./pets.get`、`definitions.Pet`、一个 `in: body` 的 POST）。

- [ ] **Step 2: 失败测试 `test_preview_swagger2_upgrades`**

调用现有 preview/import 路径（与 `test_h2_importer` 同风格），传入 fixture；期望至少 1 个 operation，且不再报「仅支持 OpenAPI 3.x」。

- [ ] **Step 3: 实现升级**

在 `load_document`：若 `doc.get("swagger")` 以 `2.` 开头 → `upgrade_swagger2`；若已是 `openapi: 3.` 保持；若两者皆无 → `INVALID_ARGUMENT`。  
升级实现优先：依赖成熟库（评估 `swagger2openapi` CLI 不适合进程内；推荐 Python 包如将 Swagger 转 3 的维护中库，选型写入 PR 说明）。转换失败 → `INVALID_ARGUMENT` + 原因摘要。

- [ ] **Step 4: 负例测试**

`test_swagger_1_rejected`：`swagger: "1.2"` → 400。  
`test_openapi3_unchanged`：现有 mes-vn01 行为不变。

- [ ] **Step 5: 控制台**

导入抽屉标题/帮助改为「OpenAPI / Swagger」；`t(...)` + en/vi。

- [ ] **Step 6: milestones H38 + 提交**

验收：`pytest tests/test_h38_swagger2.py tests/test_h2_importer.py -q` 全绿。

---

### Task 2: H39a ADR — DB 连接如何挂 system_connection

**Files:**
- Create: ADR（按仓库 ADR 流程，建议标题「sql_readonly 连接与 system_connection.scheme 扩展」）
- Modify: `docs/specs/system-connection.schema.json`（按 ADR 结论）
- Create: `docs/specs/sql-query-template.schema.json`

**决策（写入 ADR，实施前若产品否决则停）：**

1. **推荐方案 A（本计划默认）**  
   - `system_connection.scheme` 扩展：`postgres` | `mysql`（后续再加 `mssql`/`mongodb`）。  
   - `base_url` 模式放宽为对应 URI（**不含密码**），例如 `postgres://host:5432/dbname`、`mysql://host:3306/dbname`。  
   - 密码/用户仅在 OpenBao `credential_ref`。  
   - `tls_mode`：DB TLS 用 `system_ca` / `custom_ca` / `insecure`（无 pinned 首期可注明 N/A）。  
2. **备选方案 B**（若不愿动 HTTP 连接模型）：HTTP `system_connection` 仅挂 company/system_key；DSN 全在 `connector_instance.config`。ADR 必须二选一，禁止模糊。

**模板 JSON Schema 要点：**

```json
{
  "template_key": "order_list_by_plant",
  "engine": "postgres",
  "statement": "SELECT wo_no, status FROM v_wo WHERE plant = :plant LIMIT :limit",
  "param_schema": { "type": "object", "properties": { "plant": {"type":"string"}, "limit": {"type":"integer","maximum":200} }, "required": ["plant"] },
  "max_rows": 200,
  "statement_timeout_ms": 5000,
  "explain_cost_cap": 100000
}
```

- [ ] **Step 1: 写 ADR（Accepted/Proposed）并更新 schema 生成物**
- [ ] **Step 2: 若方案 A，更新 `gen_schemas` / 契约测试中的 scheme 枚举**
- [ ] **Step 3: milestones 记 H39a；提交**

---

### Task 3: H39b 迁移与 kind

**Files:**
- Create: `migrations/versions/20261022_0025_sql_readonly.py`（编号以当时 head+1 为准）
- Modify: `src/aip/control_plane/conn/schemas.py`、`conn/api.py`（`kind` 增加 `sql_readonly`）
- Modify: `tests/iam_seed.py` truncate 列表

**表 `conn.sql_query_template`（建议列）：**

| 列 | 类型 | 说明 |
| --- | --- | --- |
| id | uuid PK | |
| company_id | uuid | RLS |
| connector_instance_id | uuid FK | kind 必须为 sql_readonly |
| template_key | text | 实例内唯一 |
| engine | text | postgres / mysql |
| statement | text | 参数化；禁止 `;` 多语句 |
| param_schema | jsonb | JSON Schema |
| max_rows | int | default 200, max 1000 |
| statement_timeout_ms | int | default 5000 |
| explain_cost_cap | numeric | nullable=跳过成本门 |
| output_field_whitelist | jsonb | 列白名单 |
| status | text | draft / active / disabled |
| created_at | timestamptz | UTC |

约束：RLS 策略与现有 `conn.*` 一致；`UNIQUE (connector_instance_id, template_key)`。

- [ ] **Step 1: 迁移 + 授权 + RLS**
- [ ] **Step 2: API 创建 connector 时允许 `kind=sql_readonly`**
- [ ] **Step 3: 测试 `test_create_sql_readonly_instance`**
- [ ] **Step 4: 提交**

---

### Task 4: H39c SqlReadonlyConnector（PostgreSQL）+ Runtime

**Files:**
- Create: `src/aip/libs/dbclient/pool.py`（按 connection 构建 async 引擎/池；**禁止**在 Connector 内拼明文密码进日志）
- Create: `src/aip/connectors/sql_readonly.py`
- Modify: `src/aip/data_plane/runtime.py`（`kind == "sql_readonly"` 分支）
- Test: `tests/test_h39_sql_readonly.py`（可用现有测试 PG 或 testcontainers）

**Interfaces:**
- `SqlReadonlyConnector(name, config, templates: Mapping[str, Template], secrets)`
- `async def execute(self, op: str, params: dict, ctx: ExecContext) -> ConnectorResult`  
  - `op` = `template_key`  
  - Tool Spec `connector_ref.operation` 与之对应  
- 行为：校验参数 → 强制 `LIMIT`（取 min(请求, max_rows, 1000)）→ 只读事务 + timeout → fetch → 按 whitelist 投影

**映射约定（Tool Spec / 声明式旁路）：**

```yaml
connector: erp_vn_ro
operation: order_list_by_plant   # = template_key
# 无 HTTP request 块；Runtime 见 kind=sql_readonly 走 SQL 路径
```

或在 Tool Spec `connector_ref` 增加 `"transport": "sql_template"`（若现有结构无 YAML operation，则用 connector config 内 `operations` 表）。**实现时与现有 snapshot 中 declarative config 形状对齐，选改动面最小的一种，并在 Task 4 测试里钉死。**

- [ ] **Step 1: 测试 `test_sql_template_select_ok`** — 插入模板，执行返回行，含 LIMIT
- [ ] **Step 2: 测试 `test_sql_rejects_multi_statement`** — `statement` 含 `;` → INVALID_ARGUMENT
- [ ] **Step 3: 测试 `test_sql_sandbox_blocks_non_select`** — `DELETE`/`UPDATE` 模板 active 被拒（登记时也可拒）
- [ ] **Step 4: 实现池 + Connector + Runtime 分支**
- [ ] **Step 5: 提交**

---

### Task 5: H39d 控制面 API

**Files:**
- Create: `src/aip/control_plane/sql_templates/{service,api,schemas}.py`
- Modify: app 挂载路由、`tools/gen_openapi.py`
- Modify: `docs/api/error-codes.md`（若需新码：建议复用 `INVALID_ARGUMENT` / `DOWNSTREAM_TIMEOUT`；成本超限用 `INVALID_ARGUMENT` 或登记 `QUERY_COST_EXCEEDED`）

**API（建议）：**

| 方法 | 路径 | operationId | 说明 |
| --- | --- | --- | --- |
| POST | `/api/v1/connector-instances/{id}/sql-templates` | `createSqlTemplate` | 创建 draft；可选跑 EXPLAIN |
| GET | `/api/v1/connector-instances/{id}/sql-templates` | `listSqlTemplates` | |
| PUT | `/api/v1/sql-templates/{id}` | `updateSqlTemplate` | |
| POST | `/api/v1/sql-templates/{id}/try` | `trySqlTemplate` | 参数试跑，sandbox 连接；返回行数与截断样例 |
| POST | `/api/v1/sql-templates/{id}/activate` | `activateSqlTemplate` | EXPLAIN 过门 + status=active |

权限：`platform_admin` / `asset_developer`；company 范围与实例一致。

- [ ] **Step 1: 契约测试路由登记**
- [ ] **Step 2: try/activate 单测（含 cost cap 失败）**
- [ ] **Step 3: 实现 + gen_openapi**
- [ ] **Step 4: milestones H39d；提交**

---

### Task 6: H39e 控制台与 Tool 草稿体验

**Files:**
- Modify: `console/src/pages/Connections.tsx`（或独立页签「SQL 模板」）
- Modify: `console/src/pages/Assets.tsx`（创建 Tool 时可选 sql operation）
- Modify: locales

**最小 UI：**

1. 连接器实例 kind=sql_readonly 时显示「SQL 模板」Tab。  
2. 表单：template_key、statement、参数 JSON Schema、max_rows、timeout、试跑、激活。  
3. 帮助文案：禁止写库；须用只读账号与副本。

- [ ] **Step 1: 类型 + API client**
- [ ] **Step 2: UI + i18n 覆盖测试**
- [ ] **Step 3: 手工验收清单写入 milestones**
- [ ] **Step 4: 提交**

---

### Task 7: H39f MySQL 引擎

**Files:**
- Modify: `sql_readonly.py`、`pool.py`
- Modify: `pyproject.toml` optional `[mysql]`
- Test: 用 testcontainers MySQL **或** 跳过无容器时的 mark（CI 有 Docker 则跑）

- [ ] **Step 1: `test_mysql_template_select_ok`**
- [ ] **Step 2: 只读会话设置（`START TRANSACTION READ ONLY` 等）**
- [ ] **Step 3: milestones H39f；提交**

---

## 测试清单（汇总）

| 用例 | 切片 |
| --- | --- |
| Swagger2 preview 出 operation | H38 |
| Swagger1 拒绝 / OpenAPI3 回归 | H38 |
| 创建 sql_readonly 实例 | H39b |
| PG SELECT 模板成功 + LIMIT | H39c |
| 多语句 / 非 SELECT 拒绝 | H39c |
| EXPLAIN 超成本不可 activate | H39d |
| try 不写审计明文密码 | H39d |
| 控制台 i18n | H39e |
| MySQL SELECT | H39f |
| 路由均在 OpenAPI | 各 API 切片 |

## 可行性与工期（参考）

| 切片 | 人日 | 风险 |
| --- | --- | --- |
| H38 | 2–3 | 转换库对脏 Swagger 的兼容 |
| H39a | 1–2 | scheme 扩展需产品确认 |
| H39b | 1–2 | 低 |
| H39c | 4–6 | 池、超时、快照装载模板 |
| H39d | 3–4 | EXPLAIN 成本字段跨版本差异 |
| H39e | 2–3 | 低 |
| H39f | 2–3 | CI 容器 |

**合计约 3–4 周（1 名后端为主 + 少量前端）**；H38 可单独先合并。

## 依赖与不做清单（再次锁定）

- 不实现 Agent 自由 SQL / Text-to-SQL。  
- 不实现 Mongo/SQL Server（模型验证后再开）。  
- 不实现 RPA。  
- 写操作继续走 API/存储过程（代码 Connector），不走本模板通道。

---

## Execution Handoff

计划已保存到 `docs/superpowers/plans/2026-10-22-swagger-and-sql-readonly.md`。

**建议执行顺序：** H38 → H39a（ADR 确认）→ H39b → H39c → H39d → H39e → H39f。

在新会话中可说明：按该计划用 `subagent-driven-development` 或 `executing-plans` 从 Task 1 开始；**H39a ADR 未合并前不要写 H39b 迁移。**
