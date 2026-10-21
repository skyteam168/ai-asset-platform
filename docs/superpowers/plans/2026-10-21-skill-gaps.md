# Skill 缺口补齐 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按 architecture.md 6.2 / 7.1 / 10.4 补齐 Skill「封装→评测→上线→分发」相对 Tool 仍缺的能力，使分公司可用标准渠道下载并校验。

**Architecture:** Skill 仍是指令型（或经白名单的脚本型）Agent Skills 目录；门禁与 Tool 共用状态机；执行不在平台、只在消费方。G0 增加官方 `skills-ref` 校验；指导效果用评测集（对齐 H7 选择评测模式）；分发保留 zip + 增加 Git 镜像与 SDK 封装；对象存储迁 SeaweedFS。

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy async / Alembic / pytest；可选依赖 `skills-ref`（PyPI）；现有 `corp-skill` CLI；Python/TS SDK。

**Spec:** `docs/architecture.md` §6.2、§7.1、§10.4、§10.5；`docs/specs/skill-package.md`；对照已实现 M6c / H7 / H26。

## Global Constraints

- 设计唯一依据 `docs/architecture.md`；冲突先停，不改设计。
- **分公司命名空间 `company/{code}`：架构写明「预留，首期不开放」→ 本计划不做。**
- 错误码只用不在 docs/api/error-codes.md 之外的值；新增先登记。
- 新表：`company_id` + RLS + `aip_app` 授权 + `tests/iam_seed.py` truncate。
- 每切片：迁移 → 服务 → API → 测试 → `tools/gen_openapi.py` → milestones → 单独提交（用户要求时）。
- 控制台文案 `t("…")` + en-US / vi-VN。

## Review Focus

- skills-ref 不可用时 G0 不得静默跳过（应失败或显式降级策略写进验收）。
- 脚本白名单未批前仍硬拒；批后仅放行登记后缀与路径模式。
- Skill 评测不得在平台「执行」Skill，只测「给定场景选对 Tool / 遵循指引」。
- SeaweedFS 迁移后旧 bytea 行可读（双读或一次性迁）。
- Git 渠道不得泄露未授权 Skill 内容。

## 范围裁剪（对用户上次列出的「尚未做」）

| 项 | 本计划 | 理由 |
| --- | --- | --- |
| skills-ref validate（G0） | **做** H29 | architecture 7.1 / 10.4 |
| Skill 指导效果 / 选择评测 | **做** H30 | architecture 6.2；对齐 H7 |
| 带脚本 Skill 白名单 | **做** H31 | architecture 10.4 |
| SDK 封装 Skill API | **做** H32 | architecture 10.5 扩展；消费方下载/校验 |
| corp-skill `--target` 适配表 | **做** H33 | architecture 10.4 |
| Git 仓库分发渠道 | **做** H34 | architecture 10.4「Git + zip」 |
| SeaweedFS 存包 | **做** H35 | architecture 对象存储；现 bytea 为首期捷径 |
| 分公司命名空间发布 | **不做** | 架构「首期不开放」 |
| frontmatter 嵌套 metadata | **不做**（可另开） | H26 明示不在片；非阻塞分发 |
| Nike 演示 Skill 样例 | 附带 H30/H32 验收数据 | 便于 E2E |

## 文件地图（按切片）

| 切片 | 主要文件 |
| --- | --- |
| H29 | `skill/package.py`、`gates/service.py`、`pyproject.toml`（optional `skills-ref`）、`tests/test_h29_skills_ref.py` |
| H30 | 迁移 `skill_guidance_eval*`、`testlab/skill_eval.py`、`gates` composition、`Assets.tsx`、`tests/test_h30_skill_eval.py` |
| H31 | 迁移 `skill_script_allowlist`、`skill/service.py` 放行逻辑、审批 API、`tests/test_h31_script_allowlist.py` |
| H32 | `src/aip/sdk/skills.py`、`sdk/typescript`、`tests/test_h32_sdk_skills.py` |
| H33 | `corp_skill.py` + 配置 `skill_targets.toml` / 设置项、测试 |
| H34 | `skill/git_mirror.py`、发布钩子写 bare/mirror、`GET .../git-url`、测试 |
| H35 | `skill/storage.py`、迁移加 `object_key`、双读、测试 |

---

### Task 1: H29 G0 接入 skills-ref validate

