# Staging SMTP（G4.1）

## 配置（Secret 注入，勿入库）

| 变量 | 说明 |
| --- | --- |
| `AIP_SMTP_HOST` / `AIP_SMTP_PORT` | 中继主机与端口（常用 587） |
| `AIP_SMTP_USER` / `AIP_SMTP_PASSWORD` | 登录凭据 |
| `AIP_SMTP_FROM` | 发件人 |
| `AIP_SMTP_STARTTLS` | 默认 true |

未配 `AIP_SMTP_HOST` 时使用 `LogMailChannel`（仅打日志）。

## 验收

1. Staging 发平台告警样例：  
   `AIP_TEST_SMTP_HOST=… AIP_TEST_SMTP_TO=… pytest -q -m smtp tests/test_h56_smtp_feishu.py`
2. 日志仅含 `to=` / `subject=` / `bytes=` / `err=<ExceptionType>`，**无密码**。
3. 瞬时失败自动重试最多 3 次（`SmtpMailChannel`）。
4. 脱敏收件证明放入 `.aip-data/staging-evidence/`（gitignore）。

## 清单

勾选 [production-checklist.md](production-checklist.md)「SMTP / 飞书」与「Notifier 环境变量」。
