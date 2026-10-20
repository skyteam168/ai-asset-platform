// 端到端：真实控制面（tools/dev.py serve）托管 console/dist，平台管理员走一遍主要流程。
// 凭据取自环境变量，缺省读 bootstrap 写出的 .aip-data/dev-admin.txt（仅 dev）。
import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { TOTP } from "otpauth";

function credentials() {
  if (process.env.AIP_E2E_PASSWORD && process.env.AIP_E2E_TOTP_SECRET) {
    return { login: process.env.AIP_E2E_LOGIN ?? "admin", password: process.env.AIP_E2E_PASSWORD, secret: process.env.AIP_E2E_TOTP_SECRET };
  }
  const text = readFileSync(resolve(process.cwd(), "../.aip-data/dev-admin.txt"), "utf-8");
  return {
    login: /^login_name=(.*)$/m.exec(text)![1].trim(),
    password: /^password=(.*)$/m.exec(text)![1].trim(),
    secret: /secret=([A-Z2-7]+)/.exec(text)![1],
  };
}

async function freshCode(secret: string): Promise<string> {
  // 同一时间窗的动态码只能用一次；离窗口结束太近时等下一个窗口
  const left = 30 - ((Date.now() / 1000) % 30);
  if (left < 5) await new Promise((r) => setTimeout(r, (left + 1) * 1000));
  return new TOTP({ secret }).generate();
}

async function pick(page: Page, label: string, option: string) {
  await page.getByLabel(label, { exact: true }).click({ force: true }); // 已有值时选中项覆盖在输入框上
  await page.locator(".ant-select-dropdown:visible .ant-select-item-option").filter({ hasText: new RegExp(`^${option}( ·|$)`) }).first().click();
}

const RUN = Date.now().toString(36);

test.describe.configure({ mode: "serial" });

let page: Page;

test.beforeAll(async ({ browser }) => {
  page = await browser.newPage();
});

test("口令 + TOTP 登录，菜单完整", async () => {
  const c = credentials();
  await page.goto("/console/");
  await page.getByLabel("用户名").fill(c.login);
  await page.getByLabel("口令").fill(c.password);
  await page.getByLabel("动态码").fill("000000");
  await page.getByRole("button", { name: /登\s*录/ }).click();
  await expect(page.getByText(/用户名、口令或动态码错误/)).toBeVisible();

  await page.getByLabel("动态码").fill(await freshCode(c.secret));
  await page.getByRole("button", { name: /登\s*录/ }).click();
  await expect(page.getByRole("menuitem")).toHaveCount(8);
  await expect(page.getByText("平台管理员").first()).toBeVisible();
});

test("组织树展开并按子树过滤用户", async () => {
  await page.getByRole("menuitem", { name: "组织与用户" }).click();
  await expect(page.locator(".ant-tree-treenode")).not.toHaveCount(1);
  await page.locator(".ant-tree-node-content-wrapper").filter({ hasText: "HQ" }).click();
  await expect(page.getByText("用户（HQ 子树）")).toBeVisible();
});

test("登记明文 HTTP 连接（传输例外）并执行连接测试", async () => {
  await page.getByRole("menuitem", { name: "系统连接" }).click();
  await page.getByRole("button", { name: "登记连接" }).click();
  const dialog = page.getByRole("dialog");
  await pick(page, "公司", "HQ");
  await dialog.getByLabel("系统").fill(`dp${RUN}`);
  await dialog.getByLabel("地址").fill("http://127.0.0.1:18082");
  await pick(page, "协议", "http");
  await expect(dialog.getByText(/传输例外/)).toBeVisible();
  await dialog.getByLabel("例外理由").fill("e2e 数据面健康探测");
  await dialog.getByLabel("到期日").click();
  await page.locator(".ant-picker-dropdown:visible td.ant-picker-cell:not(.ant-picker-cell-disabled)").nth(5).click();
  await dialog.getByRole("button", { name: /确\s*定/ }).click();
  await expect(page.getByText("已登记，待数据 Owner 审批")).toBeVisible();

  const row = page.getByRole("row").filter({ hasText: `dp${RUN}` });
  await expect(row.getByText("明文 HTTP")).toBeVisible();
  await expect(row.getByText("pending_approval")).toBeVisible();
  await row.getByRole("button", { name: "连接测试" }).click();
  await page.getByRole("button", { name: /执行测试/ }).click();
  await expect(page.locator(".ant-drawer .ant-alert")).toContainText(/通过|失败/);
  await expect(page.locator(".ant-drawer").getByText("解析地址")).toBeVisible();
  await page.locator(".ant-drawer-close").click();
});

test("新建资产与版本，G0 静态检查给出缺失项", async () => {
  await page.getByRole("menuitem", { name: "资产与门禁" }).click();
  await page.getByRole("button", { name: "新建资产" }).click();
  const name = `e2e.asset_${RUN}`;
  await page.getByRole("dialog").getByLabel("名称").fill(name);
  await pick(page, "归属组织", "HQ");
  await page.getByRole("dialog").getByRole("button", { name: /确\s*定/ }).click();
  await expect(page.getByText("已创建")).toBeVisible();

  await page.getByRole("cell", { name }).click();
  await page.getByRole("button", { name: "新建版本" }).click();
  await page.getByRole("dialog", { name: "新建版本" }).getByRole("button", { name: /确\s*定/ }).click();
  const version = page.locator(".ant-drawer").getByRole("cell", { name: "1.0.0" });
  await expect(version).toBeVisible();
  await version.click();
  await expect(page.locator(".ant-drawer").getByText("版本 1.0.0")).toBeVisible();

  await page.getByRole("button", { name: "提交（G0 静态检查）" }).click();
  await expect(page.getByText("G0 未通过")).toBeVisible();
  await expect(page.getByText(/至少需要一个功能用例和一个权限用例/)).toBeVisible();
  await page.getByRole("button", { name: /知道了|OK|确\s*定/ }).last().click();

  await page.getByRole("tab", { name: "门禁记录" }).click();
  await expect(page.locator(".ant-drawer").getByRole("cell", { name: "G0" })).toBeVisible();
  await page.locator(".ant-drawer-close").click();
});

test("下发授权上限，审计页可校验哈希链", async () => {
  await page.getByRole("menuitem", { name: "授权" }).click();
  await page.locator(".ant-select").filter({ hasText: "分公司" }).first().click();
  await page.locator(".ant-select-dropdown:visible .ant-select-item-option").filter({ hasText: "HQ" }).click();
  await page.locator(".ant-select").filter({ hasText: "资产集合" }).click();
  await page.locator(".ant-select-dropdown:visible .ant-select-item-option").filter({ hasText: `e2e.asset_${RUN}` }).click();
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "下发上限" }).click();
  await expect(page.getByText("已下发")).toBeVisible();
  await expect(page.getByRole("cell", { name: `e2e.asset_${RUN}` })).toBeVisible();

  await page.getByRole("menuitem", { name: "审计" }).click();
  await page.getByRole("button", { name: "校验哈希链与锚点" }).click();
  await expect(page.getByText(/哈希链与签名锚点全部校验通过/)).toBeVisible();
});

test("退出后回到登录页", async () => {
  await page.getByRole("button", { name: /退\s*出/ }).click();
  await expect(page.getByRole("button", { name: /登\s*录/ })).toBeVisible();
});
