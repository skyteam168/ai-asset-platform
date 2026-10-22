# 发现检索与物化索引（architecture.md 10.3）

## 1. 现状（M7m + H41）

| 层 | 行为 |
| --- | --- |
| 发布快照 | 每条 release 带已 fold 的 `search_text`（capability_id / title / name / description / usage_notes / domain） |
| 数据面发现 | `search_capabilities` 在**本地快照**上评分（控制面宕机仍可用）；优先读 `search_text`，旧快照回退现算 |
| 分词 | 空白分词 + CJK **非重叠双字**；越文元 NFD 去调（unaccent 等价） |
| 实现 | [`src/aip/libs/searchtext.py`](../../src/aip/libs/searchtext.py) |
| 控制面目录 | `GET /api/v1/console/catalog?q=`：有 zhparser 时走 `registry.catalog_fts`；否则子串降级 |

默认开发库为 `postgres:16-alpine`，**未安装 zhparser**。发现仍走快照内存检索；控制面 `q` 无扩展时子串匹配。

## 2. pg_trgm（H17）

迁移 `0020` 执行 `CREATE EXTENSION IF NOT EXISTS pg_trgm`（alpine contrib 可用），为后续控制面目录相似度 / `%` 查询打地基。  
**尚未**挂 GIN 列到业务表；数据面发现不依赖该扩展。

## 3. zhparser opt-in（H41）

| 组件 | 路径 |
| --- | --- |
| 镜像 | [`deploy/Dockerfile.postgres-zhparser`](../../deploy/Dockerfile.postgres-zhparser)（`postgres:16-bookworm` + SCWS + zhparser） |
| Compose | [`deploy/docker-compose.zhparser.yml`](../../deploy/docker-compose.zhparser.yml) 覆盖 postgres 镜像 |
| 启动 | `python tools/dev.py zhparser-up`（重建 postgres 卷 → 构建 → migrate） |
| 迁移 | `20261022_0026_zhparser_fts`：仅当 `pg_available_extensions` 含 zhparser 时 `CREATE EXTENSION`、`chinese` 配置、`registry.catalog_fts` + GIN；alpine 为 NOTICE no-op |
| 代码 | [`src/aip/control_plane/registry/fts.py`](../../src/aip/control_plane/registry/fts.py) |
| 测例 | `tests/test_h41_zhparser.py`（`@pytest.mark.zhparser`；无扩展 skip） |

数据面发现**不**依赖库内 FTS（符合「先 Principal 过滤再检索」与离线快照）。

## 4. 与 architecture「PostgreSQL 全文检索」的关系

首期发现流量走数据面内存检索。库内 FTS 用于控制面大规模目录运维查询：

1. 扩展：`pg_trgm` 默认可用；中文分词用 **zhparser**（opt-in 镜像）。
2. 物化：`registry.catalog_fts.search_text` → generated `tsv`（`to_tsvector('chinese', …)`）。
3. 写入：控制台目录列表时 upsert；后续可与 registry 发布同事务。
4. 数据面不必依赖该表。

## 5. 验证

- 默认栈：`alembic upgrade head`（0026 no-op）+ `pytest -q tests/test_h41_zhparser.py`（zhparser 用例 skip）
- Opt-in：`tools/dev.py zhparser-up` 后 `pytest -q -m zhparser tests/test_h41_zhparser.py`
- 其它：`tests/test_m7_search_index.py`、`tests/test_m7_discovery.py`、`tests/test_h17_pg_fts_ha.py`
