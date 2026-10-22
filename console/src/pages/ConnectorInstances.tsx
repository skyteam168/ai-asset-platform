import { Alert, Button, Drawer, Form, Input, InputNumber, Modal, Select, Space, Tag, Typography } from "antd";
import { useState } from "react";
import { get, post, type Page } from "../api";
import { useAuth } from "../auth";
import { JsonInput, jsonRule, JsonView, matches, OrgName, PAGINATION, parseJson, StateTag, Toolbar } from "../components";
import { ListTable } from "../ListTable";
import { useAction, useLoad } from "../hooks";
import type { Connection, ConnectorInstance } from "../types";
import { t } from "../i18n";
import { ApiImportDrawer } from "./ApiImport";

type Kind = ConnectorInstance["kind"];

const KIND_LABEL: Record<Kind, string> = {
  declarative: t("声明式 REST"),
  kb: t("知识库"),
  mock: t("Mock"),
  upstream_mcp: t("上游 MCP"),
  sql_readonly: t("数据库只读"),
};

/** 各类 Connector 的 config 起点（与 connectors/ 下各实现读取的键一致）。 */
export const CONFIG_TEMPLATE: Record<Kind, unknown> = {
  declarative: {
    operations: [{
      connector: "mes_vn01", operation: "query_work_orders", mode: "read",
      request: { method: "GET", path: "/api/work-orders", query: { factory: "{{ args.factory_id }}" } },
      response: { data: "$.items[*]" },
    }],
  },
  kb: { paths: {} },
  mock: { operations: { query_work_orders: { response: { items: [] } } } },
  upstream_mcp: { mcp_path: "/mcp", tools_manifest_hash: "" },
  sql_readonly: {},
};

interface SqlTemplate {
  id: string;
  template_key: string;
  engine: string;
  statement: string;
  status: string;
  max_rows: number;
}

export function ConnectorInstancesTab({ connections }: { connections: Connection[] }) {
  const { has } = useAuth();
  const list = useLoad(() => get<Page<ConnectorInstance>>("/connector-instances"), []);
  const [open, setOpen] = useState(false);
  const [importing, setImporting] = useState(false);
  const [sqlInst, setSqlInst] = useState<ConnectorInstance | null>(null);
  const [kw, setKw] = useState("");
  const byId = new Map(connections.map((c) => [c.id, c]));
  const dev = has("platform_admin", "asset_developer");
  return (
    <>
      <Toolbar placeholder={t("名称 / 类型")} onSearch={setKw}
        actions={dev && (
          <Space>
            <Button onClick={() => setOpen(true)}>{t("新建连接器实例")}</Button>
            <Button onClick={() => setImporting(true)}>{t("API 导入")}</Button>
          </Space>
        )} />
      {list.error && <Alert type="error" message={list.error} />}
      <ListTable<ConnectorInstance> pagination={PAGINATION} rowKey="id" loading={list.loading}
        dataSource={(list.data?.items ?? []).filter((c) => matches(c, kw, ["name", "kind"]))}
        expandable={{ expandedRowRender: (c) => <JsonView value={c.config} /> }}
        columns={[
          { title: t("名称"), dataIndex: "name", render: (v: string) => <Typography.Text strong>{v}</Typography.Text> },
          { title: t("类型"), dataIndex: "kind", render: (k: Kind) => <Tag>{KIND_LABEL[k] ?? k}</Tag> },
          { title: t("系统连接"), dataIndex: "system_connection_id", render: (id: string | null) => {
            const c = id ? byId.get(id) : undefined;
            return c ? `${c.system_key}（${c.env}）` : id ? <Typography.Text type="secondary">{id.slice(0, 8)}…</Typography.Text> : "-";
          } },
          { title: t("公司"), dataIndex: "company_id", render: (v) => <OrgName id={v} /> },
          { title: t("并发 / 速率 / 突发"), key: "limits", render: (_, c) => `${c.concurrency ?? "-"} / ${c.rate_per_s ?? "-"} / ${c.burst ?? "-"}` },
          { title: t("状态"), dataIndex: "status", render: (v) => (v ? <StateTag state={v} /> : "-") },
          {
            title: t("操作"), key: "ops", width: 120,
            render: (_, c) => c.kind === "sql_readonly" && dev
              ? <Button size="small" type="link" onClick={() => setSqlInst(c)}>{t("SQL 模板")}</Button>
              : null,
          },
        ]} />
      <NewConnector open={open} connections={connections} onClose={() => setOpen(false)} onDone={list.reload} />
      <ApiImportDrawer open={importing} connections={connections} onClose={() => setImporting(false)} onDone={list.reload} />
      <SqlTemplatesDrawer inst={sqlInst} onClose={() => setSqlInst(null)} />
    </>
  );
}

