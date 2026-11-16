# 业务系统接入全流程操作手册（简易接入 → MCP）

本文按 **2026-11-10 横机报修系统（`facility.repair.data_report`）实测跑通路径**整理，供接入**下一个系统**时逐步照做。

- **主线（阶段 A～H）**：适合 `none` / Cookie 会话、常走「简易接入」向导的系统。
- **旁路（阶段 A′）**：适合 **Bearer / API Key / 登录换 JWT** 的系统——**门禁到 MCP 与主线相同**，只换「连接 + 凭据」配法。

设计依据：[architecture.md](../architecture.md) §9.2、ADR-0027 / ADR-0028。本手册只写**操作顺序**与踩坑，不改设计。

**先分清两种 Token（极易混）：**

| 名称 | 用在哪 | 谁持有 |
| --- | --- | --- |
| **下游凭据** | 调老系统 API（Bearer / API Key / 登录换的 JWT） | 写在「系统连接」→ OpenBao，Tool Spec / 日志不见明文 |
| **平台消费方 Token** | AI 工作台调 AIP 的 MCP / REST | 「消费方」签发，Grant 授权后才能 `tools/list` |

---

## 0. 开始前准备

### 0.1 环境已启动

1. Docker Desktop 已开。
2. 仓库根目录执行：
   - `uv sync`（首次）
   - `.venv/Scripts/python tools/dev.py up`
   - `.venv/Scripts/python tools/dev.py serve`（局域网联调时先设 `AIP_SERVE_HOST=0.0.0.0`）
3. 控制台：`http://127.0.0.1:18081/console/`（或本机局域网 IP `:18081/console/`）
4. 数据面 / MCP：`http://127.0.0.1:18082/mcp`（或 `http://<本机局域网IP>:18082/mcp`）

### 0.2 准备好账号（职责分离，不能一人包办全部审批）

| 用途 | 建议账号 | 角色 |
| --- | --- | --- |
| 登记连接、建草稿、跑门禁、灰度/全量 | `admin` 或资产开发者 | `platform_admin` / `asset_developer` |
| 批准明文 HTTP「传输例外」 | **不能是登记人本人**；用 `security` 等 | `data_owner` |
| 业务验收 G5 | 资产所属公司的 `business_owner`（如越南厂资产需对该厂有 BO） | `business_owner` |
| 安全审核 G6 | `security` 等 | `data_owner`（且不能与 G5 同一人） |

本地 TOTP：`admin` 见 `.aip-data/dev-admin.txt`；`bo` / `security` / `dev` 见 `.aip-data/nike-path-staff.json`（口令与 admin 同为 bootstrap 口令时以本地文件为准）。

### 0.3 准备好目标系统信息（写在纸上再点）

1. **系统标识 `system_key`**：只用英文小写字母/数字/下划线，如 `repair`（**不要用中文**）。
2. **环境 `env`**：本地联调常用 `test`。
3. **归属公司**：与组织编码一致，如 `VN-E41139` / `HQ` / `DEMO`（**没有 VN01**）。
4. **base_url**：只到主机和端口，例如 `http://10.10.130.199:8082`  
   - **禁止**带 `/Index/login/login` 等页面路径。
5. **鉴权方式（先对号入座，再选阶段 A 或 A′）**：

| 下游怎么认你 | 选模式 | 走哪一节 |
| --- | --- | --- |
| 接口完全开放 | `none` | 阶段 A（简易接入） |
| 表单登录拿 Session Cookie | `cookie` | 阶段 A（简易接入） |
| 长期固定 Token，请求头 `Authorization: Bearer …` | `bearer` | **阶段 A′** |
| 长期 API Key，自定义头如 `X-API-Key` | `api_key` | **阶段 A′** |
| 用户名密码调登录接口，响应里取 JWT，过期再换 | `login` | **阶段 A′**（复杂握手见 A′.4） |

6. **至少一个只读接口**：方法、路径、成功条件（如 `$.msg == "success"`）、数据路径（如 `$.data`）。
7. **AI 工作台将使用的消费方 `client_id`**（如 `agent-platform-test`），后面授权必须授给**这个**消费方，否则 MCP 列表看不见工具。

---

## 阶段 A：简易接入（none / Cookie → Tool 草稿）

入口：**侧栏「接入管理」→「简易接入」**，或「系统连接」页的简易接入入口。  
向导四步：**系统 → 鉴权 → 接口 → 生成**。

