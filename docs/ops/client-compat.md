# MCP 客户端兼容矩阵（architecture.md 10.1）

维护说明：新增客户端或协议版本时先补本表与 `tests/test_m7_client_compat.py`，再升级 MCP SDK。

## 1. 自动化冒烟（CI）

| 客户端画像 | 协议 | 连通方式 | 自动化证据 |
| --- | --- | --- | --- |
| 官方 MCP Python SDK | 2025-11-25（legacy）与 2026-07-28 | Streamable HTTP + 平台访问令牌 | `test_m7_client_compat.py::test_official_sdk_smoke`、`test_m4_mcp.py` |
| MCP Inspector 画像 | 2026-07-28 | 原始 JSON-RPC + `Mcp-Method` / `Mcp-Name` | `test_m7_client_compat.py::test_inspector_http_smoke` |
| 桌面 Agent 画像（Claude Desktop / Cursor 类） | 2025-11-25 | `initialize` 握手后 tools/list·call（无 `_meta` 版本） | `test_m7_client_compat.py::test_desktop_legacy_http_smoke` |

冒烟路径（每个画像）：未认证 401 + RFC 9728 质询 → 令牌交换 → tools/list 含授权工具且不含未授权 → tools/call 读类成功。

## 2. 官方 MCP Inspector（人工 / 联调）

1. `python tools/dev.py up && python tools/dev.py serve`（数据面默认 `:18082`）。
2. 为联调消费方签发静态令牌并完成 jwt-bearer 令牌交换，得到平台 `access_token`（**不要**把同一 `X-User-Assertion` 复用到 MCP 多请求上，见 architecture.md 3.1）。
3. 启动 Inspector，Transport 选 Streamable HTTP，URL：`http://127.0.0.1:18082/mcp`（或套件 `/mcp/bundles/{name}`、发现 `/mcp/discovery`）。
4. 认证头：`Authorization: Bearer <access_token>`。
5. 验收：List Tools 可见已授权能力；Call 读类 Tool 返回 structuredContent；未授权名称为 Unknown tool。

## 3. 已知限制

| 项 | 说明 |
| --- | --- |
| 模式 B 请求头断言 | MCP 客户端会复用同一组头 → 断言 jti 冲突；MCP **必须用模式 A**（平台访问令牌） |
| SSE / 双向流 | 网关当前以 JSON-RPC 请求-响应为主；Inspector 的 Streamable HTTP POST 路径已覆盖 |
| A2A | 见 `a2a_gateway`；不在本矩阵（另表） |

## 4. 版本巡检

每季度对照 MCP / Agent Skills / A2A 发布说明跑本仓库 `test_m4_mcp` + `test_m7_client_compat`，通过后再升级依赖。
