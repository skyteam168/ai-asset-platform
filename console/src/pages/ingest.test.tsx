import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { session } from "../api";
import { OrgProvider } from "../components";
import { Markdown, parseBlocks } from "../markdown";
import { mockApi, page, wrap } from "../test/util";
import type { Connection, Me } from "../types";
import { guessFormat } from "./ApiImport";
import { SPEC_TEMPLATES, specTemplate } from "./Assets";
import { ConnectionsPage } from "./Connections";
import { checkFileName, SkillPackagePanel } from "./SkillEditor";

const dev: Me = {
  id: "u1", login_name: "d", display_name: "D", home_org_id: "o1", effective_clearance: 3,
  roles: [{ role: "asset_developer", scope_org_id: "o1" }],
};

const conn = {
  id: "c1", company_id: "co1", system_key: "mes", env: "test", base_url: "https://10.0.0.1", scheme: "https",
  tls_mode: "system_ca", pins: [], transport_exception_reason: null, transport_exception_expires_at: null,
  transport_approved_by: [], tls_last_seen_fingerprint: null, status: "active",
} as unknown as Connection;

describe("Markdown 预览", () => {
  it("解析标题、列表、代码块与表格", () => {
    const blocks = parseBlocks("# 标题\n\n- a\n- b\n\n```json\n{}\n```\n\n| x | y |\n| --- | --- |\n| 1 | 2 |\n\n正文");
    expect(blocks.map((b) => b.kind)).toEqual(["heading", "list", "code", "table", "para"]);
  });

  it("只渲染 http(s) 外链，脚本链接降级为文本", () => {
    render(<Markdown source={"[ok](https://a.example) [bad](javascript:alert(1)) **粗** `code`"} />);
    expect(screen.getByRole("link", { name: "ok" })).toHaveAttribute("href", "https://a.example");
    expect(screen.queryByRole("link", { name: "bad" })).not.toBeInTheDocument();
    expect(screen.getByText("粗").tagName).toBe("STRONG");
  });
});

describe("新建版本 Spec 模板", () => {
  it("按资产类型切换模板与 Schema", () => {
    expect(SPEC_TEMPLATES.skill.schema).toBe("skill-spec");
    expect(JSON.parse(specTemplate("skill"))).toHaveProperty("name", "query-work-orders");
    expect(JSON.parse(specTemplate("workflow"))).toHaveProperty("budget");
    expect(JSON.parse(specTemplate("tool"))).toHaveProperty("connector_ref");
    expect(specTemplate("prompt")).toBe("{}");
  });
});

describe("连接器实例与 API 导入", () => {
  it("列出实例并登记新实例", async () => {
    session.set("s");
    const calls = mockApi((method, path) => {
      if (path === "/me") return { body: dev };
      if (path === "/connections") return page([conn]);
      if (path === "/connector-instances" && method === "GET") {
        return page([{ id: "i1", name: "mes_vn01", kind: "declarative", company_id: "co1", system_connection_id: "c1",
          config: {}, concurrency: 8, rate_per_s: 20, burst: 40, status: "active" }]);
      }
      if (path === "/connector-instances") return { status: 201, body: { id: "i2" } };
      return method === "GET" ? page([]) : undefined;
    });
    render(wrap(<OrgProvider><ConnectionsPage /></OrgProvider>));
    const user = userEvent.setup();
    await user.click(await screen.findByRole("tab", { name: "连接器实例" }));
    expect(await screen.findByText("mes_vn01")).toBeInTheDocument();
    expect(screen.getByText("mes（test）")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "新建连接器实例" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText("名称"), { target: { value: "mes_mock" } });
    await user.click(within(dialog).getByLabelText("类型"));
    await user.click(await screen.findByTitle("Mock"));
    await waitFor(() => expect((within(dialog).getByLabelText("配置（JSON）") as HTMLTextAreaElement).value).toContain("query_work_orders"));
    await user.click(within(dialog).getByRole("button", { name: /确\s*定|OK/ }));
    await waitFor(() => {
      const body = calls.find((c) => c.method === "POST" && c.path === "/connector-instances")?.body as Record<string, unknown>;
      expect(body).toMatchObject({ name: "mes_mock", kind: "mock", system_connection_id: null, concurrency: 8 });
      expect(body.config).toEqual({ operations: { query_work_orders: { response: { items: [] } } } });
    });
  });

  it("先预览草稿（persist=false），再选连接落库（persist=true）", async () => {
    session.set("s");
    const preview = {
      connector_config: { operations: [] }, warnings: ["跳过无 operationId 的操作"],
      tools: [{ capability_id: "production.mes.list_orders", name: "list_orders", description: "列出工单", risk_level: "read",
        connector_ref: { connector: "mes_api", operation: "list_orders" } }],
    };
    const calls = mockApi((method, path, body) => {
      if (path === "/me") return { body: dev };
      if (path === "/connections") return page([conn]);
      if (path === "/importer/openapi") {
        const persist = (body as { persist: boolean }).persist;
        return { body: persist ? { ...preview, connector_instance_id: "i9",
          drafts: [{ asset_id: "a1", version_id: "v1", capability_id: "production.mes.list_orders", state: "draft" }] } : preview };
      }
      return method === "GET" ? page([]) : undefined;
    });
    render(wrap(<OrgProvider><ConnectionsPage /></OrgProvider>));
    const user = userEvent.setup();
    await user.click(await screen.findByRole("tab", { name: "连接器实例" }));
    await user.click(await screen.findByRole("button", { name: "API 导入" }));
    fireEvent.change(await screen.findByLabelText("Connector 名称"), { target: { value: "mes_api" } });
    fireEvent.change(screen.getByLabelText("业务域"), { target: { value: "production" } });
    fireEvent.change(screen.getByLabelText("系统"), { target: { value: "mes" } });
    fireEvent.change(screen.getByLabelText("文档（JSON 或 YAML）"), { target: { value: '{"openapi":"3.0.0"}' } });
    fireEvent.click(screen.getByRole("button", { name: "预览草稿" }));
    expect(await screen.findByText("production.mes.list_orders")).toBeInTheDocument();
    expect(screen.getByText(/跳过无 operationId/)).toBeInTheDocument();
    expect(calls.find((c) => c.path === "/importer/openapi")?.body).toMatchObject({ persist: false, connector_name: "mes_api", domain: "production" });

    fireEvent.click(screen.getByRole("button", { name: "落库为草稿" }));
    expect(await screen.findByText("落库前须选择系统连接")).toBeInTheDocument();
    await user.click(screen.getAllByLabelText("系统连接").at(-1)!);
    await user.click(await screen.findByTitle("mes（test） · https://10.0.0.1"));
    fireEvent.click(screen.getByRole("button", { name: "落库为草稿" }));
    await waitFor(() => {
      const persisted = calls.filter((c) => c.path === "/importer/openapi").at(-1)?.body;
      expect(persisted).toMatchObject({ persist: true, connection_id: "c1", owner_org_id: null });
    });
    expect(await screen.findByText(/已创建 Connector 实例与 1 个 Tool 草稿/)).toBeInTheDocument();
  }, 60_000);

  it("按内容识别 Postman Collection", () => {
    expect(guessFormat('{"info":{"_postman_id":"x"}}')).toBe("postman");
    expect(guessFormat("openapi: 3.0.0")).toBe("openapi");
  });
});

