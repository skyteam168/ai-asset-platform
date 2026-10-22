import { Alert, Button, Descriptions, Drawer, Form, Input, InputNumber, Modal, Select, Space, Tabs, Tag, Typography } from "antd";
import { useState } from "react";
import { get, post, put, type Page } from "../api";
import { useAuth } from "../auth";
import { Clearance, JsonInput, jsonRule, JsonView, matches, OrgName, PageCard, PAGINATION, parseJson, Toolbar, useOrgs } from "../components";
import { ListTable } from "../ListTable";
import { useAction, useLoad } from "../hooks";
import type { Binding, Consumer, CredentialMeta, User } from "../types";
import { t } from "../i18n";

const POLICY_LABEL = { strict: t("严格绑定"), by_employee_no: t("按工号匹配"), app_only: t("仅应用身份") };

export function ConsumersPage() {
  const { has } = useAuth();
  const consumers = useLoad(() => get<Page<Consumer>>("/consumers"), []);
  const [open, setOpen] = useState(false);
  const [detail, setDetail] = useState<Consumer | null>(null);
  const [kw, setKw] = useState("");
  return (
    <PageCard>
      <Toolbar placeholder={t("client_id / 联系人")} onSearch={setKw}
        actions={has("platform_admin") && <Button onClick={() => setOpen(true)}>{t("注册消费方")}</Button>} />
      {consumers.error && <Alert type="error" message={consumers.error} />}
      <ListTable<Consumer> pagination={PAGINATION} rowKey="id" loading={consumers.loading} dataSource={(consumers.data?.items ?? []).filter((c) => matches(c, kw, ["client_id"]))}
        onRow={(r) => ({ onClick: () => setDetail(r), style: { cursor: "pointer" } })}
        columns={[
          { title: "client_id", dataIndex: "client_id", render: (v: string) => <Typography.Link>{v}</Typography.Link> },
          { title: t("所属公司"), dataIndex: "company_id", render: (v) => <OrgName id={v} /> },
          { title: t("密级上限"), dataIndex: "clearance_cap", render: (v) => <Clearance level={v} /> },
          { title: t("绑定策略"), dataIndex: "bind_policy", render: (v: Consumer["bind_policy"]) => POLICY_LABEL[v] },
          {
            title: t("每日调用配额"),
            key: "quota",
            render: (_: unknown, c: Consumer) => c.quota?.daily_calls != null ? c.quota.daily_calls.toLocaleString() : t("无限制"),
          },
          { title: t("状态"), dataIndex: "status", render: (v) => <Tag color={v === "active" ? "green" : "red"}>{v}</Tag> },
        ]} />
      <NewConsumer open={open} onClose={() => setOpen(false)} onDone={consumers.reload} />
      <ConsumerDrawer consumer={detail} onClose={() => setDetail(null)} />
    </PageCard>
  );
}

function NewConsumer(p: { open: boolean; onClose: () => void; onDone: () => void }) {
  const [form] = Form.useForm();
  const { companies } = useOrgs();
  const act = useAction();
  return (
    <Modal open={p.open} title={t("注册消费方")} onCancel={p.onClose} onOk={() => form.submit()} destroyOnClose>
      <Form form={form} layout="vertical" preserve={false} initialValues={{ clearance_cap: 2, bind_policy: "strict" }}
        onFinish={async (v) => { if (await act(() => post("/consumers", v), t("已注册"))) { p.onClose(); p.onDone(); } }}>
        <Form.Item name="client_id" label="client_id" rules={[{ required: true, pattern: /^[a-z0-9][a-z0-9-]{2,63}$/, message: t("小写字母、数字、连字符，3 到 64 位") }]}>
          <Input placeholder="vn-ai-platform" />
        </Form.Item>
        <Form.Item name="company_id" label={t("所属公司")} rules={[{ required: true }]}>
          <Select options={companies.map((c) => ({ value: c.id, label: `${c.code} · ${c.name}` }))} />
        </Form.Item>
        <Form.Item name="clearance_cap" label={t("密级上限")}><InputNumber min={1} max={4} /></Form.Item>
        <Form.Item name="bind_policy" label={t("用户绑定策略")}>
          <Select options={Object.entries(POLICY_LABEL).map(([value, label]) => ({ value, label }))} />
        </Form.Item>
        <Form.Item name="contact" label={t("联系人")}><Input /></Form.Item>
      </Form>
    </Modal>
  );
}

