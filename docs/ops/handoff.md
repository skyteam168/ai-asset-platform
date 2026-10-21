# 交接说明：当前进度与待做（2026-10-20）

给接手的开发者或 AI 编程助手（Cursor 等）：先读本文，再读 [architecture.md](../architecture.md) 对应章节与 [milestones.md](milestones.md) 最后几节。设计依据只有 architecture.md；与它冲突时先提问，不要自行改设计。

## 1. 进度

| 里程碑 | 状态 | 提交 |
| --- | --- | --- |
| M0 文档契约、脚手架、CI | 完成 | 7173984 |
| M1 自建 IAM | 完成 | b078743 |
| M2 Registry、PDP（OPA）、审计哈希链 | 完成 | 39273df |
| M3 Connector、出站 TLS 工厂、Tool Runtime | 完成 | e0d166c |
| M4 MCP Gateway、门禁与审批、授权流程、部署打包 | 完成 | 934f83c |
| 控制台最小集、镜像内置控制台 | 完成 | a86ace1、7f0dfa1 |
| ADR-0003 工作流引擎选型（自研 step journal） | 完成 | 4005ff0 |
| M5 确认流、Workflow 引擎、G3、平台待办 | 完成 | f8a9a2d |
| 控制台品牌、中英越三语、明暗主题、界面改版 | 完成 | 58c17e7、8c4b8ad |
| **M6a** 风险规则、自动熔断与复核恢复、配额与计量 | 完成 | f96ddb1 |
| **M6b** 套件、目录与 webhook | 完成 | 0208d45 |
| **M6c** Skill Registry 与 corp-skill | 完成 | 04063f7 |
| **M6d** 合成探针与契约漂移 | 完成 | 412ef8b |
| **M6e** M5 延后项与控制台 | 完成 | 3184785 |
| **M7a** MCP tasks + get_run_status | 完成 | 05269d9 |
| **M7b** MCP MRTR / elicitation 确认 | 完成 | 98f8017 |
| **M7c** MCP 发现模式三元工具 | 完成 | 92fdd8a |
| **M7d** 上游 MCP 连接器 | 完成 | b81acbf |
| **M7e** Python SDK + LangChain | 完成 | f401279 |
| **M7f** TypeScript SDK | 完成 | bf6568a |
| **M7g** A2A 最小出口 | 完成 | 62d5100 |
| **M7h** MCP 客户端兼容 | 完成 | cf36e0b |
| **M7i** 故障演练（12.2） | 完成 | dbb4a25 |
| **M7j** consumer-kit 断言签名示例 | 完成 | 33aab1a |
| **M7k** PostgreSQL 切换与审计零丢失 | 完成 | 1efdf31 |
| **M7l** 多进程容量压测 | 完成 | 3c9058d |
| **M7m** 发现检索物化 / 中文匹配 | 完成 | 26a888a |
| **H1** G4 回归与影响分析 | 完成 | af41a15 |
| **H2** OpenAPI Importer（声明式草稿） | 完成 | 9414150 |
| **H3** Postman Importer（声明式草稿） | 完成 | 30cbde4 |
| **H4** Prism Mock 自动生成 | 完成 | 52bbb04 |
| **H5** OpenTelemetry 贯通 | 完成 | 699b4ad |
| **H6** LLM Gateway（白名单 / 预算 / 脱敏） | 完成 | f71dde4 |
| **H7** 工具选择评测（套件 G2） | 完成 | 45e1f2f |
| **H8** Importer AI 候选描述 | 完成 | 30e2140 |
| **H9** G0 PII 辅助发现 | 完成 | e851e05 |
| **H10** 控制台 LLM / 选择评测 | 完成 | 5d58390 |
| **M7** 其余 | **待做**（见第 7 节） | — |

测试现状：后端含 M6b–M7i 用例；TS SDK 与 consumer-kit（Python/TS）有 vitest/pytest；控制台组件测试与端到端在提交前再跑全量。

## 2. 本地运行（Windows 开发机）

前置：Docker Desktop 已启动、uv、Python 3.12、Node 22。仓库根目录执行：

```bash
uv sync
.venv/Scripts/python tools/dev.py up          # 启动 PostgreSQL(55432) / Valkey / OpenBao(58200) / OPA(58181) / SeaweedFS 并迁移
.venv/Scripts/python tools/dev.py migrate     # 只跑迁移（新增迁移后执行）
.venv/Scripts/python tools/dev.py bootstrap   # 首次：建集团与管理员，凭据写入 .aip-data/dev-admin.txt
cd console && npm ci && npm run build && cd ..
.venv/Scripts/python tools/dev.py serve       # 控制面 :18081（控制台 /console/），数据面 :18082
.venv/Scripts/python tools/dev_login.py       # 另开终端：打印 admin 口令与实时 TOTP 动态码
.venv/Scripts/python tools/dev.py seed-demo   # 可选：M5 演示数据（工作流任务与调用确认）
```