### A1. 系统（登记连接）

1. 打开「简易接入」。
2. 选择「新建系统连接」（若该公司+系统+环境已存在，改选「选用已有连接」，跳到 A2）。
3. 填写：
   - 公司：选正确公司（如越南工厂）。
   - 系统标识：英文，如 `repair`。
   - 环境：`test`。
   - 地址：`http://主机:端口`（无路径）。
4. 若是明文 HTTP，按提示填写**传输例外理由**与**到期日**（默认约 90 天内）。
5. 提交登记。
6. **若提示「同一公司+系统+环境已存在」**：改「选用已有连接」，选中已有记录，不要硬新建。

### A2. 鉴权

1. 进入第 2 步「鉴权」。
2. 选择模式：
   - 接口无需登录：`none` → 可点测试连通（可选）→ 下一步。
   - 需要表单登录：`cookie` → 填登录路径、用户名字段、密码字段、账号密码 → 保存凭据 → **测试鉴权**（成功即可，界面不应回显密码）。
3. 点「下一步」。

### A3. 接口（可多个）

1. 进入第 3 步「接口」。
2. 填一条接口：
   - 操作名：英文标识，如 `data_report`（将进入 connector operation）。
   - 方法：`GET` / `POST` 等。
   - 路径：如 `/Index/data_report/...`（相对 base_url）。
   - 成功条件：如 `$.msg == "success"`。
   - 数据路径：如 `$.data`。
   - 标题/描述：给人看的中文说明。
3. 点「添加接口」。
4. 在列表中对该行点「试一下 / 点测」，确认返回业务 JSON，而不是 HTML 登录页或「not JSON」。
5. **同一系统还要接入更多接口**：重复步骤 2–4，全部点测通过。
6. 勾选本批要落库为 Tool 的接口 → 「下一步」。

### A4. 生成草稿

1. 进入第 4 步「生成」。
2. 确认 capability / 名称预览无误。
3. 点「生成草稿」。
4. 记下生成的资产名（常见形态：`facility.<system>.<operation>`，如 `facility.repair.data_report`）。
5. 到「资产与门禁」能搜到该资产，且存在 **draft** 版本 → 本阶段完成。

**本阶段未做完之前，不要指望 MCP 能看见工具。**

---

## 阶段 A′：Bearer / API Key / 登录换 JWT（Token 类系统）

> **与主线的关系**：A′ 只替换「登记连接 + 配下游凭据 + 落草稿」；完成后从 **阶段 C** 起与 Cookie 路径完全相同（用例 → G0～G8 → 授权 → MCP）。  
> **简易接入向导当前仅 `none` / `cookie`**。Token / JWT 请用「系统连接」+ 凭据 API（或后续扩展向导），再用「API 导入 / 手工导入 / 简易接入里选已有连接」落 Tool。

### A′.0 选型（再确认一次）

1. 打开下游接口文档，看认证说明。
2. 按 0.3 表选定 `bearer` / `api_key` / `login`。
3. 准备纸面材料：
   - `bearer`：完整 access token 字符串（将写入 OpenBao 的 `token` 字段）。
   - `api_key`：密钥字符串 + 请求头名（默认 `X-API-Key`）。
   - `login`：登录 URL 路径、请求 JSON 字段、响应里 JWT 的 JSONPath（如 `$.data.token`）、账号密码；若需「先拉公钥再 RSA 加密口令」见 A′.4。

### A′.1 登记 HTTPS 连接（系统连接页）

1. 打开「系统连接」→「新建」。
2. 填写：
   - 公司、系统标识（英文）、环境（如 `test`）。
   - **scheme**：`https`（Token 类系统优先 HTTPS）。
   - **base_url**：`https://主机:端口`（**不要**带 `/oauth/token` 或登录页路径）。
   - **TLS 策略**：
     - 公网标准证书 → `system_ca`
     - 公司内部 CA → `custom_ca`（上传 CA）
     - 自签且要钉死证书 → `pinned`（连接测试后固定指纹）
     - 仅内网临时放行 → `insecure`（仍算传输例外，要审批）
3. 提交。若是 `https` + `system_ca`/`custom_ca`/`pinned` 且非 insecure，一般**直接 active**，可跳过阶段 B。
4. 若误用 `http` 或选了 `insecure`：仍须走阶段 B 批准传输例外。
5. 建议点「测试连接」：确认握手、证书、白名单通过（测试本身可不带业务凭据）。

