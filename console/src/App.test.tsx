import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { AppShell } from "./App";
import { session } from "./api";
import { mockApi, page, wrap } from "./test/util";
import type { Me, Role } from "./types";

const me = (...roles: Role[]): Me => ({
  id: "u1", login_name: "alice", display_name: "Alice", home_org_id: "o1", effective_clearance: 3,
  roles: roles.map((role) => ({ role, scope_org_id: "o1" })),
});

const common = (m: Me) => (method: string, path: string) => {
  if (path === "/me") return { body: m };
  if (method === "GET") return page([]);
  return undefined;
};

describe("控制台外壳", () => {
  it("用户名、口令、TOTP 登录后进入首个可见页面", async () => {
    const calls = mockApi((method, path, body) => {
      if (path === "/auth/login") {
        const b = body as { totp: string };
        return b.totp === "123456"
          ? { body: { session_token: "s-1" } }
          : { status: 401, body: { code: "UNAUTHENTICATED", detail: "动态码错误" } };
      }
      return common(me("platform_admin"))(method, path);
    });
    render(wrap(<AppShell />));
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("用户名"), "alice");
    await user.type(screen.getByLabelText("口令"), "correct-horse-battery");
    await user.type(screen.getByLabelText("动态码"), "000000");
    await user.click(screen.getByRole("button", { name: /登\s*录/ }));
    expect(await screen.findByText(/动态码错误/)).toBeInTheDocument();

    await user.clear(screen.getByLabelText("动态码"));
    await user.type(screen.getByLabelText("动态码"), "123456");
    await user.click(screen.getByRole("button", { name: /登\s*录/ }));
    expect(await screen.findByRole("button", { name: "账号菜单" })).toHaveTextContent("Alice");
    expect(session.get()).toBe("s-1");
    expect(calls.find((c) => c.path === "/auth/login" && (c.body as { totp: string }).totp === "123456")?.body)
      .toEqual({ login_name: "alice", password: "correct-horse-battery", totp: "123456" });
    // 5 个顶层菜单（含分发与 Skill）+ 3 个分组标题 + 6 个分组内菜单（含 LLM 配置）
    expect(screen.getAllByRole("menuitem")).toHaveLength(14);
  });

  it("菜单按角色裁剪：审计员只看到风控、审计与组织", async () => {
    session.set("s-2");
    mockApi(common(me("auditor")));
    render(wrap(<AppShell />));
    await waitFor(() => expect(screen.getAllByRole("menuitem")).toHaveLength(5));
    expect(screen.getAllByRole("menuitem").map((x) => x.textContent)).toEqual(["风控与审计", "风控与计量", "审计", "系统管理", "组织与用户"]);
  });

  it("没有任何角色时提示联系管理员", async () => {
    session.set("s-3");
    mockApi(common(me()));
    render(wrap(<AppShell />));
    expect(await screen.findByText(/没有可用的控制台功能/)).toBeInTheDocument();
  });
});