前端开发：`cd console && npm run dev`，打开 http://localhost:5173/console/（/api 与 /healthz 代理到 18081）。

## 3. 质量门禁（每次提交前全部通过）

```bash
.venv/Scripts/python tools/dev.py lint        # ruff + mypy --strict（src）+ TLS 约束检查
.venv/Scripts/python tools/dev.py test        # 全量 pytest（需要 dev.py up 起来的基础设施）
.venv/Scripts/python tools/dev.py docs        # 生成 ADR / JSON Schema / OpenAPI，并要求生成物已提交（git diff 为空）+ spectral + 链接检查
cd console && npm run typecheck && npm test && npm run build
cd sdk/typescript && npm ci && npm run typecheck && npm test && npm run build
cd sdk/consumer-kit/python && uv venv .venv && uv pip install -e ".[dev]" && .venv/Scripts/pytest   # Windows；Linux 用 .venv/bin/pytest
cd sdk/consumer-kit/typescript && npm ci && npm run typecheck && npm test && npm run build
cd console && npx playwright test e2e/console.spec.ts   # 需先 serve；凭据读 .aip-data/dev-admin.txt
```

`docs` 任务里的 `git diff --exit-code` 要求生成物已提交，所以开发中先跑 `tools/gen_openapi.py` 与 `tools/gen_schemas.py` 再提交，提交后再跑一次 `docs`。

界面走查截图：`cd console && SCREENSHOTS=1 npx playwright test e2e/screenshots.spec.ts`，输出到 `.tmp/screens`（1920×906，浅色与深色）。

## 4. 代码地图

| 路径 | 内容 |
| --- | --- |
| `src/aip/control_plane/` | 控制面：`iam/`、`registry/`、`grant/`、`gates/`、`approval/`、`testlab/`、`conn/`、`risk/`（M6a）、`webhook/`（M6b）、`skill/`（M6c 包与目录）、`todo_api.py`、`console_api.py`（含 `/console/catalog`） |
| `src/aip/data_plane/` | 数据面：`runtime.py`（统一执行链、幂等、确认、配额、对账）、`confirmation.py` / `confirm_api.py`（确认对象与通道）、`workflow/engine.py`（step journal 引擎）、`quota.py`、`mcp_gateway.py`、`a2a_gateway.py`（Agent Card + SendMessage/GetTask）、`oauth.py`、`pdp.py` / `facts.py` |
| `src/aip/libs/` | `httpclient`（唯一允许关闭证书校验的位置）、`tokens`、`audit`（spool、哈希链、锚点）、`workflow`（DSL 校验与 CEL 子集表达式，两个平面共用）、`common`（配置、错误码、web 装配） |
| `src/aip/sdk/` | 官方 Python 消费方 SDK（`Client` / `SdkError`）与可选 LangChain 适配；出站用 httpx，不经 `libs/httpclient` |
| `sdk/typescript/` | 官方 TypeScript 消费方 SDK（包名 `aip-sdk`：`Client` / `SdkError`）；fetch 可注入，与 Python 同 REST 面 |
| `sdk/consumer-kit/` | 消费方断言签名示例（Python / TS 可运行；Java / .NET 单文件模板）；不接主 `aip` 依赖 |
| `migrations/versions/` | Alembic，当前到 0015（upstream_mcp；此前 0014 decided_via=mcp） |
| `policies/aip/authz.rego` | OPA 五方交集策略与表驱动测试 |
| `tools/` | `gen_openapi.py`（OpenAPI 的唯一来源）、`gen_schemas.py`（docs/specs 的唯一来源）、`gen_adrs.py`、`dev.py`、`seed_m5_demo.py`、`dev_login.py` |
| `console/` | React 18 + Ant Design 5 + Vite；`src/App.tsx`（外壳：分组侧栏、顶栏、页签条）、`src/theme.tsx`（主题 token）、`src/i18n.ts` + `src/locales/`（三语）、`src/pages/*` |
| `tests/` | 按里程碑命名 `test_m{N}_*.py`；共用夹具 `iam_seed.py`、`m2_fixtures.py`（发布一个读类 Tool）、`m3_world.py`（不经数据库直接构造签名快照的数据面 World）；`test_m7_client_compat.py`（MCP 三客户端画像冒烟） |
| `docs/ops/client-compat.md` | MCP 客户端兼容矩阵与 Inspector 联调步骤 |
| `docs/ops/fault-drills.md` | 故障演练矩阵（12.2）与自动化对照 |
| `docs/ops/pg-failover.md` | PostgreSQL 主从切换本地演练（opt-in compose `pg-ha`） |
| `docs/ops/search-index.md` | 发现检索物化 search_text；zhparser / pg_trgm 可选路径 |
| `spikes/adr0003/` | 工作流引擎选型 spike（不进入产品代码） |

