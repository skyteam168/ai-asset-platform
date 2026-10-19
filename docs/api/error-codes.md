# 错误码登记表

代码只能使用本表登记的错误码；新增先在此登记并同步 `src/aip/libs/common/errors.py`（测试会校验两者一致）。

| code | HTTP | retryable | 类别 | 含义 |
| --- | --- | --- | --- | --- |
| INVALID_ARGUMENT | 400 | false | 参数 | 参数不符合 Schema 或业务格式 |
| IDEMPOTENCY_CONFLICT | 409 | false | 参数 | 同一幂等键对应不同参数 |
| PRECONDITION_FAILED | 412 | false | 参数 | If-Match 不匹配 |
| NOT_FOUND | 404 | false | 参数 | 资源不存在或不可见 |
| UNAUTHENTICATED | 401 | false | 认证 | 缺少或无效的凭据 |
| TOKEN_EXPIRED | 401 | false | 认证 | 平台令牌已过期 |
| ASSERTION_INVALID | 401 | false | 认证 | 用户断言签名、格式或 aud 不正确 |
| ASSERTION_EXPIRED | 401 | false | 认证 | 用户断言已过期或有效期超过上限 |
| ASSERTION_REPLAYED | 401 | false | 认证 | 用户断言 jti 已使用 |
| MFA_REQUIRED | 401 | false | 认证 | 需要 TOTP |
| ACCOUNT_LOCKED | 423 | false | 认证 | 连续失败被锁定 |
| PERMISSION_DENIED | 403 | false | 权限 | 授权判定不通过 |
| USER_NOT_BOUND | 403 | false | 权限 | 消费方侧用户未绑定到平台用户 |
| OUT_OF_SCOPE | 403 | false | 权限 | 参数超出用户数据范围 |
| CLASSIFICATION_DENIED | 403 | false | 权限 | 有效密级不足 |
| CONFIRMATION_REQUIRED | 202 | false | 确认 | 需要用户确认或审批 |
| CONFIRMATION_MISMATCH | 409 | false | 确认 | 确认的用户或参数与冻结内容不一致 |
| RATE_LIMITED | 429 | true | 限流 | 超出限流或配额 |
| DOWNSTREAM_AUTH_EXPIRED | 502 | true | 下游 | 下游认证过期，刷新后可重试一次 |
| DOWNSTREAM_UNAVAILABLE | 503 | true | 下游 | 下游 5xx 或连接失败 |
| DOWNSTREAM_TIMEOUT | 504 | true | 下游 | 下游超时 |
| TLS_UNTRUSTED | 502 | false | 传输 | 证书链不受信任 |
| TLS_EXPIRED | 502 | false | 传输 | 证书已过期 |
| TLS_HOSTNAME_MISMATCH | 502 | false | 传输 | 主机名与证书不符 |
| TLS_PIN_MISMATCH | 502 | false | 传输 | 证书指纹与登记值不符 |
| TLS_FINGERPRINT_CHANGED | 502 | false | 传输 | insecure 连接的证书指纹发生变化（阻断模式） |
| SCHEME_MISMATCH | 500 | false | 传输 | base_url 协议与连接 scheme 不一致 |
| REDIRECT_BLOCKED | 502 | false | 传输 | 下游重定向被拒绝（降级或跨主机） |
| TRANSPORT_NOT_ALLOWLISTED | 502 | false | 传输 | http / insecure 连接的目标地址不在内网白名单 |
| TRANSPORT_EXCEPTION_EXPIRED | 502 | false | 传输 | 传输例外已过期，连接已禁用 |
| BUSINESS_RULE_VIOLATION | 422 | false | 业务 | 下游业务规则拒绝 |
| OUTCOME_UNKNOWN | 202 | false | 业务 | 写操作结果未知，进入对账 |
| CIRCUIT_OPEN | 503 | false | 保护 | 熔断中 |
| MAINTENANCE_WINDOW | 503 | false | 保护 | 维护窗口内 |
| SANDBOX_WRITE_BLOCKED | 403 | false | 保护 | 沙箱模式拒绝 live 写操作 |
| POLICY_UNAVAILABLE | 503 | false | 平台 | PDP 或快照不可用，默认拒绝 |
| INTERNAL | 500 | false | 平台 | 平台内部错误 |