**Files:**
- Modify: `pyproject.toml`（optional-dependencies `skills = ["skills-ref>=0.1.1"]`）
- Create: `src/aip/control_plane/skill/skills_ref.py`（解压到临时目录 → 调官方 validate API/CLI）
- Modify: `src/aip/control_plane/gates/service.py`（skill 分支 G0）
- Modify: `docs/specs/skill-package.md`
- Test: `tests/test_h29_skills_ref.py`
- OpenAPI：无新路由则只更新 milestones

- [x] Step 1: 写失败测试：合法包 G0 含 skills-ref 通过；故意坏 frontmatter → G0 失败且 problems 含 skills-ref
- [x] Step 2: 实现 `validate_skill_dir`；未安装 `skills-ref` 时 G0 problem 明确提示（CI/dev 装上 optional）
- [x] Step 3: `uv sync --extra skills`；跑通测试；更新 milestones H29

---

### Task 2: H30 Skill 指导效果评测（G2）

**Files:**
- Create: migration `0022_skill_guidance_eval.py`
- Create: `src/aip/control_plane/testlab/skill_eval.py`（场景 → 期望 capability_id；经 LLM Gateway；阈值对齐 H7 可略降条数或同 20）
- Modify: `gates/service.py` `_composition_lab` skill 分支跑评测
- Modify: API `GET/PUT /versions/{id}/skill-eval`（或复用 selection-eval 并扩展 asset type=skill）
- Modify: console `Assets.tsx` Skill 版本 Tab
- Test: `tests/test_h30_skill_eval.py`

- [x] Step 1: 复用 H7 表结构模式，skill 专用 cases（question / expect_capability_id）
- [x] Step 2: 无评测集或未达阈值 → G2 失败；脚本化 LLM → 通过
- [x] Step 3: 控制台可编辑；i18n；milestones H30

---

### Task 3: H31 脚本 Skill 白名单

**Files:**
- Create: migration `skill_script_allowlist`（company_id, path_glob/suffix, version_id 或 asset_id, approved_by, expires_at）
- Modify: `skill/package.py`：有有效白名单条目时允许对应后缀
- Modify: API 申请/审批（data_owner）；G0 记录白名单证据
- Test: `tests/test_h31_script_allowlist.py`

- [x] Step 1: 无白名单 + `.py` → 仍拒
- [x] Step 2: 审批通过后同包可 attach；篡改路径不在白名单 → 拒
- [x] Step 3: milestones H31

---

### Task 4: H32 SDK Skill 客户端

**Files:**
- Create: `src/aip/sdk/skills.py`（search / install_check / download / verify_local）
- Modify: `src/aip/sdk/__init__.py`、`sdk/typescript` 对等 API
- Test: `tests/test_h32_sdk_skills.py` + TS vitest

- [ ] Step 1: Python 客户端对接现有 `/skills*` 
- [ ] Step 2: TS 同步
- [ ] Step 3: milestones H32

---

### Task 5: H33 corp-skill `--target` 适配表

**Files:**
- Create: `docs/specs/skill-targets.md` + 默认 `deploy/skill-targets.example.toml`
- Modify: `src/aip/cli/corp_skill.py`（`--target cursor|claude|generic` → 目录）
- Test: `tests/test_h33_skill_targets.py`

---

### Task 6: H34 Git 分发渠道

**Files:**
- Create: `src/aip/control_plane/skill/git_export.py`（发布 active 时导出到配置的 git-data 目录 / 返回 clone URL）
- Modify: promote/release 钩子或显式 `POST /skills/{name}/versions/{ver}/publish-git`
- Modify: list/manifest 增加 `git_url`（若启用）
- Test: `tests/test_h34_skill_git.py`（本地 bare repo）

**设计锁定（本计划内）：** 平台侧维护 **bare repo 镜像**（非托管 GitHub）；消费方 `git clone` 与 zip 内容哈希一致。不引入外部 GitHub App。

---

### Task 7: H35 SeaweedFS 存包

**Files:**
- Modify: `skill` 表或 package 行加 `object_key`；`attach_package` 写 S3；`download` 双读（object 优先，回落 bytea）
- Migration + 可选迁移动脚本
- Test: 用现有 storage fixture

---

## 执行顺序与验收总闸

1. H29 → H30 → H31 → H32 → H33 → H34 → H35  
2. 每片独立可演示；全做完后：控制台建 Nike 演示 Skill → 评测通过 → Active → `corp-skill install` / SDK download → verify OK。

## 不在本计划

- 改 MCP `name` 为中文  
- 开放 `company/{code}` 发布  
- 拖拽 Skill 市场页（架构「后做」）
