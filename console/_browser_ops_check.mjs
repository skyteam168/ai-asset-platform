// 浏览器验收：草稿丢弃/资产删除、连接与实例删除、弃用/退役
// 会话：API 登录写入 sessionStorage（与表单登录等价，避开 TOTP 同窗与 Password 控件抖动）
import { chromium, expect } from "@playwright/test";
import { readFileSync, mkdirSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { spawnSync } from "node:child_process";
import { TOTP } from "otpauth";

const root = resolve(import.meta.dirname, "..");
const out = resolve(root, ".tmp/browser-ops");
mkdirSync(out, { recursive: true });
const text = readFileSync(resolve(root, ".aip-data/dev-admin.txt"), "utf-8");
const login = /^login_name=(.*)$/m.exec(text)[1].trim();
const password = /^password=(.*)$/m.exec(text)[1].trim();
const secret = /secret=([A-Z2-7]+)/.exec(text)[1];

async function freshCode() {
  let left = 30 - ((Date.now() / 1000) % 30);
  if (left < 12) await new Promise((r) => setTimeout(r, (left + 1) * 1000));
  return new TOTP({ secret }).generate();
}

const report = [];
const ok = (name, detail = "") => report.push({ name, ok: true, detail });
const fail = (name, detail) => report.push({ name, ok: false, detail });
const shot = async (page, name) => page.screenshot({ path: resolve(out, name), fullPage: true });

const browser = await chromium.launch({ channel: "chrome", headless: false, slowMo: 40 });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, locale: "zh-CN" });
const base = "http://127.0.0.1:18081";

