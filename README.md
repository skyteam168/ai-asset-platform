# 企业 AI 资产管理中台（AIP）

旧系统与各类 Agent 之间的“转换插头”加资产治理底座。唯一设计依据：[docs/architecture.md](docs/architecture.md)。

## 快速开始

前置：Docker、[uv](https://docs.astral.sh/uv/)、Python 3.12、Node（可选，用于 spectral）。

```bash
uv sync                          # 安装依赖到 .venv
python tools/dev.py up           # 启动 PostgreSQL / Valkey / OpenBao / OPA / SeaweedFS 并迁移
python tools/dev.py ci           # 文档契约 + lint + 全部测试
python tools/dev.py serve        # 控制面 :18081，数据面 :18082
```

Linux / macOS 可用 `make up`、`make ci` 等同名任务。Windows 下用 `.venv\Scripts\python tools\dev.py <task>`。

## 目录

| 路径 | 内容 |
| --- | --- |
| `src/aip/libs` | 共享库：common（错误码、配置、web 装配）、tokens、httpclient（唯一允许关闭证书校验的位置） |
| `src/aip/control_plane` | 控制面：IAM、Registry、Release、Grant、连接、审批、审计 |
| `src/aip/data_plane` | 数据面：Gateway（MCP + REST + OAuth）、Runtime、PDP 客户端 |
| `migrations/` | Alembic（异步），expand / contract 规则 |
| `deploy/` | docker-compose（dev），后续 Helm / Kustomize |
| `docs/` | 架构、ADR、OpenAPI 契约、JSON Schema、运维与里程碑记录 |
| `tools/` | 生成器（ADR、Schema、OpenAPI）与检查脚本 |

## 规则

开发规则见 [.cursor/rules/platform.mdc](.cursor/rules/platform.mdc)（来自 architecture.md 15.2）。里程碑验收记录见 [docs/ops/milestones.md](docs/ops/milestones.md)。
