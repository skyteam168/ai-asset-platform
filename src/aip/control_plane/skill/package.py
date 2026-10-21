"""Skill 包解析与验签（标准 Agent Skills 目录：SKILL.md + 可选 references/、assets/）。

清单 = 每个文件的相对路径 → sha256；签名 = 平台密钥对 canonical_json(manifest) 的 ECDSA。
默认只接受指令型包：拒绝常见脚本后缀（带脚本需额外审查，首期硬拒）。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import re
import zipfile
from typing import Any

from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.tokens.keys import SigningKey, canonical_json
from aip.libs.tokens.signed import sign_bytes, verify_bytes

SKILL_MD = "SKILL.md"
LOCK_FILE = "skill.lock.json"
# 首期默认不发带脚本的 Skill（architecture.md 10.4）
BLOCKED_SUFFIX = frozenset({".py", ".sh", ".bash", ".ps1", ".js", ".mjs", ".cjs", ".exe", ".bat", ".cmd"})
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """解析 SKILL.md 顶部 YAML frontmatter（只取标量字符串键，避免引入运行时 pyyaml 依赖）。"""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "SKILL.md frontmatter 未正确结束")
    block = text[3:end].strip("\n")
    body = text[end + 4 :].lstrip("\n")
    meta: dict[str, str] = {}
    for line in block.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key, val = key.strip(), val.strip()
        if val.startswith('"') and val.endswith('"') and len(val) >= 2:
            # 在线编辑器写出的双引号标量按 JSON 字符串解码（JSON 字符串同时是合法的 YAML 双引号标量）
            try:
                val = str(json.loads(val))
            except json.JSONDecodeError:
                val = val.strip('"')
        else:
            val = val.strip("'")
        if key:
            meta[key] = val
    return meta, body


def build_manifest(files: dict[str, bytes]) -> dict[str, str]:
    return {path: hashlib.sha256(data).hexdigest() for path, data in sorted(files.items())}


def sign_manifest(manifest: dict[str, str], key: SigningKey) -> str:
    return sign_bytes(canonical_json(manifest), key)


def verify_manifest(manifest: dict[str, str], signature: str, keys: list[SigningKey]) -> bool:
    return verify_bytes(canonical_json(manifest), signature, keys)


def _flatten_root(paths: list[str]) -> list[str]:
    """若所有条目共享同一顶层目录（且该目录不是 references/assets），则剥掉。"""
    tops = {p.split("/")[0] for p in paths if p}
    if len(tops) == 1:
        root = next(iter(tops))
        if root not in ("references", "assets") and any("/" in p for p in paths):
            return ["/".join(p.split("/")[1:]) for p in paths]
    return paths


def unpack_zip(raw: bytes) -> dict[str, bytes]:
    """解压 zip 为 path→bytes；拒绝路径穿越、绝对路径与脚本后缀。"""
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as e:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "不是合法的 zip 包") from e
    entries: list[tuple[str, bytes]] = []
    for info in zf.infolist():
        if info.is_dir():
            continue
        name = info.filename.replace("\\", "/")
        if name.startswith("/") or ".." in name.split("/"):
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, f"非法路径：{name}")
        parts = [p for p in name.split("/") if p]
        path = "/".join(parts)
        if info.file_size > 2 * 1024 * 1024:
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, f"单文件过大：{path}")
        entries.append((path, zf.read(info)))
    flat_paths = _flatten_root([p for p, _ in entries])
    files: dict[str, bytes] = {}
    for path, data in zip(flat_paths, (d for _, d in entries), strict=True):
        if not path:
            continue
        lower = path.lower()
        for suf in BLOCKED_SUFFIX:
            if lower.endswith(suf):
                raise PlatformError(ErrorCode.INVALID_ARGUMENT, f"首期不接受带脚本的 Skill：{path}")
        files[path] = data
    if SKILL_MD not in files:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "Skill 包必须包含 SKILL.md")
    return files


def validate_and_describe(files: dict[str, bytes]) -> dict[str, Any]:
    """校验 frontmatter 与 lock，返回写入 asset_version.spec 的字段。"""
    text = files[SKILL_MD].decode("utf-8")
    meta, body = parse_frontmatter(text)
    name = meta.get("name", "").strip()
    description = meta.get("description", "").strip()
    if not NAME_RE.match(name):
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "SKILL.md 缺少合法的 name（frontmatter）")
    if len(description) < 10:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "SKILL.md 的 description 至少 10 个字符")
    tools: list[str] = []
    if LOCK_FILE in files:
        try:
            lock = json.loads(files[LOCK_FILE].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, "skill.lock.json 不是合法 JSON") from e
        raw_tools = lock.get("tools") or lock.get("tool_refs") or []
        if not isinstance(raw_tools, list):
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, "skill.lock.json 的 tools 须为数组")
        tools = [str(t) for t in raw_tools]
    for ref in tools:
        if "@" not in ref or not re.match(r"^[a-z0-9_.]+@\d+\.\d+\.\d+$", ref):
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, f"非法 Tool 引用：{ref}")
    described: dict[str, Any] = {
        "name": name,
        "description": description,
        "namespace": "global",
        "capability_id": f"skill.global.{name.replace('-', '_')}",
        "license": meta.get("license"),
        "compatibility": meta.get("compatibility"),
        "tools": tools,
        "body_preview": body[:500],
    }
    if meta.get("allowed-tools"):
        # Agent Skills 规范：allowed-tools 为空格分隔的列表
        described["allowed_tools"] = meta["allowed-tools"].split()
    return described


def make_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path, data in sorted(files.items()):
            zf.writestr(path, data)
    return buf.getvalue()


def local_verify(root_files: dict[str, bytes], manifest: dict[str, str]) -> list[str]:
    """本地 verify：返回问题列表；空列表表示完好。改过的文件标「本地修改」。"""
    problems: list[str] = []
    current = build_manifest(root_files)
    for path, digest in manifest.items():
        if path not in current:
            problems.append(f"缺失文件：{path}")
        elif current[path] != digest:
            problems.append(f"本地修改：{path}")
    for path in current:
        if path not in manifest:
            problems.append(f"多余文件：{path}")
    return problems


# ---------- 在线编辑：表单内容 ⇄ 标准 Agent Skills 目录 ----------

REFERENCE_SUFFIX = frozenset({".md", ".markdown", ".txt", ".json", ".yaml", ".yml", ".csv"})
MAX_ASSET_BYTES = 1024 * 1024
_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_PLAIN = re.compile(r"^[^\s\-?:,\[\]{}#&*!|>'\"%@`][^:#\"'\\]*$")
_SINGLE_LINE_MAX = {"description": 1024, "license": 200, "compatibility": 500}


def _scalar(value: str) -> str:
    """frontmatter 标量：安全时原样输出，否则输出 JSON 双引号字符串（YAML 兼容，parse_frontmatter 可还原）。"""
    if _PLAIN.match(value) and value == value.strip():
        return value
    return json.dumps(value, ensure_ascii=False)


def _check_path(path: str, prefix: str) -> str:
    parts = path.replace("\\", "/").split("/")
    if parts[0] != prefix or len(parts) < 2 or not all(_SEGMENT.match(x) for x in parts[1:]):
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, f"非法文件路径：{path}（须位于 {prefix}/ 下）")
    lower = path.lower()
    if any(lower.endswith(suf) for suf in BLOCKED_SUFFIX):
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, f"首期不接受带脚本的 Skill：{path}")
    return "/".join(parts)


def build_source_files(source: dict[str, Any]) -> dict[str, bytes]:
    """把编辑器提交的 frontmatter、正文、Tool 引用与 references/assets 组装为标准目录（path→bytes）。

    只产出指令型 Skill：SKILL.md、可选 skill.lock.json、references/ 文本、assets/ 小附件；不接受脚本。
    """
    name = str(source.get("name") or "").strip()
    if not NAME_RE.match(name):
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "name 须为小写字母开头的 2–64 位标识（a-z、0-9、-、_）")
    fields: list[tuple[str, str]] = [("name", name)]
    for key in ("description", "license", "compatibility"):
        val = str(source.get(key) or "").strip()
        if not val:
            continue
        if "\n" in val or "\r" in val:
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, f"{key} 须为单行文本")
        if len(val) > _SINGLE_LINE_MAX[key]:
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, f"{key} 不能超过 {_SINGLE_LINE_MAX[key]} 个字符")
        fields.append((key, val))
    allowed = [str(x).strip() for x in source.get("allowed_tools") or [] if str(x).strip()]
    if any(re.search(r"\s", x) for x in allowed):
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "allowed-tools 的每一项不能包含空白")
    if allowed:
        fields.append(("allowed-tools", " ".join(allowed)))
    front = "\n".join(f"{k}: {_scalar(v)}" for k, v in fields)
    body = str(source.get("body") or "").strip("\n")
    files: dict[str, bytes] = {SKILL_MD: f"---\n{front}\n---\n\n{body}\n".encode()}
    tools = [str(x).strip() for x in source.get("tools") or [] if str(x).strip()]
    if tools:
        files[LOCK_FILE] = (json.dumps({"tools": sorted(set(tools))}, ensure_ascii=False, indent=2) + "\n").encode()
    for ref in source.get("references") or []:
        path = _check_path(str(ref.get("path") or ""), "references")
        if not any(path.lower().endswith(suf) for suf in REFERENCE_SUFFIX):
            raise PlatformError(
                ErrorCode.INVALID_ARGUMENT, f"references/ 只接受文本文件（md、txt、json、yaml、csv）：{path}"
            )
        if path in files:
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, f"文件重复：{path}")
        files[path] = str(ref.get("content") or "").encode()
    for asset in source.get("assets") or []:
        path = _check_path(str(asset.get("path") or ""), "assets")
        try:
            data = base64.b64decode(str(asset.get("content_base64") or ""), validate=True)
        except (binascii.Error, ValueError) as e:
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, f"附件不是合法的 base64：{path}") from e
        if len(data) > MAX_ASSET_BYTES:
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, f"附件不能超过 1MB：{path}")
        if path in files:
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, f"文件重复：{path}")
        files[path] = data
    return files


def read_source(files: dict[str, bytes]) -> dict[str, Any]:
    """把已入库的包拆回编辑器表单；编辑器不承载的文件放进 warnings（再次保存时不会保留）。"""
    meta, body = parse_frontmatter(files[SKILL_MD].decode("utf-8"))
    tools: list[str] = []
    if LOCK_FILE in files:
        try:
            lock = json.loads(files[LOCK_FILE].decode("utf-8"))
            tools = [str(x) for x in (lock.get("tools") or lock.get("tool_refs") or [])]
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            tools = []
    references: list[dict[str, str]] = []
    assets: list[dict[str, Any]] = []
    warnings: list[str] = []
    for path, data in sorted(files.items()):
        if path in (SKILL_MD, LOCK_FILE):
            continue
        if path.startswith("references/") and any(path.lower().endswith(s) for s in REFERENCE_SUFFIX):
            try:
                references.append({"path": path, "content": data.decode("utf-8")})
                continue
            except UnicodeDecodeError:
                pass
        elif path.startswith("assets/"):
            assets.append({"path": path, "content_base64": base64.b64encode(data).decode(), "size": len(data)})
            continue
        warnings.append(f"编辑器不保留此文件，再次保存会移除：{path}")
    return {
        "name": meta.get("name", ""),
        "description": meta.get("description", ""),
        "license": meta.get("license") or None,
        "compatibility": meta.get("compatibility") or None,
        "allowed_tools": (meta.get("allowed-tools") or "").split(),
        "body": body,
        "tools": tools,
        "references": references,
        "assets": assets,
        "warnings": warnings,
    }
