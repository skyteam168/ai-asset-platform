# Nike Postman → Active → Grant → MCP 全流程

> 演示路径，不改架构。金标对照：`tests/test_m4_e2e.py`。

## 为何导入后「尚未可用」

Importer 只落 **Tool 草稿**（`state=draft`）+ declarative Connector。Agent/MCP 要看到并调用，还必须：

1. 门禁 G0→G8 发布到 `active`（含 Test Lab）
2. 发布快照被数据面拉取
3. 消费方 + 授权上限 + Grant
4. release.`exposure` 含 `mcp` / `rest`

真机 `http://10.10.131.188:7001` 另需传输例外审批；演示用 **Mock Connector** 跑通同一条门禁链，不依赖内网。

## 步骤（脚本自动化）

| # | 动作 | 接口 / 说明 |
| --- | --- | --- |
| 0 | 选 3 个 GET 只读导入资产 | `getHomeData` / `getProductList` / `getMachineData` |
| 1 | 建门禁角色用户 | `dev` / `bo` / `security`（若无） |
| 2 | Mock Connector `nike_demo` | `POST /connector-instances` `kind=mock` |
| 3 | 每个资产新建 `1.0.0` 版本 | 指向 mock；`app_identity_allowed=true` |
| 4 | 用例 + 验收单 | functional + permission（`granted:false`） |
| 5 | G0 submit → G1–G4 run-tests | |
| 6 | G5 BO + G6 data_owner 审批 | 不同人 |
| 7 | G7 canary → G8 promote | `exposure: [rest,mcp]`，`min_calls:0` |
| 8 | ceiling + grant → `demo-agent` | |
| 9 | REST invoke + MCP `tools/call` | 数据面 `:18082` |

## 运行

```bash
# 需先 serve；已有 Nike 导入草稿与 seed-demo（demo-agent）
.venv/Scripts/python tools/activate_nike_path.py
```

产物：`.aip-data/nike-path.json`（资产、能力 ID、调用样例）。

## 外部 Agent 工作台如何调用

数据面默认：`http://127.0.0.1:18082`（生产换成可达的网关地址）。消费方用 `demo-agent`（`seed-demo` 签发的静态令牌在 `.aip-data/demo-m5.json` 的 `token` 字段，形如 `aipk_…`；**勿提交到 git**）。

已授权能力（H28）：`nike.sample.app_13`、`nike.sample.pc_12`、`nike.sample.app_8`。

### 方式 A：MCP（推荐，Cursor / Claude Desktop / Inspector）

1. Transport：**Streamable HTTP**
2. URL：`http://127.0.0.1:18082/mcp`
3. Header：`Authorization: Bearer <demo-agent 的 static token>`
4. 先 `tools/list`，再 `tools/call`，`name` = capability_id，`arguments` = `{}`

Cursor `mcp.json` 示例（按所用客户端字段微调）：

```json
{
  "mcpServers": {
    "aip-nike": {
      "url": "http://127.0.0.1:18082/mcp",
      "headers": {
        "Authorization": "Bearer aipk_替换为demo-m5.json里的token"
      }
    }
  }
}
```

说明见 [client-compat.md](client-compat.md)。MCP **不要**用模式 B（`X-User-Assertion` 复用会撞 jti）；应用身份直接 Bearer 静态令牌即可（本演示 Tool 已开 `app_identity_allowed`）。

### 方式 B：REST

```http
POST http://127.0.0.1:18082/api/v1/invoke/nike.sample.app_13
Authorization: Bearer aipk_…
Content-Type: application/json

{"args": {}}
```

### 方式 C：官方 Python SDK

```python
from aip.sdk import Client

async with Client("http://127.0.0.1:18082", static_token="aipk_…") as c:
    r = await c.invoke("nike.sample.app_13", {})
    print(r)
```

员工身份（五方交集完整路径）需：登记 JWK → 绑定员工 → jwt-bearer 换 `access_token` → 再调 MCP/REST；分公司系统见 `sdk/consumer-kit/`。
