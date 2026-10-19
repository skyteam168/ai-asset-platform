# 里程碑验收记录

每个里程碑结束时对照 architecture.md 第 14 章逐条记录。命令均在仓库根目录执行。

## M0 基础与文档（P0a、P0）— 2026-10-19 通过

环境：Windows 11、Python 3.12.0、Docker 29.8.2、uv 0.11.14、Node 24。

| 验收项 | 结果 | 证据 |
| --- | --- | --- |
| docs 体系：architecture.md、ADR 0001 至 0025 与模板、conventions、error-codes、5 份 JSON Schema、2 份 OpenAPI | 通过 | `tests/test_m0_docs.py` 15 项通过 |
| JSON Schema 元校验与正反例（只读 Tool 通过；write 缺对账、danger 自动确认、未知字段被拒；http / pinned / insecure 连接配置正反例） | 通过 | 同上 |
| OpenAPI 3.1 校验、operationId 唯一、无 YAML 别名 | 通过 | openapi-spec-validator；spectral 0 error |
| openapi-generator 生成 Python 与 TypeScript client | 通过 | v7.10.0，两份契约 × 两种语言 4/4 成功 |
| 链接检查 | 通过 | `tools/check_links.py` |
| `python tools/dev.py up`：5 个基础设施容器启动，baseline 迁移到 head | 通过 | `alembic current` = 0001 (head)；`tests/test_m0_infra.py` 5 项通过 |
| 两个服务 /healthz 正常 | 通过 | uvicorn 实跑：控制面、数据面均 200 |
| 数据面无快照与策略时 /readyz 返回 503（fail closed） | 通过 | `tests/test_m0_apps.py` |
| 实现的路由都在 OpenAPI 契约中 | 通过 | `test_implemented_routes_are_in_contract` |
| 业务代码出现 verify=False / CERT_NONE 时 CI 失败 | 通过 | `tests/test_m0_tls_lint.py` 4 项通过（AST 检查 + ruff banned-api，httpclient 包豁免） |
| ruff、mypy --strict | 通过 | ruff 全部通过；mypy 15 个文件无问题 |
| 测试总数 | 34 通过，0 失败，0 跳过 | `python -m pytest -q` |

偏差与说明：

- Python 根包命名为 `aip`（`platform` 与标准库冲突），architecture.md 第 13 章已同步。
- 资源动作路径改为子路径动词（`/connections/{id}/test`），architecture.md 与 conventions.md 已同步。
- 本机无 make，提供 `tools/dev.py` 作为跨平台入口；Makefile 为薄封装。
- CI 工作流（`.github/workflows/ci.yml`）已编写，尚未在 GitHub 上实际运行；openapi-diff 只在 PR 上执行。