function NewConnector(p: { open: boolean; connections: Connection[]; onClose: () => void; onDone: () => void }) {
  const [form] = Form.useForm();
  const act = useAction();
  const kind = Form.useWatch("kind", form) as Kind | undefined;
  return (
    <Modal open={p.open} title={t("新建连接器实例")} width={720} onCancel={p.onClose} onOk={() => form.submit()} destroyOnClose>
      <Form form={form} layout="vertical" preserve={false}
        initialValues={{ kind: "declarative", concurrency: 8, rate_per_s: 20, burst: 40, config: JSON.stringify(CONFIG_TEMPLATE.declarative, null, 2) }}
        onValuesChange={(changed) => {
          if (changed.kind) form.setFieldValue("config", JSON.stringify(CONFIG_TEMPLATE[changed.kind as Kind], null, 2));
        }}
        onFinish={async (v) => {
          const body = {
            name: v.name, kind: v.kind, system_connection_id: v.system_connection_id ?? null,
            config: v.kind === "sql_readonly" ? {} : parseJson(v.config),
            concurrency: v.concurrency, rate_per_s: v.rate_per_s, burst: v.burst,
          };
          if (await act(() => post("/connector-instances", body), t("已登记"))) { p.onClose(); p.onDone(); }
        }}>
        <Space style={{ display: "flex" }} align="start">
          <Form.Item name="name" label={t("名称")} style={{ width: 260 }}
            rules={[{ required: true, pattern: /^[a-z][a-z0-9_]{2,63}$/, message: t("小写字母开头，仅含 a-z / 0-9 / _") }]}>
            <Input placeholder="mes_vn01" />
          </Form.Item>
          <Form.Item name="kind" label={t("类型")}>
            <Select style={{ width: 160 }} options={(Object.keys(KIND_LABEL) as Kind[]).map((k) => ({ value: k, label: KIND_LABEL[k] }))} />
          </Form.Item>
        </Space>
        <Form.Item name="system_connection_id" label={t("系统连接")}
          rules={[{ required: kind !== "mock", message: t("非 mock Connector 必须绑定系统连接") }]}>
          <Select allowClear options={p.connections.map((c) => ({ value: c.id, label: `${c.system_key}（${c.env}） · ${c.base_url}` }))} />
        </Form.Item>
        <Space style={{ display: "flex" }} align="start">
          <Form.Item name="concurrency" label={t("并发上限")}><InputNumber min={1} max={256} /></Form.Item>
          <Form.Item name="rate_per_s" label={t("每秒速率")}><InputNumber min={0.1} step={1} /></Form.Item>
          <Form.Item name="burst" label={t("突发")}><InputNumber min={1} /></Form.Item>
        </Space>
        {kind !== "sql_readonly" && (
          <Form.Item name="config" label={t("配置（JSON）")} rules={[{ required: true }, jsonRule]}>
            <JsonInput rows={12} />
          </Form.Item>
        )}
        {kind === "sql_readonly" && (
          <Alert type="info" showIcon message={t("数据库只读：登记后在「SQL 模板」中添加参数化 SELECT，禁止写库。")} />
        )}
      </Form>
    </Modal>
  );
}

