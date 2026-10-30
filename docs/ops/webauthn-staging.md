# WebAuthn 安全密钥（G3.3）

## 启用

```bash
AIP_WEBAUTHN_ENABLED=true
AIP_WEBAUTHN_STUB=true          # 本地测例；真密钥器校验 attestation/assertion 后改 false
AIP_WEBAUTHN_RP_ID=localhost
AIP_WEBAUTHN_RP_NAME=AIP Console
```

## 流程

1. 登录后 `POST /users/{id}/webauthn/register/begin` → 浏览器 `navigator.credentials.create`
2. `POST …/register/finish` 提交 `credential_id` + COSE 公钥（stub 不验 attestation）
3. 登录第二因子：`POST /auth/webauthn/authenticate/begin` → `get` → `finish`（带 `sign_count`）
4. **`sign_count` 必须严格大于库内值**，否则 `UNAUTHENTICATED`（重放拒绝）

## 测例

```bash
pytest -q tests/test_h53_webauthn.py
```

真器 / Playwright 虚拟 authenticator：在 stub=false 接上签名校验后再做 e2e；当前仓库以 stub + sign_count 门禁满足 G3.3 防重放验收。
