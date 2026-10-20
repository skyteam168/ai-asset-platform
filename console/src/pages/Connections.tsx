import { Alert, Button, DatePicker, Descriptions, Drawer, Form, Input, Modal, Select, Space, Switch, Table, Tag, Typography } from "antd";
import dayjs from "dayjs";
import { useState } from "react";
import { get, post, type Page } from "../api";
import { useAuth } from "../auth";
import { OrgName, StateTag, useOrgs } from "../components";
import { useAction, useLoad } from "../hooks";
import type { Connection, ConnectionTest } from "../types";

/** 传输方式标签：明文与不校验证书醒目标出（architecture.md 9.6）。 */
export function TransportTag({ c }: { c: Pick<Connection, "scheme" | "tls_mode"> }) {
  if (c.scheme === "http") return <Tag color="red">明文 HTTP</Tag>;
  if (c.tls_mode === "insecure") return <Tag color="volcano">不校验证书</Tag>;
  if (c.tls_mode === "pinned") return <Tag color="blue">HTTPS · 固定指纹</Tag>;
  if (c.tls_mode === "custom_ca") return <Tag color="blue">HTTPS · 内部 CA</Tag>;
  return <Tag color="green">HTTPS · 系统 CA</Tag>;
}

export function ConnectionsPage() {
  const { has } = useAuth();
  const conns = useLoad(() => get<Page<Connection>>("/connections"), []);
  const [open, setOpen] = useState(false);
  const [testing, setTesting] = useState<Connection | null>(null);
  const act = useAction();
  return (
    <>
      <Space style={{ marginBottom: 12 }}>
        <Typography.Title level={4} style={{ margin: 0 }}>系统连接</Typography.Title>
        {has("platform_admin", "asset_developer") && <Button type="primary" onClick={() => setOpen(true)}>登记连接</Button>}
      </Space>
      {conns.error && <Alert type="error" message={conns.error} />}
      <Table<Connection> rowKey="id" size="small" loading={conns.loading} dataSource={conns.data?.items ?? []}
        columns={[
          { title: "系统", dataIndex: "system_key" },
          { title: "环境", dataIndex: "env" },
          { title: "公司", dataIndex: "company_id", render: (v) => <OrgName id={v} /> },
          { title: "地址", dataIndex: "base_url", render: (v) => <Typography.Text code>{v}</Typography.Text> },
          { title: "传输", key: "transport", render: (_, c) => <TransportTag c={c} /> },
          { title: "例外到期", dataIndex: "transport_exception_expires_at", render: (v) => (v ? String(v).slice(0, 10) : "-") },
          { title: "状态", dataIndex: "status", render: (v) => <StateTag state={v} /> },
          {
            title: "操作", key: "ops", render: (_, c) => (
              <Space>
                <Button size="small" onClick={() => setTesting(c)}>连接测试</Button>
                {c.status === "pending_approval" && has("data_owner") && (
                  <Button size="small" type="primary" onClick={async () => {
                    const r = await act(() => post<{ status: string }>(`/connections/${c.id}/approve-exception`));
                    if (r) { Modal.info({ title: r.status === "active" ? "已生效" : "已记录审批，生产连接还需第二位数据 Owner" }); conns.reload(); }
                  }}>审批例外</Button>
                )}
              </Space>
            ),
          },
        ]} />
      <NewConnection open={open} onClose={() => setOpen(false)} onDone={conns.reload} />
      <TestDrawer conn={testing} onClose={() => setTesting(null)} />
    </>
  );
}

