# 审计锚点 WORM 归档（H14）

依据 [architecture.md](../architecture.md) §12.4：每小时将各 shard 链头写入 `audit.audit_anchor` 后，同步到对象锁桶（不可覆写）。

## 行为

| 步骤 | 说明 |
| --- | --- |
| 锚定 | 数据面 `ship_audit` 按 `AIP_AUDIT_ANCHOR_INTERVAL_S`（默认 3600s）签名写入 `audit_anchor` |
| 同步 | `AIP_WORM_ENABLED=true` 时，经 `libs/storage` 以 `put_if_absent` 写入桶 |
| 对象键 | `audit-anchors/{shard_id}/{up_to_seq}.json` |
| 载荷 | `shard_id` / `up_to_seq` / `head_hash` / `signature` / `key_id` / `anchored_at` |
| 冲突 | 同键已存在且内容不同 → 失败（日志告警，不改库内锚点） |

关闭 `AIP_WORM_ENABLED`（默认）时仅写库表，与 H14 前行为一致。

## 本地启用

SeaweedFS 已随 `dev.py up` 启动（S3 `:58333`）。

```bash
set AIP_WORM_ENABLED=true
set AIP_WORM_BUCKET=aip-audit-worm
set AIP_S3_ENDPOINT=http://127.0.0.1:58333
set AIP_S3_ACCESS_KEY=aip
set AIP_S3_SECRET_KEY=aip-dev-only
python tools/dev.py serve
```

校验：单测 `tests/test_h14_worm.py`（内存存储 + 可选 SeaweedFS 往返）；库内锚点与对象一致性见 `verify_worm`。

## 模块

| 路径 | 用途 |
| --- | --- |
| `libs/storage/s3.py` | SigV4 path-style 客户端 |
| `libs/storage/worm.py` | `MemoryWormStore` / `S3WormStore`、`sync_anchors` / `verify_worm` |
| `libs/audit/store.anchor(..., worm=)` | 锚定后可选同步 |

## 不在本片

- SeaweedFS / RGW 原生 Object Lock 策略配置（合规桶）
- 控制台「WORM 校验」按钮
- 全量审计 JSONL 归档到对象存储（仅锚点）