### A′.2 写入下游凭据（OpenBao，不回显明文）

控制台若暂无完整表单，用控制面 API（admin / 总部 IT 会话）：

**公共步骤**

1. 记下连接 `id`（系统连接列表）。
2. `PUT /api/v1/connections/{id}/credentials`，Body 只含需要的密文字段（至少一项）。
3. 响应只应出现 `credential_ref`（形如 `kv/data/<system_key>/<env>`），**不应**回显 token/password。

**模式 bearer（静态 Bearer Token）**

1. Body 示例：
   ```json
   { "token": "<下游发给你的长期或短期 token>" }
   ```
2. 鉴权配置（下一步 test-auth / Connector `auth`）使用：
   ```json
   { "mode": "bearer" }
   ```
3. 出站效果：`Authorization: Bearer <token>`。

**模式 api_key**

1. Body 示例：
   ```json
   { "api_key": "<密钥>" }
   ```
2. 鉴权配置：
   ```json
   { "mode": "api_key", "header": "X-API-Key" }
   ```
3. 若下游头名不是 `X-API-Key`，改 `header` 为实际名（如 `X-Token`）。

**模式 login（用户名密码换 JWT）**

1. Body 示例：
   ```json
   { "username": "<账号>", "password": "<密码>" }
   ```
2. 鉴权配置（简单 JSON 登录）：
   ```json
   {
     "mode": "login",
     "path": "/api/login",
     "token_path": "$.data.token",
     "ttl_s": 1800,
     "header": "Authorization",
     "prefix": "Bearer "
   }
   ```
3. 含义逐步对齐：
   - `path`：相对 **base_url** 的登录路径。
   - `token_path`：登录响应 JSON 里 JWT 的位置。
   - `ttl_s`：缓存多久再重新登录；若 JWT 自带 `exp`，可改用 `"ttl_from": "jwt_exp"`（见 ADR-0027）。
   - 平台会 single-flight：并发调用只打一次登录；401 时可刷新一次。

### A′.3 测试鉴权（必做）

1. `POST /api/v1/connections/{id}/test-auth`  
   Body：`{ "auth": { ...上一步的 mode 配置... } }`
2. 期望：`ok: true`；**响应不得包含** token / Cookie / 密码明文。
3. 失败时按 detail 查：
   - 登录非 200 / 非 JSON → path 或 base_url 错。
   - 「没有令牌」→ `token_path` 与真实响应不符（用真实登录响应对照改 JSONPath）。
   - TLS / 主机名 / 指纹 → 回 A′.1 调 TLS。
4. 通过后再去做接口点测或导入。

### A′.4 复杂登录握手（可选，RSA 加密口令等）

若下游是「先 GET 公钥 → RSA 加密 password → 再 POST 换 JWT」（设备管家类），不要手写代码 Connector，用声明式 `prepare` / `transform`（ADR-0027）：

1. `auth.mode` 仍为 `login`。
2. 增加 `prepare[]`：最多 5 步，用 JSONPath `capture` 公钥等。
3. 增加 `transform[]`：仅白名单算子（如 `rsa_encrypt_json`）。
4. `body` 模板只用 `{{secret.*}}` / `{{capture.*}}` / `{{vars.*}}` / `{{uuid}}` / `{{now_ms}}` 等允许变量。
5. 建议 `ttl_from: "jwt_exp"`，按票面过期续期。
6. 完整示例见 `docs/adr/0027.md` 与 `tools/activate_equipment_path.py` 演示路径。
7. 配好后同样跑 A′.3 `test-auth`。

### A′.5 落 Tool 草稿（与主线汇合）

凭据与 test-auth 通过后，任选一种落草稿方式：

1. **简易接入**：第 1 步选「选用已有连接」（即本连接）→ 鉴权步可跳过或仅确认 → 第 3 步加接口并点测（出站会带上连接凭据）→ 生成草稿。  
   - 注意：向导 UI 若仍只展示 none/cookie，点测依赖连接上已绑定的 `credential_ref` + Connector/导入时的 `auth` 配置；复杂 `login` auth 需写在 Connector 实例 / 导入配置的 `auth` 字段，与连接凭据配合。