try {
  // ---- 注入会话 ----
  let token = null;
  for (let i = 0; i < 3; i++) {
    const code = await freshCode();
    const res = await page.request.post(`${base}/api/v1/auth/login`, {
      data: { login_name: login, password, totp: code },
    });
    if (res.ok()) {
      token = (await res.json()).session_token;
      break;
    }
    const body = await res.text();
    if (i === 2) throw new Error(`API 登录失败: ${res.status()} ${body}`);
    await new Promise((r) => setTimeout(r, 2000));
  }
  await page.goto(`${base}/console/`);
  await page.evaluate((t) => sessionStorage.setItem("aip.session", t), token);
  await page.reload();
  await expect(page.getByRole("button", { name: "账号菜单" })).toBeVisible({ timeout: 20000 });
  await expect(page.getByText("平台管理员").first()).toBeVisible({ timeout: 10000 });
  // 确认 token 仍在（排除 /me 401 清会话）
  const kept = await page.evaluate(() => sessionStorage.getItem("aip.session"));
  if (!kept) throw new Error("登录后 sessionStorage 被清空");
  writeFileSync(resolve(out, "post-login.txt"), await page.evaluate(() => document.body.innerText.slice(0, 3000)));
  await shot(page, "01-logged-in.png");
  ok("会话注入并进入控制台");

  const run = Date.now().toString(36);
  const go = async (path) => {
    await page.goto(`${base}/console/#${path}`);
    await page.waitForTimeout(600);
  };

  // ---------- 草稿删除 ----------
  await go("/assets");
  await expect(page.getByRole("button", { name: "新建资产" })).toBeVisible({ timeout: 15000 });
  await shot(page, "01b-assets-page.png");
  await page.getByRole("button", { name: "新建资产" }).click();
  const assetName = `browser.ops.${run}`;
  await page.getByLabel("名称").fill(assetName);
  await page.getByLabel("归属组织").click({ force: true });
  await page.locator(".ant-select-dropdown:visible .ant-select-item-option").first().click();
  await page.getByRole("button", { name: /确\s*定/ }).click();
  await expect(page.getByText(assetName).first()).toBeVisible({ timeout: 15000 });
  await page.getByText(assetName).first().click();
  await page.getByRole("button", { name: "新建版本" }).click();
  await page.getByRole("button", { name: /确\s*定/ }).click();
  await expect(page.locator(".ant-drawer .ant-table-row").first()).toBeVisible({ timeout: 15000 });
  await page.locator(".ant-drawer .ant-table-row").first().click();
  await expect(page.getByRole("button", { name: "丢弃草稿" })).toBeVisible({ timeout: 10000 });
  await shot(page, "02-draft-panel.png");
  await page.getByRole("button", { name: "丢弃草稿" }).click();
  await page.locator(".ant-modal-confirm").getByRole("button", { name: /确\s*定/ }).click();
  await expect(page.getByText("已丢弃草稿")).toBeVisible({ timeout: 10000 });
  ok("UI 丢弃草稿版本");

  await page.getByRole("button", { name: "删除资产" }).click();
  await page.locator(".ant-modal-confirm").getByRole("button", { name: /确\s*定/ }).click();
  await expect(page.getByText("已删除资产")).toBeVisible({ timeout: 10000 });
  await shot(page, "03-asset-deleted.png");
  ok("UI 删除未发布资产");

  // ---------- 连接删除 ----------
  await go("/connections");
  await expect(page.getByRole("button", { name: "登记连接" })).toBeVisible({ timeout: 15000 });
  await page.getByRole("button", { name: "登记连接" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("公司").click({ force: true });
  await page.locator(".ant-select-dropdown:visible .ant-select-item-option").first().click();
  const sysKey = `bops_${run}`;
  await dialog.getByLabel("系统").fill(sysKey);
  await dialog.getByLabel("地址").fill("https://browser-ops.example.test");
  await dialog.getByRole("button", { name: /确\s*定/ }).click();
  await expect(page.getByText(sysKey).first()).toBeVisible({ timeout: 15000 });
  ok("UI 登记系统连接");

  await page.getByRole("tab", { name: "连接器实例" }).click();
  await page.getByRole("button", { name: "新建连接器实例" }).click();
  const instDlg = page.getByRole("dialog");
  const instName = `bops_inst_${run}`;
  await instDlg.getByLabel("名称").fill(instName);
  // Mock 无需绑定系统连接，避免新建连接后列表未刷新导致下拉缺项
  await instDlg.getByLabel("类型").click({ force: true });
  await page.locator(".ant-select-dropdown:visible .ant-select-item-option").filter({ hasText: /^Mock$/ }).first().click();
  await instDlg.getByRole("button", { name: /确\s*定/ }).click();
  await expect(page.getByText(instName).first()).toBeVisible({ timeout: 15000 });
  await shot(page, "04-connector-created.png");

  await page.getByRole("row").filter({ hasText: instName }).getByRole("button", { name: "删除" }).click();
  await page.locator(".ant-modal-confirm").getByRole("button", { name: /确\s*定/ }).click();
  await expect(page.getByText("已删除连接器实例")).toBeVisible({ timeout: 10000 });
  ok("UI 删除连接器实例");

  await page.locator(".ant-tabs-tab").filter({ hasText: /^系统连接$/ }).click();
  await page.getByRole("row").filter({ hasText: sysKey }).getByRole("button", { name: "删除" }).click();
  await page.locator(".ant-modal-confirm").getByRole("button", { name: /确\s*定/ }).click();
  await expect(page.getByText("已删除连接")).toBeVisible({ timeout: 10000 });
  await shot(page, "05-connection-deleted.png");
  ok("UI 删除系统连接");

  // ---------- 弃用 / 退役 ----------
  const headers = {
    Authorization: `Bearer ${token}`,
    "Content-Type": "application/json",
    Accept: "application/json",
  };
  const orgs = await (await page.request.get(`${base}/api/v1/orgs`, { headers })).json();
  const items = orgs.items || orgs;
  const hq = items.find((o) => o.code === "HQ") || items.find((o) => o.type === "company") || items[0];
  const an = `browser.retire.${run}`;
  const assetRes = await page.request.post(`${base}/api/v1/assets`, {
    headers,
    data: { type: "tool", name: an, owner_org_id: hq.id, classification: 2 },
  });
  expect(assetRes.ok(), await assetRes.text()).toBeTruthy();
  const asset = await assetRes.json();
  const spec = {
    capability_id: "browser.ops.query",
    domain: "browser",
    name: "ops_query",
    description: "浏览器验收用只读工具，查询占位数据并返回状态。",
    input_schema: { type: "object", properties: { id: { type: "string" } } },
    output_schema: { type: "object" },
    connector_ref: { connector: "mock", operation: "ops_query" },
    risk_level: "read",
    timeout: { connect_ms: 2000, read_ms: 5000, deadline_ms: 8000 },
    data_classification: 2,
    identity_mode: "service_account",
    confirm_policy: "auto",
    scope_binding: { id: { dimension: "company", map: "by_code", system_key: "browser" } },
  };
  const verRes = await page.request.post(`${base}/api/v1/assets/${asset.id}/versions`, {
    headers,
    data: { semver: "1.0.0", spec },
  });
  expect(verRes.ok(), await verRes.text()).toBeTruthy();
  const ver = await verRes.json();

  const py = resolve(root, ".venv/Scripts/python.exe");
  const promote = `
import asyncio, uuid, sys
sys.path.insert(0, r${JSON.stringify(resolve(root, "src"))})
from aip.control_plane.db import Database, DbScope
from aip.control_plane.iam.authz import Actor
from aip.control_plane.registry import service as registry
from aip.libs.common.config import get_settings
async def main():
    db = Database(get_settings().app_database_url)
    vid = uuid.UUID(${JSON.stringify(ver.id)})
    async with db.tx(DbScope.system()) as conn:
        for st in ("testing", "in_review", "approved", "canary", "active"):
            await registry.transition(conn, Actor.system_actor(), vid, st, "browser-ops")
        await registry.create_release(
            conn, Actor.system_actor(), vid, env="test", channel="stable",
            scope={"group": True}, exposure=["rest"],
        )
asyncio.run(main())
print("promoted")
`;
  const promo = spawnSync(py, ["-c", promote], {
    cwd: root,
    encoding: "utf-8",
    env: { ...process.env, PYTHONIOENCODING: "utf-8", PYTHONPATH: resolve(root, "src") },
  });
  if (promo.status !== 0) throw new Error(`promote failed: ${promo.stderr || promo.stdout}`);
  ok("脚本将版本推进到 active", (promo.stdout || "").trim());

  await go("/assets");
  await page.getByPlaceholder(/资产名称/).fill(an);
  await page.keyboard.press("Enter");
  await page.waitForTimeout(800);
  await page.getByText(an).first().click();
  await page.locator(".ant-drawer .ant-table-row").first().click();
  await expect(page.getByRole("button", { name: "弃用" })).toBeVisible({ timeout: 15000 });
  await shot(page, "07-active-deprecate-btn.png");
  await page.getByRole("button", { name: "弃用" }).click();
  await page.locator(".ant-modal-confirm").getByRole("button", { name: /确\s*定/ }).click();
  await expect(page.getByText("已弃用")).toBeVisible({ timeout: 10000 });
  ok("UI 弃用 active 版本");

  await expect(page.getByRole("button", { name: "退役" })).toBeVisible({ timeout: 10000 });
  await shot(page, "08-deprecated-retire-btn.png");
  await page.getByRole("button", { name: "退役" }).click();
  await page.locator(".ant-modal-confirm").getByRole("button", { name: /确\s*定/ }).click();
  await expect(page.getByText("已退役")).toBeVisible({ timeout: 10000 });
  await shot(page, "09-retired.png");
  ok("UI 退役 deprecated 版本");
} catch (e) {
  fail("浏览器流程异常", String(e?.stack || e));
  await shot(page, "99-error.png").catch(() => {});
  const body = await page.evaluate(() => document.body.innerText.slice(0, 2000)).catch(() => "");
  writeFileSync(resolve(out, "error-body.txt"), body);
} finally {
  writeFileSync(resolve(out, "report.json"), JSON.stringify({ report }, null, 2));
  console.log(JSON.stringify({ report, out }, null, 2));
  await browser.close();
}
