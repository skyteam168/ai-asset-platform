# authorization_code + PKCE 浏览器同意（G3.2）

## 启用

```bash
AIP_OAUTH_PKCE_ENABLED=true
AIP_OAUTH_PKCE_STUB=false   # 强制走同意页；stub=true 时仍可用 POST /oauth/authorize JSON 骨架
```

## 浏览器流

1. 客户端生成 `code_verifier` / S256 `code_challenge`。
2. 打开  
   `GET {issuer}/oauth/authorize?response_type=code&client_id=…&redirect_uri=…&code_challenge=…&code_challenge_method=S256&subject=…&state=…`
3. 用户点「同意」→ `POST /oauth/authorize/consent` → **302** 到 `redirect_uri?code=…&state=…`。
4. 用 `code` + `code_verifier` + client_assertion 调 `POST /oauth/token`。
5. 授权码一次性；缺 `code_verifier` → 400；错误 verifier / 重放 → 401。

## 测例

`pytest -q tests/test_h52_pkce.py::test_browser_consent_redirect_and_token`

## 说明

- `subject` 为消费方侧员工标识，经 `user_binding` 映射到 `iam_user`（与 stub 一致）。
- 生产同意页可替换为带会话登录的控制台页；本端点为最小可用 HTML。