function ConsumerDrawer({ consumer, onClose }: { consumer: Consumer | null; onClose: () => void }) {
  return (
    <Drawer open={Boolean(consumer)} width={760} title={consumer?.client_id} onClose={onClose} destroyOnClose>
      {consumer && (
        <>
          <Descriptions size="small" column={2} bordered>
            <Descriptions.Item label={t("所属公司")}><OrgName id={consumer.company_id} /></Descriptions.Item>
            <Descriptions.Item label={t("绑定策略")}>{POLICY_LABEL[consumer.bind_policy]}</Descriptions.Item>
          </Descriptions>
          <Tabs style={{ marginTop: 12 }} items={[
            { key: "bindings", label: t("用户绑定"), children: <Bindings consumer={consumer} /> },
            { key: "creds", label: t("凭据与公钥"), children: <Credentials consumer={consumer} /> },
            { key: "quota", label: t("配额"), children: <QuotaTab consumer={consumer} onDone={() => {/* consumers.reload called via prop not needed here */}} /> },
            { key: "webhooks", label: t("Webhook"), children: <Webhooks consumer={consumer} /> },
            { key: "debug", label: t("断言调试器"), children: <AssertionDebugger consumer={consumer} /> },
          ]} />
        </>
      )}
    </Drawer>
  );
}

function Bindings({ consumer }: { consumer: Consumer }) {
  const bindings = useLoad(() => get<Page<Binding>>(`/consumers/${consumer.id}/bindings`), [consumer.id]);
  const users = useLoad(() => get<Page<User>>("/users"), []);
  const [form] = Form.useForm();
  const act = useAction();
  const userName = (id: string) => users.data?.items.find((u) => u.id === id)?.display_name ?? id.slice(0, 8);
  return (
    <>
      <Form form={form} layout="inline" style={{ marginBottom: 12 }}
        onFinish={async (v) => {
          if (await act(() => post(`/consumers/${consumer.id}/bindings`, v), t("已绑定"))) { form.resetFields(); bindings.reload(); }
        }}>
        <Form.Item name="external_subject" rules={[{ required: true, message: t("必填") }]}><Input placeholder={t("消费方侧员工标识")} /></Form.Item>
        <Form.Item name="user_id" rules={[{ required: true, message: t("必选") }]}>
          <Select showSearch optionFilterProp="label" placeholder={t("平台用户")} style={{ width: 220 }}
            options={(users.data?.items ?? []).map((u) => ({ value: u.id, label: `${u.display_name}${u.employee_no ? `（${u.employee_no}）` : ""}` }))} />
        </Form.Item>
        <Button htmlType="submit" type="primary">{t("添加绑定")}</Button>
      </Form>
      <ListTable<Binding> rowKey="id" size="small" loading={bindings.loading} dataSource={bindings.data?.items ?? []}
        columns={[
          { title: t("消费方侧标识"), dataIndex: "external_subject" },
          { title: t("平台用户"), dataIndex: "user_id", render: userName },
          { title: t("到期"), dataIndex: "expires_at", render: (v: string) => v.slice(0, 10) },
        ]} />
    </>
  );
}