2. **API / Postman / Swagger 导入**：走「创建向导 / Importer」，connector 指向该连接与 operation。
3. **资产与门禁里手建版本**：Spec 的 `connector_ref` 指向已配置 auth 的 declarative Connector。

4. 记下资产名与 draft 版本。
5. **下一步进入阶段 C**（门禁用例），不要跳到 MCP。

### A′.6 Token 类系统检查清单

```
[ ] A′.1 HTTPS 连接已登记，TLS 正确，base_url 无路径
[ ] A′.2 凭据已写入（bearer→token / api_key→api_key / login→username+password）
[ ] A′.3 test-auth 成功且无明文回显
[ ] A′.4（若需要）prepare/transform 声明式握手已测通
[ ] A′.5 Tool 草稿已生成
[ ] 若 http/insecure → 仍做阶段 B；纯 https 合规 TLS 可跳过 B
[ ] 从阶段 C 做到 H（与 Cookie 路径相同）
```

---

## 阶段 B：传输例外审批（明文 HTTP / insecure 必做）

在以下情况需要本阶段：`scheme=http`，或 `tls_mode=insecure`。  
正规 HTTPS（system_ca / custom_ca / pinned）且连接已是 **active** → **跳过本阶段，直接 C**。

明文 `http://` 或 insecure 登记后状态多为 **`pending_approval`**。未批准时，门禁真调下游会报类似 `TRANSPORT_EXCEPTION_EXPIRED`（文案笼统，实际是未激活/例外无效）。

登记成功后会生成 **`transport_exception` 审批待办**：数据 Owner 右上角铃铛有数量，「审批待办 → 门禁与使用申请」可见；也可在「系统连接」行内点「审批例外」。两条入口等价。  
若已在「系统管理 → 邮件配置」启用 SMTP，可处理角色与 data_owner（有邮箱）会收到带深链的审批邮件；未登录先登录再进待办。用户须在「组织与用户」维护邮箱。

1. **退出**登记人账号（如 admin）。
2. 用 **`data_owner` 且不是登记人** 的账号登录（如 `security`）。
3. 看铃铛 / 打开「审批待办」，处理类型为「传输例外」的单；**或**打开「系统连接」找到该行点「审批例外」。
4. 结论选通过（非 prod 一般一人即可；prod 需双人）。
5. 确认连接状态变为 **已生效 / active**（驳回则禁用）。
6. 切回开发账号继续阶段 C。

---

## 阶段 C：补齐门禁材料（仍在 draft）

入口：「资产与门禁」→ 搜资产名 → 点开 → 选中 **draft** 版本。

### C1. 门禁用例（至少各 1 条功能 + 权限）

1. 打开「门禁用例」页签。
2. 若列表为空或不对：
   - 点「填充功能模板」→ 改用例名（如「正常查询」）→ 按需改 `args` → 「添加用例」。
   - 点「填充权限模板」→ 改用例名（如「无授权被拒」）→ 「添加用例」。
3. **调用者画像里的公司编码**必须是真实组织编码：`HQ` / `VN-E41139` / `DEMO`，**不要写 VN01**。
4. 写错了：在 **draft** 下可「编辑 / 删除」后重填（离开 draft 后不可改用例）。
5. 确认有且仅需满足 G0：至少 1 个 `functional` + 1 个 `permission`。

### C2. 验收单模板

1. 打开「验收单」页签。
2. 填写目标场景（每行一个）、准确率、耗时上限。
3. 保存。

### C3. 提交 G0（静态检查）

1. 回到版本详情操作区，点「提交（G0 静态检查）」。
2. 通过后状态：`draft` → **`testing`**。
3. 若失败：按 problems 列表改 Spec/用例/验收单，仍在 draft 修（若已进 testing 失败回退，按提示回到 draft 再改）。

---

## 阶段 D：门禁实验室 G1–G4

版本状态须为 **`testing`**。

1. 点「运行门禁测试（G1 / G2 / G3）」等按钮（控制台文案以页面为准）。
2. 等待跑完，打开「门禁记录」看结果。
3. **全部通过**：状态 → **`in_review`**，并自动生成 G5、G6 审批待办。
4. **未通过**：常退回 **`draft`**。对照失败用例：
   - `TRANSPORT_EXCEPTION_EXPIRED`：回阶段 B 批传输例外。
   - `期望 succeeded，实际 failed`：查连接、鉴权、路径、成功条件。
   - 权限用例应「期望失败」：以勾选/通过为准，不要和功能用例搞反。
