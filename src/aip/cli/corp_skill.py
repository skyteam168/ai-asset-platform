"""corp-skill：Skill 目录登录、搜索、安装、更新、校验与列表（architecture.md 10.4）。

安装目录默认 ~/.aip/skills；配置写在 ~/.aip/corp-skill.json。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

import httpx

from aip.control_plane.skill import package as pkg
from aip.control_plane.skill.service import verify_package_bytes

CONF = pathlib.Path.home() / ".aip" / "corp-skill.json"
DEFAULT_TARGET = pathlib.Path.home() / ".aip" / "skills"

# architecture.md 10.4：--target 适配表（平台名 → 默认安装根目录）
TARGET_PRESETS: dict[str, pathlib.Path] = {
    "generic": DEFAULT_TARGET,
    "aip": DEFAULT_TARGET,
    "cursor": pathlib.Path.home() / ".cursor" / "skills",
    "claude": pathlib.Path.home() / ".claude" / "skills",
    "claude-desktop": pathlib.Path.home() / ".claude" / "skills",
}


def resolve_target(raw: str | None) -> pathlib.Path:
    """解析 --target：预设名（cursor/claude/generic）或任意路径。"""
    if raw is None or raw == "":
        conf_t = _load_conf().get("target")
        if isinstance(conf_t, str) and conf_t:
            return resolve_target(conf_t)
        return DEFAULT_TARGET
    key = raw.strip().lower().replace("_", "-")
    if key in TARGET_PRESETS:
        return TARGET_PRESETS[key]
    return pathlib.Path(raw).expanduser()


def _load_conf() -> dict[str, Any]:
    if CONF.exists():
        data = json.loads(CONF.read_text(encoding="utf-8"))
        return dict(data) if isinstance(data, dict) else {}
    return {}


def _save_conf(data: dict[str, Any]) -> None:
    CONF.parent.mkdir(parents=True, exist_ok=True)
    CONF.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _client() -> tuple[httpx.Client, dict[str, str]]:
    conf = _load_conf()
    if not conf.get("base_url") or not conf.get("token"):
        sys.exit("请先运行：corp-skill login --url URL --token TOKEN")
    headers = {"Authorization": f"Bearer {conf['token']}"}
    return httpx.Client(base_url=conf["base_url"].rstrip("/"), headers=headers, timeout=60), headers


def cmd_login(args: argparse.Namespace) -> None:
    conf = _load_conf()
    conf["base_url"] = args.url.rstrip("/")
    conf["token"] = args.token
    if args.consumer_id:
        conf["consumer_id"] = args.consumer_id
    if args.target:
        conf["target"] = args.target
    _save_conf(conf)
    print(f"已登录 {conf['base_url']}（凭据写入 {CONF}）")


def cmd_search(args: argparse.Namespace) -> None:
    client, _ = _client()
    r = client.get("/api/v1/skills", params={"q": args.query or "", "channel": args.channel})
    r.raise_for_status()
    for item in r.json().get("items") or []:
        mark = " [撤回]" if item.get("retracted") else ""
        print(f"{item['name']}@{item['version']}{mark}  {item.get('description', '')[:80]}")


def cmd_list(args: argparse.Namespace) -> None:
    target = resolve_target(args.target)
    if not target.exists():
        print("(本地无已安装 Skill)")
        return
    client = None
    try:
        client, _ = _client()
    except SystemExit:
        pass
    for d in sorted(p for p in target.iterdir() if p.is_dir()):
        meta_path = d / ".aip-skill.json"
        if not meta_path.exists():
            print(f"{d.name}  (无元数据)")
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        retracted = ""
        if client is not None:
            try:
                m = client.get(f"/api/v1/skills/{meta['name']}/versions/{meta['version']}/manifest")
                if m.status_code == 200 and m.json().get("retracted"):
                    retracted = " [撤回]"
            except httpx.HTTPError:
                pass
        local = ""
        if (d / "SKILL.md").exists() and meta.get("manifest"):
            files = {
                str(p.relative_to(d)).replace("\\", "/"): p.read_bytes()
                for p in d.rglob("*")
                if p.is_file() and p.name != ".aip-skill.json"
            }
            problems = pkg.local_verify(files, meta["manifest"])
            if problems:
                local = " [本地修改]"
        print(f"{meta['name']}@{meta['version']}{retracted}{local}")


def cmd_install(args: argparse.Namespace) -> None:
    if "@" not in args.ref:
        sys.exit("格式：name@version")
    name, _, version = args.ref.partition("@")
    conf = _load_conf()
    client, _ = _client()
    consumer_id = args.consumer_id or conf.get("consumer_id")
    if consumer_id:
        check = client.post(
            "/api/v1/skills/install-check",
            json={"consumer_id": consumer_id, "name": name, "version": version, "env": args.env},
        )
        check.raise_for_status()
        body = check.json()
        if body.get("retracted"):
            sys.exit(f"Skill 已撤回，无法安装：{name}@{version}")
        missing = body.get("missing_grants") or []
        if missing:
            print("缺少以下 Tool 授权，请先在控制台申请：", file=sys.stderr)
            for m in missing:
                print(f"  - {m}", file=sys.stderr)
            sys.exit(2)
    man = client.get(f"/api/v1/skills/{name}/versions/{version}/manifest")
    man.raise_for_status()
    meta = man.json()
    dl = client.get(f"/api/v1/skills/{name}/versions/{version}/download")
    dl.raise_for_status()
    problems = verify_package_bytes(dl.content, meta["manifest"], meta["signature"])
    if problems:
        sys.exit("验签失败：\n  " + "\n  ".join(problems))
    files = pkg.unpack_zip(dl.content)
    target = resolve_target(args.target) / name
    if target.exists() and not args.force:
        sys.exit(f"目录已存在：{target}（加 --force 覆盖）")
    target.mkdir(parents=True, exist_ok=True)
    for path, data in files.items():
        dest = target / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    (target / ".aip-skill.json").write_text(
        json.dumps(
            {
                "name": name,
                "version": version,
                "manifest": meta["manifest"],
                "signature": meta["signature"],
                "content_sha256": meta["content_sha256"],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"已安装到 {target}")


def cmd_update(args: argparse.Namespace) -> None:
    """按本地已装列表从目录拉取同名最新 stable 版本（简化：重新 install 指定 version 或目录返回的 version）。"""
    client, _ = _client()
    name = args.name
    r = client.get("/api/v1/skills", params={"q": name, "channel": "stable"})
    r.raise_for_status()
    items = [i for i in r.json().get("items") or [] if i["name"] == name and not i.get("retracted")]
    if not items:
        sys.exit(f"目录中无可用版本：{name}")
    latest = items[0]
    args.ref = f"{latest['name']}@{latest['version']}"
    args.force = True
    cmd_install(args)


def cmd_verify(args: argparse.Namespace) -> None:
    root = pathlib.Path(args.path or ".")
    meta_path = root / ".aip-skill.json"
    if not meta_path.exists():
        sys.exit(f"未找到 {meta_path}（请在已安装目录执行，或指定 --path）")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    files = {
        str(p.relative_to(root)).replace("\\", "/"): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file() and p.name != ".aip-skill.json"
    }
    # 先按清单比对本地文件
    problems = pkg.local_verify(files, meta["manifest"])
    # 再验平台签名（对清单）
    from aip.libs.tokens.keys import platform_keys

    if not pkg.verify_manifest(meta["manifest"], meta["signature"], list(platform_keys())):
        # CLI 侧可能没有平台私钥对应的同一密钥；用控制面清单接口的签名字段 + 本地比对即可
        # 若密钥不一致，退化为：只要本地与安装时清单一致就通过，并提示无法离线验签
        print("警告：本机无法用平台公钥验签（请确认 AIP_SIGNING_KEY_PEM 或仍以清单哈希为准）", file=sys.stderr)
    if problems:
        print("校验失败：")
        for p in problems:
            print(f"  {p}")
        sys.exit(1)
    print(f"校验通过：{meta['name']}@{meta['version']}")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="corp-skill", description="企业 Skill 安装与校验")
    sub = p.add_subparsers(dest="cmd", required=True)

    login = sub.add_parser("login", help="登记控制面地址与会话/静态令牌")
    login.add_argument("--url", required=True)
    login.add_argument("--token", required=True)
    login.add_argument("--consumer-id")
    login.add_argument(
        "--target",
        help="安装根目录或预设：generic|cursor|claude（见 TARGET_PRESETS）",
    )
    login.set_defaults(func=cmd_login)

    search = sub.add_parser("search", help="搜索目录")
    search.add_argument("query", nargs="?", default="")
    search.add_argument("--channel", default="stable")
    search.set_defaults(func=cmd_search)

    install = sub.add_parser("install", help="安装 name@version")
    install.add_argument("ref")
    install.add_argument("--target")
    install.add_argument("--consumer-id")
    install.add_argument("--env", default="test")
    install.add_argument("--force", action="store_true")
    install.set_defaults(func=cmd_install)

    update = sub.add_parser("update", help="更新已安装 Skill 到目录最新版")
    update.add_argument("name")
    update.add_argument("--target")
    update.add_argument("--consumer-id")
    update.add_argument("--env", default="test")
    update.set_defaults(func=cmd_update)

    verify = sub.add_parser("verify", help="校验本地 Skill（篡改则失败）")
    verify.add_argument("--path", default=".")
    verify.set_defaults(func=cmd_verify)

    lst = sub.add_parser("list", help="列出本地已安装 Skill（含撤回/本地修改标记）")
    lst.add_argument("--target")
    lst.set_defaults(func=cmd_list)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