describe("在线 Skill 编辑器", () => {
  it("文件名校验：拒绝脚本与非文本 references", () => {
    expect(checkFileName("guide.md", "references")).toBeNull();
    expect(checkFileName("run.py", "assets")).toMatch(/脚本/);
    expect(checkFileName("logo.png", "references")).toMatch(/文本文件/);
    expect(checkFileName("../x.md", "references")).not.toBeNull();
    expect(checkFileName("logo.png", "assets")).toBeNull();
  });

  it("无包时从模板起步，保存时提交 frontmatter、正文、Tool 引用与 references", async () => {
    session.set("s");
    const calls = mockApi((method, path) => {
      if (path === "/me") return { body: dev };
      if (path === "/versions/v1/skill-source" && method === "GET") return { status: 404, body: { code: "NOT_FOUND", detail: "Skill 包不存在" } };
      if (path === "/versions/v1/skill-source") {
        return { body: { version_id: "v1", name: "query-work-orders", files: ["SKILL.md"], content_sha256: "ab", warnings: ["描述中不允许出现外部链接"] } };
      }
      if (path === "/console/catalog") return { body: { items: [{ capability_id: "production.mes.query_work_orders", version: "1.0.0" }], bundles: [] } };
      return method === "GET" ? page([]) : undefined;
    });
    render(wrap(
      <SkillPackagePanel versionId="v1" assetId="a1" editable onSaved={() => undefined}
        spec={{ name: "query-work-orders", description: "指导模型查询本厂在制工单并汇总状态。" }} />,
    ));
    const user = userEvent.setup();
    const editor = await screen.findByLabelText("SKILL.md 正文（Markdown）");
    expect((editor as HTMLTextAreaElement).value).toContain("# query-work-orders");
    expect(await screen.findByRole("heading", { name: "query-work-orders" })).toBeInTheDocument();
    fireEvent.change(editor, { target: { value: "# 查询工单\n\n1. 先确认工厂。" } });
    expect(await screen.findByRole("heading", { name: "查询工单" })).toBeInTheDocument();

    fireEvent.change(screen.getByPlaceholderText("guide.md"), { target: { value: "status.md" } });
    await user.click(screen.getByRole("button", { name: "新增" }));
    fireEvent.change(await screen.findByLabelText("文件内容：references/status.md"), { target: { value: "状态表" } });

    await user.click(screen.getByRole("button", { name: "保存并签名" }));
    await waitFor(() => {
      const put = calls.find((c) => c.method === "PUT");
      expect(put?.path).toBe("/versions/v1/skill-source");
      expect(put?.body).toMatchObject({
        name: "query-work-orders", description: "指导模型查询本厂在制工单并汇总状态。", license: null,
        body: "# 查询工单\n\n1. 先确认工厂。", tools: [], references: [{ path: "references/status.md", content: "状态表" }], assets: [],
      });
    });
    expect(await screen.findByText(/描述中不允许出现外部链接/)).toBeInTheDocument();
  });

  it("非 draft 版本只读展示已入库的包", async () => {
    session.set("s");
    mockApi((method, path) => {
      if (path === "/me") return { body: dev };
      if (path === "/versions/v2/skill-source") {
        return { body: { name: "query-work-orders", description: "d".repeat(12), license: "MIT", compatibility: null, allowed_tools: ["Read"],
          body: "# 已发布", tools: ["production.mes.query_work_orders@1.0.0"], references: [{ path: "references/a.md", content: "" }],
          assets: [], warnings: [], content_sha256: "ff" } };
      }
      return method === "GET" ? page([]) : undefined;
    });
    render(wrap(<SkillPackagePanel versionId="v2" assetId="a1" editable={false} onSaved={() => undefined} spec={{}} />));
    expect(await screen.findByRole("heading", { name: "已发布" })).toBeInTheDocument();
    expect(screen.getByText("production.mes.query_work_orders@1.0.0")).toBeInTheDocument();
    expect(screen.getByText("references/a.md")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "保存并签名" })).not.toBeInTheDocument();
  });
});
