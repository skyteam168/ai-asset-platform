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

## 服务间认证密钥（G2）

控制面与数据面必须共享同一签名材料，否则控制面签发的服务 JWT / 快照会被数据面拒绝（表现为 `/todo` 等下游 401）。

| 环境 | 配置 |
| --- | --- |
| 本地 `tools/dev.py serve` | 自动写入并注入 `AIP_SIGNING_KEY_PEM`（`.aip-data/dev-signing-key.pem`，已 gitignore）；两进程同 env |
| staging / prod（local 签名） | Secret 注入**同一** `AIP_SIGNING_KEY_PEM`（或 `signing_backend=transit` 时两端共用 Transit key） |
| 生产推荐 | `AIP_SERVICE_AUTH_MODE=jwt`（禁止仅靠长期静态令牌）；可选 `AIP_SERVICE_MTLS_CA_PEM` |

本地验证：`pytest -q tests/test_h44_service_auth.py::test_shared_signing_pem_mint_and_verify_across_settings`。  
逼近生产：`AIP_SERVICE_AUTH_MODE=jwt python tools/dev.py serve` 后，登录控制台确认待办角标非 401。

### 服务 mTLS（可选，G2.3）

当入口 / sidecar 已终止客户端证书时，把信任的 CA PEM 配到两端 `AIP_SERVICE_MTLS_CA_PEM`；`require_service_auth(..., client_cert_pem=...)` 会校验叶子证书由该 CA 签发。

可复现步骤（本地）：

1. 生成临时 CA + 客户端证书（测例已覆盖）：`pytest -q tests/test_h44_service_auth.py -k mtls`
2. 生产：由平台 PKI 签发服务身份；Secret 注入 `AIP_SERVICE_MTLS_CA_PEM`（仅 CA，不含私钥）。
3. 可选 Compose 骨架：`deploy/docker-compose.mtls-smoke.yml`（注释说明如何挂载 CA；默认不启动，避免强制证书依赖）。

单测冒烟：`test_mtls_smoke_ephemeral_ca`（无外部夹具时进程内生成 CA）。

## PostgreSQL（生产）

CloudNativePG 示例见 `deploy/k8s/cnpg/` 与 [pg-ha-prod.md](pg-ha-prod.md)；**不**纳入 `base` kustomization。

## 不在本片

- 真实集群部署与 e2e
- Helm chart / Gateway API / NetworkPolicy 全集
- 镜像构建推送流水线（仍由 Dockerfile + 外部 registry）
