# Staging / 生产 OIDC（Keycloak 等）联调（G3.1 / P8）

依据 [architecture.md](../architecture.md) §3.3：外部 IdP **只验证身份**；组织 / 角色 / 密级 **不得**从 token claim 写入 IAM。映射仅经 `external_login` → 已有 `iam_user`。

## 环境变量

| 变量 | 说明 |
| --- | --- |
| `AIP_OIDC_ENABLED=true` | 启用插件 |
| `AIP_OIDC_STUB=false` | staging/prod **必须** false |
| `AIP_OIDC_ISSUER` | 如 `https://idp.example/realms/aip` |
| `AIP_OIDC_CLIENT_ID` / `AIP_OIDC_AUDIENCE` | 控制台 confidential/public client |
| `AIP_OIDC_JWKS_URL` | `…/protocol/openid-connect/certs` |
| `AIP_OIDC_AUTHORIZATION_URL` | `…/protocol/openid-connect/auth`（控制台「企业登录」） |
| `AIP_OIDC_TOKEN_URL` | `…/protocol/openid-connect/token`（code 换票） |
| `AIP_OIDC_REDIRECT_URI` | 须与 IdP Valid redirect URIs 一致，如 `https://console…/login` |
| `AIP_OIDC_CLIENT_SECRET` | confidential client 密钥（Secret 注入，勿入库） |

## WebAuthn（可选第二因子）

| 变量 | 说明 |
| --- | --- |
| `AIP_WEBAUTHN_ENABLED=true` | 启用 |
| `AIP_WEBAUTHN_STUB=false` | staging/prod **必须** false |
| `AIP_WEBAUTHN_RP_ID` | 与控制台主机名一致（不含端口） |
| `AIP_WEBAUTHN_ORIGINS` | 逗号分隔，如 `https://console.example` |
| `AIP_WEBAUTHN_RP_NAME` | 显示名 |

组织页「安全密钥」在非 stub 下走 `navigator.credentials.create`，提交 SPKI + clientDataJSON。

## Keycloak 清单

1. Realm `aip`（或集团提供的 realm）。
2. Client：`aip-console`，Valid redirect URIs = `AIP_OIDC_REDIRECT_URI`。
3. 用户：仅创建 Keycloak 账号；在 AIP 用平台管理员 `POST /users/{id}/external-logins` 绑定 `provider=oidc` + Keycloak `sub`。
4. **不要**把 Keycloak realm role / group 同步进 AIP 授权。

## 验收

- JWKS：`pytest -q tests/test_g31_oidc_jwks.py`
- WebAuthn 非 stub：`pytest -q tests/test_p8_webauthn_crypto.py tests/test_p8_webauthn_api_crypto.py`
- Staging：控制台「企业登录」→ 会话；未绑定 → `USER_NOT_BOUND`
- 脱敏证据放 `.aip-data/staging-evidence/`（gitignore；勿提交截图中的 token）

## 回调

1. `POST /api/v1/auth/oidc/start` → `authorization_url` + PKCE `code_verifier`（浏览器 sessionStorage）。
2. IdP 回跳控制台 `/login?code=&state=`。
3. `POST /api/v1/auth/oidc/callback` 提交 `code` + `code_verifier`（服务端换 `id_token` 并 JWKS 校验），或直接提交已拿到的 `id_token`。