function Credentials({ consumer }: { consumer: Consumer }) {
  const { has } = useAuth();
  const act = useAction();
  const [token, setToken] = useState<string | null>(null);
  const [form] = Form.useForm();
  const credList = useLoad(() => get<Page<CredentialMeta>>(`/consumers/${consumer.id}/credentials`), [consumer.id]);
  if (!has("platform_admin")) return <Alert type="info" message={t("凭据与公钥由平台管理员维护。")} />;
  return (
    <Space direction="vertical" style={{ width: "100%" }}>
      <Button onClick={async () => {
        const r = await act(() => post<{ token: string }>(`/consumers/${consumer.id}/credentials`, { kind: "static_token" }));
        if (r) { setToken(r.token); credList.reload(); }
      }}>{t("签发静态 API Token（90 天）")}</Button>
      {token && (
        <Alert type="warning" showIcon message={t("只显示这一次，请立即交给消费方保存")}
          description={<Typography.Paragraph copyable code style={{ margin: 0 }}>{token}</Typography.Paragraph>} />
      )}
      <ListTable<CredentialMeta> rowKey="id" size="small" loading={credList.loading} pagination={false}
        dataSource={credList.data?.items ?? []}
        locale={{ emptyText: t("无凭据") }}
        columns={[
          { title: t("类型"), dataIndex: "kind" },
          { title: t("状态"), dataIndex: "status", render: (v) => <Tag color={v === "active" ? "green" : "default"}>{v}</Tag> },
          { title: t("创建"), dataIndex: "created_at", render: (v: string) => v.slice(0, 10) },
          { title: t("到期"), dataIndex: "expires_at", render: (v: string | null) => (v ? v.slice(0, 10) : "-") },
          {
            title: t("操作"), key: "ops", width: 80,
            render: (_, r) => r.status === "active"
              ? <Button size="small" danger onClick={() => Modal.confirm({
                  title: t("吊销该凭据？"),
                  onOk: async () => { if (await act(() => post(`/credentials/${r.id}/revoke`), t("已吊销"))) credList.reload(); },
                })}>{t("吊销")}</Button>
              : null,
          },
        ]} />
      <Form form={form} layout="vertical" onFinish={async (v) => {
        if (await act(() => post(`/consumers/${consumer.id}/jwks`, parseJson(v.jwk)), t("公钥已登记"))) form.resetFields();
      }}>
        <Form.Item name="jwk" label={t("登记用户断言验签公钥（JWK，只能是公钥）")} rules={[{ required: true }, jsonRule]}>
          <JsonInput rows={5} />
        </Form.Item>
        <Button htmlType="submit">{t("登记公钥")}</Button>
      </Form>
    </Space>
  );
}

function QuotaTab({ consumer }: { consumer: Consumer; onDone: () => void }) {
  const [form] = Form.useForm();
  const act = useAction();
  return (
    <Space direction="vertical" style={{ width: "100%" }}>
      <Alert type="info" showIcon message={t("留空表示不限制；配额按自然日重置。")} style={{ marginBottom: 8 }} />
      <Form form={form} layout="vertical" style={{ maxWidth: 400 }}
        initialValues={{ daily_calls: consumer.quota?.daily_calls ?? null, daily_writes: consumer.quota?.daily_writes ?? null }}
        onFinish={async (v) => {
          const body: Record<string, number | undefined> = {};
          if (v.daily_calls != null) body.daily_calls = v.daily_calls;
          if (v.daily_writes != null) body.daily_writes = v.daily_writes;
          await act(() => put(`/consumers/${consumer.id}/quota`, body), t("配额已更新"));
        }}>
        <Form.Item name="daily_calls" label={t("每日调用上限")}>
          <InputNumber min={1} style={{ width: "100%" }} placeholder={t("留空不限制")} />
        </Form.Item>
        <Form.Item name="daily_writes" label={t("每日写操作上限")}>
          <InputNumber min={1} style={{ width: "100%" }} placeholder={t("留空不限制")} />
        </Form.Item>
        <Button htmlType="submit" type="primary">{t("保存配额")}</Button>
      </Form>
    </Space>
  );
}

interface WebhookEndpoint {
  id: string;
  url: string;
  status: string;
  events: string[];
  created_at: string;
}
interface WebhookDelivery {
  id: string;
  event_kind: string;
  status: string;
  attempts: number;
  last_status_code: number | null;
  last_error: string | null;
  created_at: string;
}