## 5. 约定与踩过的坑（务必遵守）

**后端**

- 每个功能：迁移 → 服务层 → 接口 → 测试 → `tools/gen_openapi.py` 登记接口 → 更新 milestones.md。`tests/test_m0_apps.py::test_implemented_routes_are_in_contract` 会检查每个路由都已登记到 OpenAPI。
- 新表放在对应 schema，带 `company_id`，开启 RLS（`app_bypass_rls() OR company_id = ANY (app_company_ids())`），并给 `aip_app` 授权；测试库每个用例开始时由 `tests/iam_seed.py::truncate()` 清空，新表要加进它的列表。
- 应用角色 `aip_app` 不是超级用户；审计表只能插入；需要删除的测试数据放进 truncate 列表（用 owner 连接）。
- 错误码只用 `docs/api/error-codes.md` 已登记的（`ErrorCode` 枚举）；运行级失败原因（如 BUDGET_EXCEEDED）放在 Run 的 error 字段，不新增错误码。
- 拼 SQL 时字段名必须白名单校验，取值一律绑定参数；确需拼接的行加 `# noqa: S608` 并写清理由（ruff 要求 noqa 在被报告的那一行）。
- Workflow DSL 与 Tool Spec 的 JSON Schema 在 `tools/gen_schemas.py` 里改，然后重新生成 `docs/specs/`。
- 写操作必须带幂等键；确认后执行的幂等键 = confirmation_id；Workflow 写步骤的幂等键 = `wf:{run}:{node}`。
- 审计 spool：测试里每次新建 spool 用不同的实例 id（同一 shard 从 seq 1 重写会被入库端当作重复投递忽略）。
- 时区用 `zoneinfo` + `tzdata`（Windows 没有系统时区库）。
- 中文注释风格：模块与函数用中文 docstring 说明“为什么”，与现有代码一致。

**前端（console/）**

- 所有界面文案写成 `t("中文原文")`，带参数用 `t("共 {0} 条", [n])`；英文与越南文译文加到 `src/locales/en-US.ts` 与 `src/locales/vi-VN.ts`。`src/i18n.test.tsx` 会检查源码里每个键都有两种译文、占位符一致，缺了测试就失败。服务端返回的中文数据（如风险规则名）用 `t(值)` 显示时，也要把这些值加进字典。
- 新页面：在 `src/App.tsx` 的 `SECTIONS` 里登记（路径、图标、角色、所属分组），页面根节点用 `<PageCard>`，列表页用 `<Toolbar>` + `PAGINATION`（`src/components.tsx`），状态用 `<StateTag>`。
- 主题颜色走 Ant Design token 与 `src/styles.css` 里的 CSS 变量，不要写死颜色；改完用截图脚本对比浅色与深色。
- 菜单项的无障碍名称含图标名（如 “audit 审计”）；端到端测试按名称定位时用正则 `/(^| )审计$/`，避免与分组标题“风控与审计”重名。

**Windows 环境**

- 运行 Python 脚本前设 `PYTHONIOENCODING=utf-8`；调用 npx 用 `npx.cmd`。
- Git Bash 的 heredoc 遇到复杂引号容易出错，长脚本请写成文件再执行。
- Docker Desktop 重启后要手动打开。

**提交**

- 每个切片单独提交，提交信息中文，第一行“M6b: …”格式，末尾一行：`Co-Authored-By: <助手名称>`（之前的提交使用 `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`）。

## 6. 待做：M6 剩余切片（按顺序）

### M6b 套件、目录与 webhook — 已完成（见 milestones.md）

### M6c Skill Registry 与 corp-skill — 已完成（见 milestones.md）

首期 zip 存 `registry.skill_package`（bytea），非 SeaweedFS；带脚本的 Skill 硬拒。

### M6d 合成探针与契约漂移 — 已完成（见 milestones.md）

write 类须显式配置 `probe`（可用 reconcile 查询参数）；漂移不自动 Suspended。

### M6e M5 延后项与控制台 — 已完成（见 milestones.md）

## 7. 之后：M7 与横向事项（**当前下一步 = IAM 业务角色等**）

- **M7a–m（已完成）**：见上表与 milestones（tasks、MRTR、发现、上游 MCP、SDK、A2A、兼容、故障演练、consumer-kit、PG 切换、容量、检索物化）。
- **H1–H11（已完成）**：至执行快照信封加密；控制台 LLM / 选择评测；G0 PII 建议。
- **横向（当前下一步）**：IAM 业务角色；OTel Collector 与看板；备份 WORM；响应体 Pydantic；K8s/CI；Patroni / zhparser；wf ctx/step 信封深化。

完整的未完成清单与原因见 milestones.md 各节“仍未完成”。
