# 企业 AI 资产管理中台（AIP）

旧系统与各类 Agent 之间的“转换插头”加资产治理底座。唯一设计依据：[docs/architecture.md](docs/architecture.md)。

## 快速开始

前置：Docker、[uv](https://docs.astral.sh/uv/)、Python 3.12、Node 22（控制台与 spectral）。

```bash
uv sync                          # 安装依赖到 .venv
python tools/dev.py up           # 启动 PostgreSQL / Valkey / OpenBao / OPA / SeaweedFS 并迁移
python tools/dev.py ci           # 文档契约 + lint + 全部测试
python tools/dev.py bootstrap    # 首次运行：建集团、总部与管理员，凭据写入 .aip-data/dev-admin.txt
python tools/dev.py serve        # 控制面 :18081，数据面 :18082
python tools/smoke.py            # 另开终端：对运行中的系统做端到端冒烟
```

控制台（React + Ant Design）在 `console/`，构建产物由控制面在 `/console/` 托管：

```bash
cd console && npm ci && npm run build   # 之后 serve 起来即可访问 http://127.0.0.1:18081/console/
npm run dev                              # 开发模式 :5173，/api 代理到 :18081
npm test                                 # 组件测试（vitest）
npm run e2e                              # 浏览器端到端（Playwright，需先 serve，凭据读 .aip-data/dev-admin.txt）
```

Linux / macOS 可用 `make up`、`make ci` 等同名任务。Windows 下用 `.venv\Scripts\python tools\dev.py <task>`。

## 目录

| 路径 | 内容 |
| --- | --- |
| `src/aip/libs` | 共享库：common（错误码、配置、web 装配）、tokens、httpclient（唯一允许关闭证书校验的位置） |
| `src/aip/control_plane` | 控制面：IAM、Registry、Release、Grant、连接、审批、审计 |
| `src/aip/data_plane` | 数据面：Gateway（MCP + REST + OAuth）、Runtime、PDP 客户端 |
| `console/` | 控制台：组织与用户、消费方与绑定、系统连接与连接测试、资产门禁与审批、授权、审计 |
| `migrations/` | Alembic（异步），expand / contract 规则 |
| `deploy/` | docker-compose（dev），后续 Helm / Kustomize |
| `docs/` | 架构、ADR、OpenAPI 契约、JSON Schema、运维与里程碑记录 |
| `tools/` | 生成器（ADR、Schema、OpenAPI）与检查脚本 |

## 规则

开发规则见 [.cursor/rules/platform.mdc](.cursor/rules/platform.mdc)（来自 architecture.md 15.2）。里程碑验收记录见 [docs/ops/milestones.md](docs/ops/milestones.md)。
