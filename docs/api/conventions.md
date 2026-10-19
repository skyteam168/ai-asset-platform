# API 约定

适用于控制面（control-plane.openapi.yaml）与数据面（data-plane.openapi.yaml）的全部 REST 接口。MCP 出口遵循 MCP 规范，不受本文件约束，但错误码共用 [error-codes.md](error-codes.md)。

## 1. 传输与版本

- 只提供 HTTPS（TLS 1.2+），不监听 HTTP 端口（architecture.md 9.6）。
- 路径前缀带主版本：`/api/v1/...`；只增不改，破坏性变更开 `/api/v2`。
- 对资源的动作用子路径动词：`POST /connections/{id}/test`、`POST /confirmations/{id}/confirm`。
- 请求与响应体为 `application/json; charset=utf-8`；字段名 snake\_case。

## 2. 认证

- 控制台：`Authorization: Bearer <session_token>`（30 分钟滑动过期）。
- 消费方：`Authorization: Bearer <access_token>`（/oauth/token 签发）或静态 API Token；用户身份可在兼容模式下用 `X-User-Assertion`。
- 令牌不得放在 URL 查询参数中。

## 3. 错误（RFC 9457 Problem Details）

所有非 2xx 响应使用 `application/problem+json`：

```json
{
  "type": "https://errors.<域名>/PERMISSION_DENIED",
  "title": "Permission denied",
  "status": 403,
  "code": "PERMISSION_DENIED",
  "detail": "该消费方未获得此资产的授权",
  "instance": "/api/v1/invoke/production.mes.query_work_orders",
  "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
  "errors": [{"field": "args.factory_id", "code": "OUT_OF_SCOPE", "message": "不在数据范围内"}]
}
```

- `code` 必须是 error-codes.md 中登记的值。
- detail 面向人，不泄露旧系统内部细节（主机、堆栈、SQL）。

Tool 调用接口（`/invoke`）返回统一执行结果（architecture.md 8.2），业务失败也用 200 + `status=failed`；只有认证失败、请求格式错误、平台故障才用非 2xx。

## 4. 分页

游标分页：请求 `?limit=50&cursor=<opaque>`，响应：

```json
{"items": [], "next_cursor": "eyJpZCI6...", "has_more": true}
```

limit 默认 50，最大 200；cursor 不透明，不可解析，有效期 24 小时。

## 5. 幂等

- 所有创建类 POST 与写类 Tool 调用支持 `Idempotency-Key` 头（1 到 128 个可打印 ASCII 字符）。
- 同键同参数在 24 小时内返回首次结果；同键不同参数返回 409 `IDEMPOTENCY_CONFLICT`。

## 6. 时间与时区

- 所有时间为 RFC 3339 UTC，带 `Z`，例如 `2026-10-19T02:00:00Z`。
- 显示时区由客户端按用户 timezone 转换，接口不返回本地时间。

## 7. 并发控制

可修改资源返回 `ETag`，更新时用 `If-Match`，不匹配返回 412 `PRECONDITION_FAILED`。

## 8. 追踪

接受并透传 W3C `traceparent` / `tracestate`；所有响应带 `X-Trace-Id`。

## 9. 标识

资源 id 为 UUIDv7 字符串；业务编码（org code、capability\_id）只用于展示与查找，不作为外键。
