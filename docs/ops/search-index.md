# 发现检索与物化索引（architecture.md 10.3）

## 1. 现状（M7m）

| 层 | 行为 |
| --- | --- |
| 发布快照 | 每条 release 带已 fold 的 `search_text`（capability_id / title / name / description / usage_notes / domain） |
| 数据面发现 | `search_capabilities` 在**本地快照**上评分（控制面宕机仍可用）；优先读 `search_text`，旧快照回退现算 |
| 分词 | 空白分词 + CJK **非重叠双字**；越文 NFD 去调（unaccent 等价） |
| 实现 | [`src/aip/libs/searchtext.py`](../../src/aip/libs/searchtext.py) |

默认开发库为 `postgres:16-alpine`，**未安装 zhparser / pg_jieba**。发现仍走快照内存检索。

## 2. pg_trgm（H17）

迁移 `0020` 执行 `CREATE EXTENSION IF NOT EXISTS pg_trgm`（alpine contrib 可用），为后续控制面目录相似度 / `%` 查询打地基。  
**尚未**挂 GIN 列到业务表；数据面发现不依赖该扩展。

## 3. 与 architecture「PostgreSQL 全文检索」的关系

首期发现流量走数据面内存检索，以满足「先 Principal 过滤再检索」与离线快照。库内 FTS 适合控制面大规模目录运维查询；接入时建议：

1. 扩展：已启用 `pg_trgm`；中文分词再考虑 **zhparser** 或 **pg_jieba**（需非 alpine 镜像或自建镜像）。
2. 物化列可复用快照字段语义：`search_text` 或 `to_tsvector('jiebacfg', …)`。
3. GIN 索引；写入点与 `registry` 发布同一事务。
4. 数据面**不必**依赖该表做发现（避免控制面/库故障影响调用）。

## 4. 生产启用 zhparser（可选）

仅在确认镜像含扩展后：

```sql
CREATE EXTENSION IF NOT EXISTS zhparser;
-- 按发行版配置 text search configuration 后建 tsvector 列与 GIN
```

换镜像后需重跑迁移与回归；CI 默认栈保持 alpine + 快照内存方案。

## 5. 验证

- 单测：`tests/test_m7_search_index.py`、`tests/test_m7_discovery.py`、`tests/test_h17_pg_fts_ha.py`（`pg_trgm`）
- 手工：发现模式搜「工单」「查询工单」「nghi phep」；danger / 未授权不可见
