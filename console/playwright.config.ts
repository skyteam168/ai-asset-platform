import { defineConfig } from "@playwright/test";

// 需要先运行 python tools/dev.py serve（控制面在 18081 托管 console/dist）
export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: [["list"]],
  use: {
    baseURL: process.env.AIP_E2E_BASE_URL ?? "http://127.0.0.1:18081",
    viewport: { width: 1440, height: 900 },
    locale: "zh-CN",
    screenshot: "only-on-failure",
  },
  outputDir: "../.tmp/e2e-results",
});
