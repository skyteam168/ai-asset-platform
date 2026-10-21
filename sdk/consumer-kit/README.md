# consumer-kit：消费方断言签名示例

对齐 [architecture.md](../../docs/architecture.md) §10.5 / P13：分公司系统用私钥签发 **用户断言**，再以 `jwt-bearer` 换平台 `access_token`。

官方 REST SDK（`aip.sdk` / `aip-sdk`）负责调用业务 API；本目录只提供 **签发断言 + 换令牌** 的最小样例。

| 语言 | 路径 | CI |
|------|------|-----|
| Python | [`python/`](python/) | pytest |
| TypeScript | [`typescript/`](typescript/) | vitest |
| Java | [`java/AssertionExample.java`](java/AssertionExample.java) | 无（单文件模板） |
| .NET | [`dotnet/AssertionExample.cs`](dotnet/AssertionExample.cs) | 无（单文件模板） |

## Claims 约定

用户断言（**ES256**，`kid` 在 JWT header）：

| Claim | 含义 |
|-------|------|
| `iss` | `client_id`（如 `vn-ai`） |
| `sub` | 外部主体（工号 / 绑定键） |
| `aud` | `mcp_resource`；模式 A 换令牌时可用 `token_endpoint_aud` |
| `iat` / `exp` | 有效期 **≤ 5 分钟** |
| `jti` | **每次新随机**（防重放） |

客户端断言：`sub` = `iss` = `client_id`，`aud` = token endpoint audience。

换令牌：`POST /oauth/token`，`grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer`，附 `assertion` 与 `client_assertion`（或静态令牌，视租户配置）。

## 安全注意

- **禁止**将用户断言、`client_assertion`、完整 `access_token` 写入日志、审计或提交到版本库。
- 示例脚本只打印 `access_token` **前缀**。
- 私钥仅留在消费方；平台只登记公钥 JWK（`kid` / `x` / `y`）。

## 快速开始（Python）

```bash
cd sdk/consumer-kit/python
pip install -e ".[dev]"   # 或 uv pip install -e .
pytest
# 联调（需已登记公钥与 client）
export AIP_BASE_URL=http://127.0.0.1:18081
export AIP_CLIENT_ID=vn-ai AIP_SUBJECT=emp-1 AIP_AUDIENCE=... AIP_KID=... AIP_PRIVATE_PEM_PATH=./key.pem
python examples/exchange_demo.py
```

## 快速开始（TypeScript）

```bash
cd sdk/consumer-kit/typescript
npm ci && npm test && npm run build
# npm run example:exchange  # 同上环境变量
```

## 与官方 SDK 的分工

1. 本 kit 签发断言并换得 `access_token`。
2. 将令牌交给 `aip.sdk.Client` / `aip-sdk` 的 `Authorization: Bearer`，再调 REST / MCP。
