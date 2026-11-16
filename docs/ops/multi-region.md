# 双机房 / 跨机架拓扑（G6.6 草稿）

对照 [architecture.md](../architecture.md) 与 ADR-0025：**平台不做跨境/驻留报表**；本文件只描述部署拓扑与切换演练入口。

> **待集团确认**：RPO / RTO、主备角色、DNS/入口切换责任人。确认前 overlay 仅作草稿，不得当作已上线双活。

## Overlay

| Overlay | 用途 | 构建 |
| --- | --- | --- |
| `deploy/k8s/overlays/region-a` | 主区示例（`AIP_REGION=region-a`） | `kubectl kustomize deploy/k8s/overlays/region-a` |
| `deploy/k8s/overlays/region-b` | 备区示例（`AIP_REGION=region-b`） | `kubectl kustomize deploy/k8s/overlays/region-b` |
| `deploy/k8s/overlays/prod` | 单区生产（数据面 4 副本） | `kubectl kustomize deploy/k8s/overlays/prod` |

本地校验：

```bash
kubectl kustomize deploy/k8s/overlays/region-a > /dev/null
kubectl kustomize deploy/k8s/overlays/region-b > /dev/null
# 或：.venv/Scripts/python tools/dev.py k8s-check
```

## 数据与身份

| 组件 | 建议 |
| --- | --- |
| 业务库 | 每区独立 CNPG；跨区以异步复制或按集团策略（**非**平台跨境报表） |
| 审计库 | 独立实例；WORM 桶按区或统一合规桶 |
| OpenBao / Valkey / OPA | 每区可达；密钥不进镜像 |
| 身份 | 仍只信 IAM；OIDC 插件不读取外部组织声明 |

## 切换演练（待确认后执行）

1. 冻结备区写入或确认只读流量。
2. 核对 RPO：备库延迟 ≤ 约定。
3. 切换入口 / DNS 到 region-b（或相反）。
4. 控制面 / 数据面 readyz；抽样 MCP 调用与审计链。
5. 记录写入 [evidence](evidence/) 与 [production-checklist.md](production-checklist.md)「双机房」。

## 不在本片

- 跨境数据报表（ADR-0025）
- 未获集团确认前的生产切流
