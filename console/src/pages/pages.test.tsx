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
import { LlmConfigPage } from "./LlmConfig";

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

describe("LLM 配置页", () => {
  it("平台管理员可保存白名单与预算", async () => {
    session.set("s");
    const company = { id: "c1", code: "VN01", name: "越南", type: "company", parent_id: null, status: "active" };
    const calls = mockApi((method, path) => {
      if (path === "/me") return { body: me("platform_admin") };
      if (path === "/orgs") return page([company]);
      if (path === "/companies/c1/llm-allowlist") {
        return method === "PUT" ? { body: { company_id: "c1", names: ["gpt-4o-mini"] } } : { body: { company_id: "c1", names: [] } };
      }
      if (path === "/companies/c1/llm-budget") {
        return method === "PUT" ? { body: { company_id: "c1", daily_tokens: 1000 } } : { body: { company_id: "c1", daily_tokens: 0 } };
      }
      if (path === "/companies/c1/llm-usage") {
        return { body: { company_id: "c1", usage_date: "2026-10-21", tokens_used: 10, daily_tokens: 0 } };
      }
      return method === "GET" ? page([]) : undefined;
    });
    render(wrap(<OrgProvider><LlmConfigPage /></OrgProvider>));
    expect(await screen.findByText(/当日用量/)).toBeInTheDocument();
    const user = userEvent.setup();
    const box = await screen.findByLabelText(/模型白名单/);
    fireEvent.change(box, { target: { value: "gpt-4o-mini" } });
    fireEvent.change(screen.getByLabelText(/日 token 预算/), { target: { value: "1000" } });
    await user.click(screen.getByRole("button", { name: /^保\s*存$/ }));
    await waitFor(() => {
      expect(calls.some((c) => c.method === "PUT" && c.path === "/companies/c1/llm-allowlist")).toBe(true);
      expect(calls.some((c) => c.method === "PUT" && c.path === "/companies/c1/llm-budget")).toBe(true);
    });
  });
});

describe("平台待办：调用确认与工作流任务", () => {
  const todo = {
    confirmations: [{
      id: "c1", kind: "approval", company_id: "o1", capability_id: "finance.erp.wire", version: "1.0.0",
      summary: "向 ACME 付款 800 元", approver_role: "data_owner", end_user_id: "u9", args_sha256: "ab".repeat(32),
      execution_id: "e1", run_id: null, expires_at: "2026-10-21T00:00:00Z",
    }],
    tasks: [{
      id: "t1", run_id: "r-123456789", node_id: "fill", kind: "input", summary: "补录到货数量", assignee: "initiator",
      deadline: "2026-10-21T00:00:00Z", data: { input_schema: { type: "object", required: ["qty"] } },
    }],
  };

  it("审批调用确认走 /todo/confirmations，补录任务提交 JSON 数据", async () => {
    session.set("s");
    const calls = mockApi((method, path) => {
      if (path === "/me") return { body: me("data_owner") };
      if (path === "/todo") return { body: todo };
      if (method === "POST") return { body: { status: "succeeded" } };
      return method === "GET" ? page([]) : undefined;
    });
    render(wrap(<ApprovalsPage />));
    const user = userEvent.setup();
    await user.click(await screen.findByRole("tab", { name: /调用确认/ }));
    expect(await screen.findByText("向 ACME 付款 800 元")).toBeInTheDocument();
    await user.click(screen.getAllByRole("button", { name: /处\s*理/ }).at(-1)!);
    await user.click(screen.getByRole("button", { name: /确\s*定|OK/ }));
    await waitFor(() => expect(calls.find((c) => c.path === "/todo/confirmations/c1/decide")?.body).toEqual({ decision: "approve" }));

    await user.click(screen.getByRole("tab", { name: /工作流任务/ }));
    expect(await screen.findByText("补录到货数量")).toBeInTheDocument();
    await user.click(screen.getAllByRole("button", { name: /处\s*理/ }).at(-1)!);
    const box = await screen.findByLabelText("补录内容（JSON）");
    fireEvent.change(box, { target: { value: '{"qty": 12}' } });
    await user.click(screen.getAllByRole("button", { name: /确\s*定|OK/ }).at(-1)!);
    await waitFor(() =>
      expect(calls.find((c) => c.path === "/todo/tasks/t1/decide")?.body).toEqual({ decision: "approve", data: { qty: 12 } }),
    );
  }, 60_000);
});