5. 修好后：若已回 draft，重新走 C3 → D；不要跳步。

---

## 阶段 E：评审审批（G5 + G6）

版本状态须为 **`in_review`**。两条都通过后才会 **`approved`**。

### E1. 业务验收 G5

1. 用 **business_owner** 登录（且对该资产公司可见；越南厂资产需对该厂有 BO 角色，否则待办为空）。
2. 打开「审批待办」→「门禁与使用申请」。
3. 找到 `资产名@版本`、类型「业务验收（G5）」→「处理」。
4. 结论选「通过」。
5. 填写「验收实测结果」JSON（可按模板，如 accuracy / latency_ms）。
6. 可填意见 →「确定」。
7. 该账号下这条 G5 待办消失。

### E2. 安全审核 G6

1. **换账号**：用 `data_owner`（如 `security`），**不能**是 G5 同一个人。
2. 「审批待办」→ 找到「安全审核（G6）」同版本 →「处理」。
3. 通过 →「确定」。
4. 用开发账号打开资产版本，确认状态为 **`approved`（已批准）**。

---

## 阶段 F：发布（G7 灰度 → G8 全量）

需 **`platform_admin`**。

### F1. 灰度 G7

1. 版本为 `approved` 时，点「灰度发布（G7）」。
2. 选择灰度范围公司（至少包含资产相关公司）。
3. 出口 exposure：勾选 **`rest` 与 `mcp`**（要给 AI 工作台用就必须含 mcp）。
4. 环境：与后续 Grant 的 env 一致（本地常用 `test`）。
5. 提交。状态 → **`canary`**。

### F2. 全量 G8

G8 默认要求灰度期间该 capability+版本在执行表中有足够调用（默认 **最少 20 次**，错误率 ≤ 5%）。

**本地联调推荐：**

1. 点「全量（G8）」。
2. 把「灰度期最少调用次数」改为 **`0`**。
3. 确定。状态 → **`active`（已生效）**。

**正式环境：**

1. 先完成阶段 G 授权。
2. 用消费方真实调用满 min_calls。
3. 再全量，min_calls 保持 20（或按制度填写）。

---

## 阶段 G：授权（没有授权，MCP 永远看不到）

MCP `tools/list` **按消费方 Grant 过滤**。发布 active 不等于工作台可见。

### G1. 下发授权上限（集团 → 分公司）

1. 打开「授权」页 → 页签「授权上限（集团 → 分公司）」。
2. 用 `platform_admin`：
   - 分公司：选**消费方所属公司**（工作台若用 `agent-platform-test`，通常是 HQ；用 `vn-ai-e41139` 则是越南厂）。
   - 资产集合：勾选本工具资产。
   - 密级上限：≥ 资产密级（只读演示常用 `2`）。
3. 点「下发上限」，表中出现「已生效」行。

> 注意：上限挂在**哪家公司**，就只能给**该公司下的消费方**建授权。工作台用哪个 `client_id`，上限就下发到那家公司。

### G2. 新建授权 Grant

1. 切到页签「授权」→「新建授权」（若刚下发了上限仍为空：刷新页面或关掉弹窗再开，以拉取最新上限列表）。
2. 填写：
   - 资产：本工具。
   - 依据的授权上限：选刚下发且包含该资产的那条。
   - 被授权方类型：`consumer`。
   - 被授权方：工作台实际使用的消费方（如 `agent-platform-test`）。
   - 环境：与发布 env 一致（如 `test`）。
   - 到期日：未来日期。
3. 确定。列表中该 Grant 为有效/active。

### G3. 确认消费方凭证

1. 「消费方」页找到同一 `client_id`。
2. 确认有可用静态令牌 / 凭证；没有则签发。
3. AI 工作台 MCP 配置：
   - URL：`http://<数据面主机>:18082/mcp`
   - Header：`Authorization: Bearer <该消费方 token>`
   - **不要**选「免登录」。

### G4. 部门 / 业务线隔离（能力已有，须按此配，不会自动拆）

**结论先说清：**

