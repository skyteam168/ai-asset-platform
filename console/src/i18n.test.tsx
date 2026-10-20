import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { setLang, t } from "./i18n";
import { collectKeys } from "./i18nKeys";
import { enUS } from "./locales/en-US";
import { viVN } from "./locales/vi-VN";
import { LoginPage } from "./pages/Login";
import { wrap } from "./test/util";

// 构建期把源码作为文本读入，检查每个 t("…") 的键都有译文
const sources = import.meta.glob(["./**/*.tsx", "./**/*.ts", "!./**/*.test.*", "!./locales/**", "!./i18nKeys.ts"], {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

const placeholders = (s: string) => [...s.matchAll(/\{\d+\}/g)].map((m) => m[0]).sort().join();

afterEach(() => setLang("zh-CN"));

describe("多语言", () => {
  const keys = collectKeys(Object.values(sources));

  it("源码中的每个键都有英文与越南文译文", () => {
    expect(keys.length).toBeGreaterThan(250);
    expect(keys.filter((k) => !(k in enUS))).toEqual([]);
    expect(keys.filter((k) => !(k in viVN))).toEqual([]);
  });

  it("译文保留占位符", () => {
    for (const dict of [enUS, viVN]) {
      expect(keys.filter((k) => placeholders(k) !== placeholders(dict[k] ?? k))).toEqual([]);
    }
  });

  it("按位置替换参数，缺译回落中文", () => {
    setLang("en-US");
    expect(t("第 {0} 行：{1}", [3, "x"])).toBe("Line 3: x");
    expect(t("没有收录的句子")).toBe("没有收录的句子");
    setLang("vi-VN");
    expect(t("退出")).toBe("Đăng xuất");
  });

  it("英文界面：登录页按英文渲染", async () => {
    setLang("en-US");
    render(wrap(<LoginPage />));
    expect(await screen.findByRole("button", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.getByLabelText("Username")).toBeInTheDocument();
    expect(screen.getByText("AI Digital Asset Management Platform")).toBeInTheDocument();
  });
});

describe("主题", () => {
  it("切换深色并记住选择", async () => {
    render(wrap(<LoginPage />));
    fireEvent.click(screen.getByRole("button", { name: "主题" }));
    fireEvent.click(await screen.findByText("深色"));
    await waitFor(() => expect(document.documentElement.dataset.theme).toBe("dark"));
    expect(JSON.parse(localStorage.getItem("aip.theme") ?? "{}").mode).toBe("dark");
    fireEvent.click(screen.getByRole("button", { name: "紫" }));
    await waitFor(() => expect(JSON.parse(localStorage.getItem("aip.theme") ?? "{}").primary).toBe("#5b3fbf"));
  });
});
