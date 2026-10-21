# 发现检索与物化索引（architecture.md 10.3）

## 1. 现状（M7m）

| 层 | 行为 |
| --- | --- |
| 发布快照 | 每条 release 带已 fold 的 `search_text`（capability_id / title / name / description / usage_notes / domain） |
| 数据面发现 | `search_capabilities` 在**本地快照**上评分（控制面宕机仍可用）；优先读 `search_text`，旧快照回退现算 |
| 分词 | 空白分词 + CJK **非重叠双字**；越文 NFD 去调（unaccent 等价） |
| 实现 | [`src/aip/libs/searchtext.py`](../../src/aip/libs/searchtext.py) |

默认开发库为 `postgres:16-alpine`，**未安装 zhparser / pg_jieba**。本片不改默认镜像、不加空闲 FTS 表。

## 2. 与 architecture「PostgreSQL 全文检索」的关系

首期发现流量走数据面内存检索，以满足「先 Principal 过滤再检索」与离线快照。库内 FTS 适合控制面大规模目录运维查询；接入时建议：

1. `CREATE EXTENSION`：优先 `pg_trgm`（alpine 可用）；中文分词再考虑 **zhparser** 或 **pg_jieba**（需非 alpine 镜像或自建镜像）。
2. 物化列可复用快照字段语义：`search_text` 或 `to_tsvector('jiebacfg', …)`。
3. GIN 索引；写入点与 `registry` 发布同一事务。
4. 数据面**不必**依赖该表做发现（避免控制面/库故障影响调用）。

## 3. 生产启用 zhparser（可选）

仅在确认镜像含扩展后：

```sql
CREATE EXTENSION IF NOT EXISTS zhparser;
-- 按发行版配置 text search configuration 后建 tsvector 列与 GIN
```

换镜像后需重跑迁移与回归；CI 默认栈保持 alpine + 本片内存方案。

## 4. 验证

- 单测：`tests/test_m7_search_index.py`、`tests/test_m7_discovery.py`
- 手工：发现模式搜「工单」「查询工单」「nghi phep」；danger / 未授权不可见
