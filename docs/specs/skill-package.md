# Skill 包目录约定（architecture.md 6.2、10.4）

Skill 包是标准 Agent Skills 目录打成的 zip，带清单哈希与平台签名分发。上传 zip（`POST /api/v1/versions/{id}/skill-package`）与控制台在线编辑（`PUT /api/v1/versions/{id}/skill-source`）产出同一种包，入库、签名、写回 spec 走同一段代码（`skill/service.py::attach_package`）。元数据字段的 JSON Schema 见 [skill-spec.schema.json](skill-spec.schema.json)。

## 目录

```text
<name>/
├── SKILL.md            # 必需：YAML frontmatter + Markdown 正文
├── skill.lock.json     # 可选：平台私有的 Tool@版本引用
├── references/         # 可选：文本参考资料（md、markdown、txt、json、yaml、yml、csv）
└── assets/             # 可选：小附件（单个 ≤ 1MB）
```

zip 根目录可以直接是这些文件，也可以多包一层 `<name>/`（入库时剥掉）。入库后统一按路径排序重新打包，内容哈希对重新打包后的字节计算。

## SKILL.md frontmatter

| 键 | 必需 | 约束 | 写入 spec |
| --- | --- | --- | --- |
| `name` | 是 | 平台允许 `^[a-z][a-z0-9_-]{1,63}$`；**G0 另跑官方 skills-ref validate**（规范更严：名称宜用连字符 `-`，目录名须与 name 一致） | `name`；`capability_id = skill.global.{name}`（`-` 换成 `_`） |
| `description` | 是 | 10–1024 字符，单行 | `description` |
| `license` | 否 | ≤ 200 字符，单行 | `license` |
| `compatibility` | 否 | ≤ 500 字符，单行 | `compatibility` |
| `allowed-tools` | 否 | 空格分隔，每项不含空白 | `allowed_tools`（数组） |

解析只取单行标量；值含 `:`、`#`、引号等字符时，在线编辑器输出 JSON 风格的双引号字符串（同时是合法的 YAML 双引号标量），解析端按 JSON 字符串还原。嵌套的 `metadata` 首期不解析。

## skill.lock.json

```json
{ "tools": ["production.mes.query_work_orders@1.0.0"] }
```

每项须为 `capability_id@x.y.z`。G0 检查每个引用都能解析到已登记的 Tool 版本；`corp-skill install` 据此检查消费方是否已获授权。

## 指令型与脚本白名单

默认不发带脚本的 Skill（10.4）：路径以 `.py`、`.sh`、`.bash`、`.ps1`、`.js`、`.mjs`、`.cjs`、`.exe`、`.bat`、`.cmd` 结尾会被拒；路径不得为绝对路径或含 `..`。在线编辑器只允许写入上表中的四类文件，`references/` 只收文本。

带脚本的包须先走白名单（H31）：

1. `POST /api/v1/versions/{id}/skill-script-allowlist`（资产开发者）申请 `path_globs`（如 `scripts/*.py`）
2. `POST /api/v1/skill-script-allowlist/{entry_id}/decide`（数据 Owner）审批
3. 再 `POST .../skill-package` 上传；未命中有效白名单的脚本路径仍硬拒
4. G0 证据含 `script_allowlist`；过期条目不再生效

## Git 分发（H34）

配置 `AIP_SKILL_GIT_ROOT` 后，Active/Canary 版本可 `POST /api/v1/skills/{name}/versions/{version}/publish-git` 写入平台侧 **bare repo**；`GET .../git-url` 返回 clone 地址（`AIP_SKILL_GIT_PUBLIC_BASE` 非空时为 URL，否则为本地路径）。目录中 `git_url` 字段仅在镜像已发布时出现。不引入外部 GitHub App。

## 对象存储（H35）

`AIP_SKILL_OBJECT_STORAGE=true` 时入库双写 SeaweedFS（`AIP_SKILL_BUCKET`，默认 `aip-skills`）与 `zip_bytes`；下载优先 `object_key`，失败回落 bytea。旧行无 `object_key` 时行为不变。

## 签名与校验

- 清单 = 每个文件相对路径 → sha256；签名 = 平台密钥对 `canonical_json(清单)` 的 ECDSA（`package.py::sign_manifest`）。
- 在线保存时额外返回一次安全扫描预检（隐藏字符、诱导越权措辞、外链），正式判定在 G0 提交时（architecture.md 7.1）。
- G0 在扫描正文后调用 `skills-ref`（PyPI 包，见 `skill/skills_ref.py`）：未安装依赖时 G0 失败并提示安装，不得跳过。
- `corp-skill verify` 比对本地文件哈希，改过的文件标“本地修改”。

## 编辑器往返

`GET /api/v1/versions/{id}/skill-source` 把已入库的包拆回表单（frontmatter、正文、Tool 引用、references、assets），可作为新版本的起点再保存。编辑器不承载的文件（如根目录 README.md）在 `warnings` 中列出，再次在线保存时不会保留；需要保留时改用上传 zip。
