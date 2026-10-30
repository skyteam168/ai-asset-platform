# Staging OIDC（Keycloak）联调（G3.1）

依据 [architecture.md](../architecture.md) §3.3：外部 IdP **只验证身份**；组织 / 角色 / 密级 **不得**从 token claim 写入 IAM。映射仅经 `external_login` → 已有 `iam_user`。

## 环境变量

| 变量 | 说明 |
| --- | --- |
| `AIP_OIDC_ENABLED=true` | 启用插件 |
| `AIP_OIDC_STUB=false` | staging/prod **必须** false |
| `AIP_OIDC_ISSUER` | 如 `https://idp.example/realms/aip` |
| `AIP_OIDC_CLIENT_ID` / `AIP_OIDC_AUDIENCE` | 控制台 confidential/public client |
| `AIP_OIDC_JWKS_URL` | `…/protocol/openid-connect/certs` |
| `AIP_OIDC_TOKEN_URL` | （可选）授权码换 token |
| `AIP_OIDC_CLIENT_SECRET` | confidential client 密钥（Secret 注入，勿入库） |

## Keycloak 清单

1. Realm `aip`（或集团提供的 realm）。
2. Client：`aip-console`，Valid redirect URIs = 控制台回调（如 `https://console…/oidc/callback`）。
3. 用户：仅创建 Keycloak 账号；在 AIP 用平台管理员 `POST /users/{id}/external-logins` 绑定 `provider=oidc` + Keycloak `sub`。
4. **不要**把 Keycloak realm role / group 同步进 AIP 授权。

## 验收

- 本地 JWKS 冒烟：`pytest -q tests/test_g31_oidc_jwks.py`
- Staging：浏览器完成登录 → 会话 `aips…`；未绑定用户 → `USER_NOT_BOUND`
- 脱敏证据放 `.aip-data/staging-evidence/`（gitignore；勿提交截图中的 token）

## 回调

当前 `POST /api/v1/auth/oidc/callback` 接受已拿到的 `id_token`（由前端或 BFF 完成 code 交换后提交）。后续可在同端点扩展 `code` + `code_verifier` 服务端换票。
