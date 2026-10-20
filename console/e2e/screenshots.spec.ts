// 设计走查用：按 1920×906 截取浅色 / 深色下的主要页面（SCREENSHOTS=1 时运行，输出到 .tmp/screens）
import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { TOTP } from "otpauth";

test.skip(!process.env.SCREENSHOTS, "仅在 SCREENSHOTS=1 时运行");
test.use({ viewport: { width: 1920, height: 906 } });

const OUT = resolve(process.cwd(), "../.tmp/screens");

function creds() {
  const text = readFileSync(resolve(process.cwd(), "../.aip-data/dev-admin.txt"), "utf-8");
  return {
    login: /^login_name=(.*)$/m.exec(text)![1].trim(),
    password: /^password=(.*)$/m.exec(text)![1].trim(),
    secret: /secret=([A-Z2-7]+)/.exec(text)![1],
  };
}

for (const mode of ["light", "dark"] as const) {
  test(`界面走查（${mode}）`, async ({ page }) => {
    await page.addInitScript((m) => {
      localStorage.setItem("aip.theme", JSON.stringify({ mode: m, primary: "#3d6dfa", compact: false }));
      localStorage.setItem("aip.lang", "zh-CN");
    }, mode);
    await page.goto("/console/");
    await page.screenshot({ path: `${OUT}/login-${mode}.png` });
    const c = creds();
    await page.getByLabel("用户名").fill(c.login);
    await page.getByLabel("口令").fill(c.password);
    const left = 30 - ((Date.now() / 1000) % 30);
    if (left < 4) await page.waitForTimeout((left + 1) * 1000);
    await page.getByLabel("动态码").fill(new TOTP({ secret: c.secret }).generate());
    await page.getByRole("button", { name: /登\s*录/ }).click();
    await expect(page.getByRole("button", { name: "账号菜单" })).toBeVisible();
    for (const path of ["/connections", "/assets", "/risk", "/approvals"]) {
      await page.goto(`/console/#${path}`);
      await page.waitForTimeout(800);
      await page.screenshot({ path: `${OUT}/${path.slice(1)}-${mode}.png` });
    }
  });
}