function SqlTemplatesDrawer({ inst, onClose }: { inst: ConnectorInstance | null; onClose: () => void }) {
  const act = useAction();
  const templates = useLoad(
    () => (inst ? get<{ items: SqlTemplate[] }>(`/connector-instances/${inst.id}/sql-templates`) : Promise.reject(new Error("n/a"))),
    [inst?.id],
  );
  const [form] = Form.useForm();
  return (
    <Drawer open={Boolean(inst)} width={720} title={inst ? t("SQL 模板 · {0}", [inst.name]) : ""} onClose={onClose} destroyOnClose>
      {inst && (
        <Space direction="vertical" style={{ width: "100%" }} size="large">
          <Alert type="warning" showIcon message={t("仅允许 SELECT / WITH…SELECT；须使用只读账号与副本。")} />
          {templates.error && <Alert type="error" message={templates.error} />}
          <ListTable<SqlTemplate>
            rowKey="id" size="small" loading={templates.loading} pagination={false}
            dataSource={templates.data?.items ?? []}
            columns={[
              { title: t("模板键"), dataIndex: "template_key" },
              { title: t("引擎"), dataIndex: "engine", width: 90 },
              { title: t("状态"), dataIndex: "status", width: 90, render: (v) => <StateTag state={v} /> },
              {
                title: t("操作"), key: "ops", width: 160,
                render: (_, r) => (
                  <Space>
                    <Button size="small" type="link" disabled={r.status === "active"} onClick={async () => {
                      if (await act(() => post(`/sql-templates/${r.id}/activate`), t("已激活"))) templates.reload();
                    }}>{t("激活")}</Button>
                    <Button size="small" type="link" onClick={async () => {
                      const out = await act(() => post<{ row_count: number }>(`/sql-templates/${r.id}/try`, { params: { flag: 1 } }));
                      if (out) Modal.info({ title: t("试跑结果"), content: t("返回 {0} 行", [out.row_count]) });
                    }}>{t("试跑")}</Button>
                  </Space>
                ),
              },
            ]}
          />
          <Form form={form} layout="vertical" initialValues={{ engine: "postgres", max_rows: 200, statement: "SELECT 1 AS n WHERE :flag = 1" }}
            onFinish={async (v) => {
              const body = {
                template_key: v.template_key, engine: v.engine, statement: v.statement,
                param_schema: { type: "object", properties: { flag: { type: "integer" } } },
                max_rows: v.max_rows, explain_cost_cap: 1_000_000,
              };
              if (await act(() => post(`/connector-instances/${inst.id}/sql-templates`, body), t("已创建草稿"))) {
                form.resetFields(["template_key"]);
                templates.reload();
              }
            }}>
            <Typography.Title level={5}>{t("新建模板")}</Typography.Title>
            <Space style={{ display: "flex" }} align="start">
              <Form.Item name="template_key" label={t("模板键")} rules={[{ required: true, pattern: /^[a-z][a-z0-9_]{1,63}$/ }]} style={{ width: 200 }}>
                <Input placeholder="order_list" />
              </Form.Item>
              <Form.Item name="engine" label={t("引擎")}>
                <Select style={{ width: 140 }} options={[{ value: "postgres", label: "postgres" }, { value: "mysql", label: "mysql" }]} />
              </Form.Item>
              <Form.Item name="max_rows" label={t("最大行数")}><InputNumber min={1} max={1000} /></Form.Item>
            </Space>
            <Form.Item name="statement" label={t("SQL（命名参数 :name）")} rules={[{ required: true, min: 8 }]}>
              <Input.TextArea rows={4} />
            </Form.Item>
            <Button type="primary" htmlType="submit">{t("创建草稿")}</Button>
          </Form>
        </Space>
      )}
    </Drawer>
  );
}
