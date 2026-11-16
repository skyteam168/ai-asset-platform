# 约 200 人试点上线说明（Wave P）

对照 [task.md](task.md) **Wave P**。本波目标：单机房、约 200 名控制台/业务用户先用起来；**不做** CNPG 真 PITR、k8s 多副本压测、双机房、正式 `production-checklist` 全勾。

## staging 是什么？

| 词 | 含义 |
| --- | --- |
| **dev** | 本机开发：`tools/dev.py up` + `serve`，可用 `aip-dev-only` 默认口令 |
| **staging** | **预发 / 准生产**：配置形态接近真实用户环境（`AIP_ENV=staging`），用来验证密钥、邮件、登录、部署；可以就是「给 200 人用的那台机」，不一定要单独再买一套 |
| **prod** | 正式生产（`AIP_ENV=prod`），规则与 staging 同样禁止 `dev-only` |

本试点建议：先把 **一台服务器（或一台足够强的开发机）** 配成 `AIP_ENV=staging`（或直接 `prod`），单机房、单套 Postgres/Valkey/OpenBao 即可。

## 如何安装（试点推荐路径）

### 路径 A：单机 Docker Compose（推荐起步）

1. 机器装 Docker Desktop / Docker Engine、Python 3.12、Node（仅构建控制台时需要）。
2. 克隆仓库，在仓库根目录：

```bash
uv sync
cd console && npm ci && npm run build && cd ..
.venv/Scripts/python tools/dev.py up          # 起 PG/Valkey/OpenBao/OPA/SeaweedFS + 迁移
# 准备试点环境变量（见下节），再：
$env:AIP_ENV='staging'   # PowerShell；禁止仍用默认 dev-only 口令
$env:AIP_SERVICE_AUTH_MODE='jwt'
$env:AIP_SIGNING_KEY_PEM=(Get-Content .aip-data\dev-signing-key.pem -Raw)  # 或正式密钥
.venv/Scripts/python tools/dev.py serve
```

3. 浏览器打开 `http://<主机>:18081/console/`。
4. 自检：`.venv/Scripts/python tools/pilot_check.py`（见 Wave P / P2）。

### 路径 B：Kubernetes（有集群时）

见 [k8s.md](k8s.md)：创建 `aip-secrets` → `kubectl apply -k deploy/k8s/overlays/prod`（或 base）→ 先跑 `aip-migrate`。  
**200 人试点不是必须上 K8s**；无集群时用路径 A。

### 路径 C：企业级 G6（本波不做）

CNPG + PITR、4 副本压测、双机房：见 [production-checklist.md](production-checklist.md) / Wave G6。明确延后。

## 试点必须配置（禁止默认口令）

`AIP_ENV=staging` 或 `prod` 时，Settings 会拒绝：

- URL/口令里仍含 `dev-only`
- 未配置 `AIP_SIGNING_KEY_PEM`（且未改 Transit）
- 仍用默认 `openbao_token` / `internal_service_token`
- `AIP_SERVICE_AUTH_MODE=static`

请为试点生成独立库口令、OpenBao token、服务令牌，并写入机器环境变量或 Docker Secret（**勿提交 Git**）。

## 用户登录（动态码）

1. 管理员在「组织与用户」创建账号。
2. 弹窗 **TOTP 绑定 URI 只显示一次** → 用户用 Authenticator 扫码。
3. 之后登录：用户名 + 密码 + App 动态码。  
本地脚本 `tools/dev_login.py` **仅开发机**，不要发给 200 名用户。

## 审批邮件（建议做）

控制台「邮件配置」或 `AIP_SMTP_*`；见 [smtp-staging.md](smtp-staging.md)。  
**若公司尚未提供 SMTP 主机/账号**：功能可用（审批进站内铃铛），邮件通道标为阻塞项，不挡登录与资产流程。

## 本波明确不做

| 项 | 原因 |
| --- | --- |
| CNPG / PITR 真恢复 | 需 staging kube + 对象存储；200 人可先单机 PG + 定期 `pg_dump` |
| k8s 4 副本压测 | 需集群；本机压测已有 |
| 双机房 | 集团未要求；overlay 仅草稿 |
| production-checklist 全勾 / go-live 仪式 | 企业级 G6 出口，非试点门槛 |
| 飞书 / OTel / 真 LLM（P7/P10/P11） | 可选；不挡上线 |
| 合规 Object Lock 桶运维开锁 | 代码已支持保留期头；桶侧开锁见 [audit-worm.md](audit-worm.md) |