- MCP **不会**把「本公司已发布的全部工具」无差别推给所有人。
- `tools/list` 只展示**当前 Token 对应 Principal 有权看见**的工具。
- 若财务、织造**共用同一消费方 Token**，且该消费方被授了 10 套系统 → 两边看到的列表**一样**（联调演示常见坑）。
- 组织树里**已有**分公司下的部门（`org_unit`）；隔离靠 **Grant / 消费方 / Agent Profile 怎么切**，不是再开发一套 MCP。

**目标示例：** 十套系统里 2 套仅财务可见可调，5 套仅织造可见可调。

#### 做法一（推荐）：各部门单独消费方

1. 「组织与用户」确认部门节点已存在（如「财务部」「织造车间」挂在对应分公司下）。可选：把部门管理员做成 `company_admin` 生效在该子树。
2. 「消费方」新建两个应用，例如：
   - `finance-ai`（归属财务所在公司）
   - `weaving-ai`（归属织造所在公司；同公司也可，靠 Grant 区分）
3. 「授权上限」：
   - 给财务侧公司下发上限，**资产集合只勾财务 2 套**。
   - 给织造侧下发上限，**资产集合只勾织造 5 套**。（可同一分公司两条上限，或一条上限含多资产但下放时严格按集合授。）
4. 「授权」：
   - 财务 2 个资产 → Grant 给 `finance-ai`（`grantee_type=consumer`）。
   - 织造 5 个资产 → Grant 给 `weaving-ai`。
   - **不要**再把这 7 个都授给同一个联调 Token。
5. 财务 AI 工作台只配 `finance-ai` 的 Bearer；织造工作台只配 `weaving-ai`。
6. 两边都连同一个 `…:18082/mcp`，刷新列表：财务只见 2 个，织造只见 5 个。
7. 用错 Token 或 Grant 授错消费方时，列表会「看起来没隔离」——先查 Grant，再查工作台配置的 client。

#### 做法二：同一消费方，用 Agent Profile 拆

1. 适用：一个 AI 平台共用一个 `client_id`，但内有「财务 Agent / 织造 Agent」。
2. 控制台维护不同 **Agent Profile**。
3. Grant 的被授权方类型选 `agent_profile`，分别授财务工具 / 织造工具。
4. 工作台调用时必须带上对应 Agent 身份（平台 Principal 含 `agent_profile_id`），否则仍可能落到「整个 consumer 的并集」或不可见。
5. 适合平台型接入；联调阶段优先做法一更直观。

#### 做法三：应用层 + 用户层收窄（OBO）

1. 架构两层判定（architecture.md 4.2）：
   - **应用层**：必须有授给 consumer / agent_profile 的 Grant（否则工具进不了列表）。
   - **用户层（可选）**：若还配置了授给 `org_unit` / `user` 的 Grant，则**带员工身份**调用时还要命中部门/人。
2. 步骤概要：
   - 仍先给消费方做应用层 Grant（可先按业务线拆消费方，再叠加用户层）。
   - 再为「财务部」组织节点、`user` 等建收窄 Grant。
   - 消费方 `bind_policy` 允许用户断言；工作台用 jwt-bearer / 用户断言换平台令牌（**不能**指望纯应用 Token 自动按登录人藏工具）。
3. **纯应用身份**（现在多数工作台只有一把消费方 Bearer）→ **只走应用层**，不会按员工部门自动变列表。要「同连接不同人不同工具」，必须上 OBO。

#### 隔离验收（打勾）

```
[ ] 财务 Token 的 tools/list 只有财务相关 capability
[ ] 织造 Token 的 tools/list 只有织造相关 capability
[ ] 财务 Token 调织造工具 → 拒绝 / 不在列表
[ ] 未把全公司资产 Grant 给同一个演示用 client_id
```

---

## 阶段 H：在 AI 工作台验证 MCP

1. 保存工作台 MCP 配置后，**刷新工具列表**或重连。
2. 让 Agent：「列出当前 MCP 工具」。
3. 应能看到工具名 = Spec 的 `capability_id`（如 `facility.repair.data_report`）。
4. 让 Agent 调用该工具，参数可用 `{}`（以 input_schema 为准）。
5. 应返回业务数据，`status` 成功类结果；不应是 `PERMISSION_DENIED` / 工具不存在。

### 看不见工具时按序排查

