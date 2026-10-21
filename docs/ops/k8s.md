# Kubernetes 清单（H16）

依据 [architecture.md](../architecture.md) §12.1。本仓库提供 kustomize base + prod overlay；**不**在仓库内提交 `aip-secrets`（由 OpenBao 注入或运维创建）。

## 布局

| 路径 | 内容 |
| --- | --- |
| `deploy/k8s/base/` | Namespace（restricted）、ConfigMap、控制面 Deployment、数据面 StatefulSet + OPA sidecar、迁移 Job、Service、PDB |
| `deploy/k8s/overlays/prod/` | `AIP_ENV=prod`、`AIP_RUNTIME_MODE=live`、数据面副本 4 |

镜像占位名 `aip`，由 kustomize `images` 改写为 `registry.internal/aip/platform:0.1.0`。

## 本地校验（无需集群）

```bash
python tools/dev.py k8s-check
# 或
kubectl kustomize deploy/k8s/base
kubectl kustomize deploy/k8s/overlays/prod
```

单测：`tests/test_h16_k8s.py`（无 kubectl 则 skip）。CI 通过 `azure/setup-kubectl` 后跑同一检查。

## 部署前运维清单

1. 创建 Secret `aip-secrets`（库连接、OpenBao token、服务令牌等），勿写入 Git。
2. `kubectl apply -k deploy/k8s/overlays/prod`（或 staging 用 base）。
3. 先跑 Job `aip-migrate`，再扩控制面 / 数据面。
4. 数据面 `/readyz` 在快照与策略未齐前保持未就绪（fail closed）。

## 不在本片

- 真实集群部署与 e2e
- Helm chart / Gateway API / NetworkPolicy 全集
- 镜像构建推送流水线（仍由 Dockerfile + 外部 registry）