function NewConnection(p: { open: boolean; onClose: () => void; onDone: () => void }) {
  const [form] = Form.useForm();
  const { companies } = useOrgs();
  const act = useAction();
  const scheme = Form.useWatch("scheme", form);
  const tlsMode = Form.useWatch("tls_mode", form);
  const exception = scheme === "http" || tlsMode === "insecure";
  return (
    <Modal open={p.open} title="登记系统连接" width={640} onCancel={p.onClose} onOk={() => form.submit()} destroyOnClose>
      <Form form={form} layout="vertical" preserve={false}
        initialValues={{ scheme: "https", tls_mode: "system_ca", env: "test", tls_check_hostname: true }}
        onFinish={async (v) => {
          const body = {
            ...v,
            tls_mode: v.scheme === "http" ? undefined : v.tls_mode,
            pins: v.pin_sha256 ? [{ kind: v.pin_kind ?? "spki", sha256: v.pin_sha256.trim().toLowerCase() }] : [],
            transport_exception_expires_at: v.transport_exception_expires_at?.toISOString(),
          };
          delete body.pin_sha256;
          delete body.pin_kind;
          if (await act(() => post("/connections", body), exception ? "已登记，待数据 Owner 审批" : "已登记")) { p.onClose(); p.onDone(); }
        }}>
        <Space style={{ display: "flex" }} align="start">
          <Form.Item name="company_id" label="公司" rules={[{ required: true }]} style={{ width: 200 }}>
            <Select options={companies.map((c) => ({ value: c.id, label: c.code }))} />
          </Form.Item>
          <Form.Item name="system_key" label="系统" rules={[{ required: true }]}><Input placeholder="mes" /></Form.Item>
          <Form.Item name="env" label="环境">
            <Select style={{ width: 110 }} options={["sandbox", "test", "staging", "prod"].map((e) => ({ value: e, label: e }))} />
          </Form.Item>
        </Space>
        <Form.Item name="base_url" label="地址" rules={[{ required: true, pattern: /^https?:\/\//, message: "以 http:// 或 https:// 开头" }]}>
          <Input placeholder="https://10.10.1.20:8443" />
        </Form.Item>
        <Space style={{ display: "flex" }} align="start">
          <Form.Item name="scheme" label="协议"><Select style={{ width: 110 }} options={[{ value: "https", label: "https" }, { value: "http", label: "http" }]} /></Form.Item>
          {scheme === "https" && (
            <Form.Item name="tls_mode" label="TLS 策略">
              <Select style={{ width: 220 }} options={[
                { value: "system_ca", label: "系统 CA" }, { value: "custom_ca", label: "内部 CA" },
                { value: "pinned", label: "固定指纹（自签推荐）" }, { value: "insecure", label: "不校验证书（例外）" }]} />
            </Form.Item>
          )}
          {tlsMode === "insecure" && (
            <Form.Item name="tls_check_hostname" label="仍校验主机名" valuePropName="checked"><Switch /></Form.Item>
          )}
        </Space>
        {scheme === "https" && tlsMode === "custom_ca" && (
          <Form.Item name="ca_pem" label="内部根 CA（PEM）" rules={[{ required: true }]}><Input.TextArea rows={4} /></Form.Item>
        )}
        {scheme === "https" && tlsMode === "pinned" && (
          <Space style={{ display: "flex" }} align="start">
            <Form.Item name="pin_kind" label="指纹类型" initialValue="spki">
              <Select style={{ width: 140 }} options={[{ value: "spki", label: "公钥（推荐）" }, { value: "cert", label: "整证书" }]} />
            </Form.Item>
            <Form.Item name="pin_sha256" label="SHA-256（可先用连接测试取得）" rules={[{ required: true, pattern: /^[0-9a-fA-F]{64}$/, message: "64 位十六进制" }]} style={{ flex: 1 }}>
              <Input />
            </Form.Item>
          </Space>
        )}
        {exception && (
          <>
            <Alert type="warning" showIcon style={{ marginBottom: 12 }}
              message="传输例外：仅限内网地址、需数据 Owner 审批（生产需两人）、最长 90 天，只能使用服务账号身份。" />
            <Form.Item name="transport_exception_reason" label="例外理由" rules={[{ required: true }]}><Input /></Form.Item>
            <Form.Item name="transport_exception_expires_at" label="到期日" rules={[{ required: true }]}>
              <DatePicker disabledDate={(d) => d.isBefore(dayjs()) || d.isAfter(dayjs().add(90, "day"))} />
            </Form.Item>
          </>
        )}
        <Form.Item name="credential_ref" label="凭据引用（OpenBao 路径，可选）"><Input placeholder="kv/data/mes/vn01" /></Form.Item>
      </Form>
    </Modal>
  );
}

function TestDrawer({ conn, onClose }: { conn: Connection | null; onClose: () => void }) {
  const [result, setResult] = useState<ConnectionTest | null>(null);
  const [draft, setDraft] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const act = useAction();
  const run = async () => {
    if (!conn) return;
    setBusy(true);
    setDraft(null);
    const r = await act(() => post<ConnectionTest>(`/connections/${conn.id}/test`));
    setResult(r ?? null);
    setBusy(false);
  };
  const cert = result?.chain[0];
  return (
    <Drawer open={Boolean(conn)} width={720} title={conn ? `连接测试 · ${conn.system_key}（${conn.env}）` : ""}
      onClose={() => { setResult(null); setDraft(null); onClose(); }} destroyOnClose>
      {conn && (
        <Space direction="vertical" style={{ width: "100%" }}>
          <Space><TransportTag c={conn} /><Typography.Text code>{conn.base_url}</Typography.Text></Space>
          <Button type="primary" loading={busy} onClick={run}>执行测试（真实握手，不携带凭据）</Button>
          {result && (
            <>
              <Alert type={result.verdict === "pass" ? "success" : "error"} showIcon
                message={result.verdict === "pass" ? `通过：${result.detail ?? ""}` : `失败：${result.detail ?? ""}（${result.error_code}）`} />
              <Descriptions size="small" column={1} bordered>
                <Descriptions.Item label="解析地址">{result.resolved_ip ?? "-"} {result.allowlisted === false && <Tag color="red">不在内网白名单</Tag>}</Descriptions.Item>
                <Descriptions.Item label="TLS 版本">{result.tls_version ?? "-"}</Descriptions.Item>
                {cert && <>
                  <Descriptions.Item label="证书主体">{cert.subject}</Descriptions.Item>
                  <Descriptions.Item label="颁发者">{cert.issuer} {cert.self_signed && <Tag>自签</Tag>}</Descriptions.Item>
                  <Descriptions.Item label="有效期">{cert.not_before.slice(0, 10)} ~ {cert.not_after.slice(0, 10)}</Descriptions.Item>
                  <Descriptions.Item label="SAN">{cert.sans.join("，") || "-"}</Descriptions.Item>
                  <Descriptions.Item label="主机名匹配">{result.hostname_match ? <Tag color="green">匹配</Tag> : <Tag color="orange">不匹配</Tag>}</Descriptions.Item>
                </>}
                <Descriptions.Item label="证书指纹"><Typography.Text copyable code style={{ fontSize: 11 }}>{result.fingerprint_cert_sha256 ?? "-"}</Typography.Text></Descriptions.Item>
                <Descriptions.Item label="公钥指纹"><Typography.Text copyable code style={{ fontSize: 11 }}>{result.fingerprint_spki_sha256 ?? "-"}</Typography.Text></Descriptions.Item>
              </Descriptions>
              {result.fingerprint_spki_sha256 && result.verdict === "fail" && (
                <Button onClick={async () => { const d = await act(() => post(`/connections/${conn.id}/trust-draft`)); if (d) setDraft(d); }}>
                  固定此证书（生成 pinned 草稿）
                </Button>
              )}
              {draft !== null && (
                <Alert type="info" showIcon message="配置草稿：按此登记 pinned 连接并走审批后生效"
                  description={<pre style={{ margin: 0, fontSize: 12 }}>{JSON.stringify(draft, null, 2)}</pre>} />
              )}
            </>
          )}
        </Space>
      )}
    </Drawer>
  );
}
