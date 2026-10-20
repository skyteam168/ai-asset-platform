import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { session } from "../api";
import { OrgProvider } from "../components";
import { mockApi, page, wrap } from "../test/util";
import type { Approval, Connection, Me, Role } from "../types";
import { ApprovalsPage } from "./Approvals";
import { AuditPage } from "./Audit";
import { TransportTag } from "./Connections";

const me = (role: Role): Me => ({
  id: "u1", login_name: "a", display_name: "A", home_org_id: "o1", effective_clearance: 3, roles: [{ role, scope_org_id: "o1" }],
});

type Transport = Pick<Connection, "scheme" | "tls_mode">;

describe("TransportTag", () => {
  const cases: [Transport, string][] = [
    [{ scheme: "http", tls_mode: null }, "明文 HTTP"],
    [{ scheme: "https", tls_mode: "insecure" }, "不校验证书"],
    [{ scheme: "https", tls_mode: "pinned" }, "HTTPS · 固定指纹"],
    [{ scheme: "https", tls_mode: "custom_ca" }, "HTTPS · 内部 CA"],
    [{ scheme: "https", tls_mode: "system_ca" }, "HTTPS · 系统 CA"],
  ];
  it.each(cases)("%o → %s", (c, label) => {
    render(<TransportTag c={c} />);
    expect(screen.getByText(label)).toBeInTheDocument();
  });
});

describe("审计页", () => {
  it("把筛选条件拼进查询，审计员可校验哈希链", async () => {
    session.set("s");
    const calls = mockApi((method, path) => {
      if (path === "/me") return { body: me("auditor") };
      if (path === "/audit-logs/verify") return { body: { ok: true, problems: [], shards: 2, records: 42 } };
      if (path.startsWith("/audit-logs")) {
        return page([{
          shard_id: "cp-0", seq: 7, occurred_at: "2026-10-20T08:00:00Z", company_id: null, kind: "tool.invoke",
          record: { trace_id: "t-abc" }, hash: "abcdef0123456789",
        }]);
      }
      return method === "GET" ? page([]) : undefined;
    });
    render(wrap(<OrgProvider><AuditPage /></OrgProvider>));
    expect(await screen.findByText("t-abc")).toBeInTheDocument();
    fireEvent.change(screen.getByPlaceholderText(/事件类型/), { target: { value: "grant.create" } });
    fireEvent.click(screen.getByRole("button", { name: /查\s*询/ }));
    await waitFor(() => expect(calls.some((c) => c.path === "/audit-logs?kind=grant.create&limit=100")).toBe(true));
    fireEvent.click(await screen.findByRole("button", { name: "校验哈希链与锚点" }));
    expect(await screen.findByText(/2 个分片，42 条记录/)).toBeInTheDocument();
  });

  it("分公司管理员看不到校验按钮", async () => {
    session.set("s");
    mockApi((method, path) => (path === "/me" ? { body: me("company_admin") } : method === "GET" ? page([]) : undefined));
    render(wrap(<OrgProvider><AuditPage /></OrgProvider>));
    expect(await screen.findByRole("button", { name: /查\s*询/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "校验哈希链与锚点" })).not.toBeInTheDocument();
  });
});

describe("审批页", () => {
  const item: Approval = {
    id: "ap-1", kind: "G6", subject_id: "v1", subject: "mes.work_orders@1.0.0", approver_role: "platform_admin",
    required_approvals: 1, due_at: "2026-10-23T00:00:00Z", payload: {},
  };

  it("驳回必须写理由，写明后提交决定", async () => {
    session.set("s");
    const calls = mockApi((method, path) => {
      if (path === "/me") return { body: me("platform_admin") };
      if (path === "/approvals") return page([item]);
      if (path === "/approvals/ap-1/decide") return { body: { id: "ap-1", status: "rejected" } };
      return method === "GET" ? page([]) : undefined;
    });
    render(wrap(<ApprovalsPage />));
    const user = userEvent.setup();
    expect(await screen.findByText("mes.work_orders@1.0.0")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /处\s*理/ }));
    await user.click(await screen.findByLabelText("驳回"));
    const ok = () => screen.getByRole("button", { name: /确\s*定|OK/ });
    await user.click(ok());
    expect(await screen.findByText("驳回需写明理由")).toBeInTheDocument();
    expect(calls.some((c) => c.method === "POST")).toBe(false);
    await user.type(screen.getByLabelText("意见"), "缺少权限用例");
    await user.click(ok());
    await waitFor(() => expect(calls.find((c) => c.method === "POST")?.body).toEqual({ decision: "reject", comment: "缺少权限用例" }));
  });
});