function Webhooks({ consumer }: { consumer: Consumer }) {
  const list = useLoad(() => get<Page<WebhookEndpoint>>(`/consumers/${consumer.id}/webhooks`), [consumer.id]);
  const [form] = Form.useForm();
  const act = useAction();
  const [secret, setSecret] = useState<string | null>(null);
  const [deliveries, setDeliveries] = useState<WebhookDelivery[] | null>(null);
  return (
    <Space direction="vertical" style={{ width: "100%" }}>
      <Form form={form} layout="vertical"
        onFinish={async (v) => {
          const r = await act(() => post<{ secret: string }>(`/consumers/${consumer.id}/webhooks`, {
            events: v.events, url: v.url,
          }), t("已登记"));
          if (r) { setSecret(r.secret); form.resetFields(); list.reload(); }
        }}>
        <Form.Item name="url" label={t("回调地址")} rules={[{ required: true }]}>
          <Input placeholder="https://example.com/hooks/aip" />
        </Form.Item>
        <Form.Item name="events" label={t("事件")} rules={[{ required: true }]}
          initialValue={["catalog.changed", "risk.triggered"]}>
          <Select mode="multiple" options={[
            { value: "catalog.changed", label: "catalog.changed" },
            { value: "run.finished", label: "run.finished" },
            { value: "risk.triggered", label: "risk.triggered" },
          ]} />
        </Form.Item>
        <Button htmlType="submit" type="primary">{t("登记 Webhook")}</Button>
      </Form>
      {secret && (
        <Alert type="warning" showIcon message={t("密钥只显示这一次，请交给接收方保存")}
          description={<Typography.Paragraph copyable code style={{ margin: 0 }}>{secret}</Typography.Paragraph>} />
      )}
      <ListTable<WebhookEndpoint> rowKey="id" size="small" loading={list.loading} dataSource={list.data?.items ?? []}
        columns={[
          { title: "URL", dataIndex: "url", ellipsis: true },
          { title: t("事件"), dataIndex: "events", render: (v: string[]) => v?.join(", ") },
          { title: t("状态"), dataIndex: "status", render: (v) => <Tag color={v === "active" ? "green" : "default"}>{v}</Tag> },
          {
            title: t("操作"), width: 220,
            render: (_, r) => (
              <Space size="small">
                <Button size="small" onClick={async () => {
                  const res = await act(() => post<{ secret: string }>(`/webhooks/${r.id}/rotate-secret`, {}));
                  if (res) setSecret(res.secret);
                }}>{t("轮换密钥")}</Button>
                <Button size="small" onClick={async () => {
                  await act(() => put(`/webhooks/${r.id}/status`, { status: r.status === "active" ? "disabled" : "active" }));
                  list.reload();
                }}>{r.status === "active" ? t("停用") : t("启用")}</Button>
                <Button size="small" onClick={async () => {
                  const d = await get<Page<WebhookDelivery>>(`/webhooks/${r.id}/deliveries`);
                  setDeliveries(d.items);
                }}>{t("投递记录")}</Button>
              </Space>
            ),
          },
        ]} />
      {deliveries && (
        <ListTable<WebhookDelivery> rowKey="id" size="small" dataSource={deliveries} pagination={false}
          columns={[
            { title: t("事件"), dataIndex: "event_kind" },
            { title: t("状态"), dataIndex: "status" },
            { title: t("次数"), dataIndex: "attempts", width: 60 },
            { title: "HTTP", dataIndex: "last_status_code", width: 70 },
            { title: t("错误"), dataIndex: "last_error", ellipsis: true },
            { title: t("时间"), dataIndex: "created_at", render: (v: string) => v.slice(0, 19).replace("T", " ") },
          ]} />
      )}
    </Space>
  );
}

interface DebugResult { signature_valid: boolean; claims_valid: boolean; binding: string | null; errors: string[]; principal: unknown }

function AssertionDebugger({ consumer }: { consumer: Consumer }) {
  const [result, setResult] = useState<DebugResult | null>(null);
  const act = useAction();
  return (
    <Form layout="vertical" onFinish={async (v) => {
      const r = await act(() => post<DebugResult>("/assertion-debug", { consumer_id: consumer.id, assertion: v.assertion.trim() }));
      if (r) setResult(r);
    }}>
      <Alert type="info" showIcon style={{ marginBottom: 12 }}
        message={t("粘贴消费方签发的用户断言（JWT），查看验签、声明、绑定与最终 Principal。不消耗 jti，不记录断言内容。")} />
      <Form.Item name="assertion" rules={[{ required: true }]}><Input.TextArea rows={4} style={{ fontFamily: "Consolas, monospace" }} /></Form.Item>
      <Button htmlType="submit" type="primary">{t("调试")}</Button>
      {result && (
        <div style={{ marginTop: 12 }}>
          <Space>
            <Tag color={result.signature_valid ? "green" : "red"}>{t("签名")}{result.signature_valid ? t("有效") : t("无效")}</Tag>
            <Tag color={result.claims_valid ? "green" : "red"}>{t("声明")}{result.claims_valid ? t("有效") : t("无效")}</Tag>
            <Tag color={result.binding ? "green" : "orange"}>{result.binding ? t("已绑定") : t("未绑定")}</Tag>
          </Space>
          {result.errors.map((e) => <Alert key={e} type="error" message={e} style={{ marginTop: 8 }} />)}
          {result.principal !== null && <div style={{ marginTop: 8 }}><JsonView value={result.principal} /></div>}
        </div>
      )}
    </Form>
  );
}
