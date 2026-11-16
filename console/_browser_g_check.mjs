// Wave G 浏览器验收：会话、连接/上游 MCP、组织安全密钥、待办不踢会话
import { chromium, expect } from "@playwright/test";
import { readFileSync, mkdirSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { TOTP } from "otpauth";

const root = resolve(import.meta.dirname, "..");
const out = resolve(root, ".tmp/browser-g");
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

const browser = await chromium.launch({ channel: "chrome", headless: false, slowMo: 50 });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, locale: "zh-CN" });
const base = "http://127.0.0.1:18081";
const dp = "http://127.0.0.1:18082";

try {
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
    if (i === 2) throw new Error(`API 登录失败: ${res.status()} ${await res.text()}`);
    await new Promise((r) => setTimeout(r, 2000));
  }
  await page.goto(`${base}/console/`);
  await page.evaluate((t) => sessionStorage.setItem("aip.session", t), token);
  await page.reload();
  await expect(page.getByRole("button", { name: "账号菜单" })).toBeVisible({ timeout: 20000 });
  await shot(page, "01-logged-in.png");
  ok("会话注入并进入控制台");

  // 待办角标：不应因服务凭据把会话清掉
  await page.waitForTimeout(1500);
  const kept = await page.evaluate(() => sessionStorage.getItem("aip.session"));
  if (!kept) throw new Error("进入控制台后 session 被清空（疑似 /todo 401）");
  const todo = await page.request.get(`${base}/api/v1/todo`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (todo.status() === 401) throw new Error(`/todo 返回 401: ${await todo.text()}`);
  ok("GET /todo 非 401", `status=${todo.status()}`);

  // 连接 → 上游 MCP 源
  await page.goto(`${base}/console/#/connections`);
  await expect(page.getByRole("button", { name: "登记连接" })).toBeVisible({ timeout: 15000 });
  await page.getByRole("tab", { name: "上游 MCP 源" }).click();
  await expect(page.getByRole("button", { name: "登记上游 MCP 源" })).toBeVisible({ timeout: 10000 });
  await shot(page, "02-upstream-tab.png");
  await page.getByRole("button", { name: "登记上游 MCP 源" }).click();
  const dlg = page.getByRole("dialog");
  await expect(dlg.getByText("从连接拉取哈希")).toBeVisible({ timeout: 8000 });
  await shot(page, "03-upstream-form-pull-hash.png");
  await dlg.getByRole("button", { name: /取\s*消|Cancel/i }).click().catch(async () => {
    await page.keyboard.press("Escape");
  });
  ok("上游 MCP 登记表含「从连接拉取哈希」");

  // 组织 → 用户 → 安全密钥
  await page.goto(`${base}/console/#/orgs`);
  await expect(page.getByText("组织", { exact: false }).first()).toBeVisible({ timeout: 15000 });
  // 切到用户表：常见为「用户」页签或列表行
  const userTab = page.getByRole("tab", { name: "用户" });
  if (await userTab.count()) await userTab.click();
  await page.waitForTimeout(800);
  // 点开 admin 或首行用户
  const adminRow = page.getByRole("row").filter({ hasText: /admin|平台/ }).first();
  if (await adminRow.count()) {
    await adminRow.click();
  } else {
    await page.locator(".ant-table-row").first().click();
  }
  await expect(page.getByRole("tab", { name: "安全密钥" })).toBeVisible({ timeout: 15000 });
  await page.getByRole("tab", { name: "安全密钥" }).click();
  await expect(page.getByRole("button", { name: "登记安全密钥（stub）" })).toBeVisible({ timeout: 10000 });
  await shot(page, "04-webauthn-tab.png");
  ok("组织用户抽屉含「安全密钥」页签");

  // 数据面 PKCE 同意页 HTML（需 AIP_OAUTH_PKCE_ENABLED=true）
  const authUrl = `${dp}/oauth/authorize?response_type=code&client_id=browser-g&redirect_uri=${encodeURIComponent("http://127.0.0.1:9999/cb")}&code_challenge=${"a".repeat(43)}&code_challenge_method=S256&subject=vn.emp.001`;
  await page.goto(authUrl, { waitUntil: "domcontentloaded" });
  const authBody = await page.locator("body").innerText();
  if (!authBody.includes("授权第三方访问") && !authBody.includes("同意")) {
    throw new Error(`PKCE 同意页未出现: status body=${authBody.slice(0, 400)}`);
  }
  await shot(page, "05-pkce-consent.png");
  ok("GET /oauth/authorize 同意页可见");

  await page.goto(`${base}/console/#/assets`);
  await expect(page.getByRole("button", { name: "新建资产" })).toBeVisible({ timeout: 15000 });
  await shot(page, "06-assets.png");
  ok("资产页可打开");
} catch (e) {
  fail("浏览器流程异常", String(e?.stack || e));
  await shot(page, "99-error.png").catch(() => {});
  const body = await page.evaluate(() => document.body.innerText.slice(0, 2500)).catch(() => "");
  writeFileSync(resolve(out, "error-body.txt"), body);
} finally {
  writeFileSync(resolve(out, "report.json"), JSON.stringify({ report }, null, 2));
  console.log(JSON.stringify({ report, out }, null, 2));
  await browser.close();
}
const bad = report.filter((r) => !r.ok);
process.exit(bad.length ? 1 : 0);