1. `tools/list` 用的 token 是否就是 Grant 里的那个消费方？
2. Grant 的 env 是否与发布 env 一致？
3. 版本是否 `active` 且 release.exposure 含 `mcp`？
4. MCP URL 是否指向正在跑的数据面 `:18082`（含局域网 IP 与 `AIP_SERVE_HOST=0.0.0.0`）？
5. 控制台「授权」是否对该资产+该消费方有一条 active Grant？

---

## 阶段 I：同一系统继续加接口（可选）

1. 再走「简易接入」：第 1 步选「选用已有连接」。
2. 鉴权可跳过或复用已有凭据。
3. 第 3 步只添加**新**接口并点测。
4. 生成**新的** Tool 草稿（每个 operation 一条资产/版本）。
5. **每条新 Tool 都要重新走** 阶段 C → H（门禁、审批、发布、授权）。不能只登记接口就指望自动上 MCP。

---

## 一页纸检查清单（下一个系统打勾）

```
[ ] 0 环境 serve + 账号 TOTP + 系统信息纸面清单 + 鉴权选型（none/cookie 或 bearer/api_key/login）
[ ] A  或 A′：连接 + 下游凭据测通 + Tool 草稿（二选一，不要混用半截）
[ ] B  仅 http/insecure：非登记人 data_owner 批准传输例外 → active（正规 HTTPS 可跳过）
[ ] C1 draft：功能+权限用例，公司编码真实
[ ] C2 draft：验收单模板已存
[ ] C3 G0 提交 → testing
[ ] D  运行门禁 → in_review
[ ] E1 G5 业务验收通过
[ ] E2 G6 安全审核通过 → approved
[ ] F1 灰度（exposure 含 mcp）→ canary
[ ] F2 全量（本地可将 min_calls=0）→ active
[ ] G1 对「工作台消费方所属公司」下发含该资产的上限
[ ] G2 Grant 授给「工作台实际 client_id」，env 一致
[ ] G3 工作台 Bearer = 平台消费方 token（不是下游 JWT）
[ ] G4 若需部门隔离：分消费方（或 Agent Profile / OBO），勿把多业务线授给同一 Token
[ ] H  刷新 MCP 列表 → 可见 → 调用成功
```

---

## 常见错误对照

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 登记报公司+系统+环境重复 | 已登记过 | 选用已有连接 |
| 点测返回 HTML / not JSON | base_url 带了登录页路径，或未登录 | 改 base_url；cookie / login 鉴权 |
| system_key / body.system 非法 | 用了中文系统名 | 改英文标识 |
| 门禁 `TRANSPORT_EXCEPTION_EXPIRED` | HTTP 例外未批或连接非 active | 阶段 B |
| test-auth / 调用报下游登录失败 | path、账号或 body 不对 | 对照真实登录接口改 `login` 配置 |
| 「登录响应中没有令牌」 | `token_path` 与 JSON 不符 | 用真实响应改 JSONPath（如 `$.data.list.token`） |
| 401 刷新仍失败 | 密码错或票失效后无法再登录 | 更新 OpenBao 凭据；查 prepare/transform |
| 把下游 JWT 填进 AI 工作台 | 与平台消费方 Token 混淆 | 工作台只用消费方 Bearer；下游票只在连接凭据里 |
| 用例公司 `VN01` 参数无效 | 组织不存在 | 改为 HQ / VN-E41139 / DEMO |
| G5 待办为空 | BO 看不到该公司 | 给 BO 赋该厂 business_owner |
| 全量「调用 0 次」 | G8 指标未达标 | 本地 min_calls=0，或先授权再打满调用 |
| 授权上限下拉「暂无数据」 | 未下发上限，或页缓存 | 先下发上限；刷新后再开新建授权 |
| MCP 连上但看不到新工具 | Grant 授错消费方 | 授给工作台真实 client_id 后再刷新列表 |
| 各部门看到同一长串工具 | 共用同一消费方 Token，Grant 并集 | 阶段 G4：分消费方或 Agent Profile；勿全授给联调 Token |

---

## 相关文档

- 总设计：[architecture.md](../architecture.md)（门禁 G0–G8、传输例外、授权上限、§9.2 凭据）
- MCP 客户端联调：[client-compat.md](client-compat.md)
- 交接与本地命令：[handoff.md](handoff.md)
- Cookie / 简易接入：ADR-0028（`docs/adr/0028.md`）
- 登录换 JWT / RSA 握手：ADR-0027（`docs/adr/0027.md`）；演示 `tools/activate_equipment_path.py`
