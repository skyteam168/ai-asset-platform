import { Alert, Button, DatePicker, Descriptions, Drawer, Form, Input, Modal, Select, Space, Switch, Tabs, Tag, Typography } from "antd";
import dayjs from "dayjs";
import { useState } from "react";
import { del, get, post, type Page } from "../api";
import { useAuth } from "../auth";
import { matches, OrgName, PageCard, PAGINATION, StateTag, Toolbar, useOrgs } from "../components";
import { ListTable } from "../ListTable";
import { useAction, useLoad } from "../hooks";
import type { Connection, ConnectionTest, UpstreamMcpSource } from "../types";
import { t } from "../i18n";
import { ConnectorInstancesTab } from "./ConnectorInstances";

/** 传输方式标签：明文与不校验证书醒目标出（architecture.md 9.6）。 */
export function TransportTag({ c }: { c: Pick<Connection, "scheme" | "tls_mode"> }) {
  if (c.scheme === "postgres" || c.scheme === "mysql") {
    const eng = c.scheme === "postgres" ? "PostgreSQL" : "MySQL";
    if (c.tls_mode === "insecure") return <Tag color="volcano">{eng} · {t("不校验证书")}</Tag>;
    return <Tag color="cyan">{eng}</Tag>;
  }
  if (c.scheme === "http") return <Tag color="red">{t("明文 HTTP")}</Tag>;
  if (c.tls_mode === "insecure") return <Tag color="volcano">{t("不校验证书")}</Tag>;
  if (c.tls_mode === "pinned") return <Tag color="blue">{t("HTTPS · 固定指纹")}</Tag>;
  if (c.tls_mode === "custom_ca") return <Tag color="blue">{t("HTTPS · 内部 CA")}</Tag>;
  return <Tag color="green">{t("HTTPS · 系统 CA")}</Tag>;
}

export function ConnectionsPage() {
  const { has } = useAuth();
  const conns = useLoad(() => get<Page<Connection>>("/connections"), []);
  const [open, setOpen] = useState(false);
  const [testing, setTesting] = useState<Connection | null>(null);
  const [kw, setKw] = useState("");
  const act = useAction();
  const systems = (
    <>
      <Toolbar placeholder={t("系统 / 地址 / 环境")} onSearch={setKw}
        actions={has("platform_admin", "asset_developer") && <Button onClick={() => setOpen(true)}>{t("登记连接")}</Button>} />
      {conns.error && <Alert type="error" message={conns.error} />}
      <ListTable<Connection> pagination={PAGINATION} rowKey="id" loading={conns.loading} dataSource={(conns.data?.items ?? []).filter((c) => matches(c, kw, ["system_key", "base_url", "env"]))}
        columns={[
          { title: t("系统"), dataIndex: "system_key", render: (v: string) => <Typography.Text strong>{v}</Typography.Text> },
          { title: t("环境"), dataIndex: "env" },
          { title: t("公司"), dataIndex: "company_id", render: (v) => <OrgName id={v} /> },
          { title: t("地址"), dataIndex: "base_url", render: (v) => <Typography.Text code>{v}</Typography.Text> },
          { title: t("传输"), key: "transport", render: (_, c) => <TransportTag c={c} /> },
          { title: t("例外到期"), dataIndex: "transport_exception_expires_at", render: (v) => (v ? String(v).slice(0, 10) : "-") },
          { title: t("状态"), dataIndex: "status", render: (v) => <StateTag state={v} /> },
          {
            title: t("操作"), key: "ops", render: (_, c) => (
              <Space>
                {(c.scheme === "http" || c.scheme === "https") && (
                  <Button size="small" onClick={() => setTesting(c)}>{t("连接测试")}</Button>
                )}
                {c.status === "pending_approval" && has("data_owner") && (
                  <Button size="small" type="primary" onClick={async () => {
                    const r = await act(() => post<{ status: string }>(`/connections/${c.id}/approve-exception`));
                    if (r) { Modal.info({ title: r.status === "active" ? t("已生效") : t("已记录审批，生产连接还需第二位数据 Owner") }); conns.reload(); }
                  }}>{t("审批例外")}</Button>
                )}
                {has("platform_admin", "asset_developer") && (
                  <Button size="small" danger onClick={() => Modal.confirm({
                    title: t("删除系统连接？"),
                    content: t("删除前须先移除绑定的连接器实例与上游 MCP 源。"),
                    okButtonProps: { danger: true },
                    onOk: async () => {
                      if (await act(() => del(`/connections/${c.id}`), t("已删除连接"))) conns.reload();
                    },
                  })}>{t("删除")}</Button>
                )}
              </Space>
            ),
          },
        ]} />
    </>
  );
  return (
    <PageCard>
      <Tabs items={[
        { key: "systems", label: t("系统连接"), children: systems },
        { key: "connectors", label: t("连接器实例"), children: <ConnectorInstancesTab connections={conns.data?.items ?? []} /> },
        { key: "upstream", label: t("上游 MCP 源"), children: <UpstreamMcpSourcesTab connections={conns.data?.items ?? []} /> },
      ]} />
      <NewConnection open={open} onClose={() => setOpen(false)} onDone={conns.reload} />
      <TestDrawer conn={testing} onClose={() => setTesting(null)} />
    </PageCard>
  );
}

function UpstreamMcpSourcesTab({ connections }: { connections: Connection[] }) {
  const { has } = useAuth();
  const sources = useLoad(() => get<Page<UpstreamMcpSource>>("/upstream-mcp-sources"), []);
  const [open, setOpen] = useState(false);
  const [kw, setKw] = useState("");
  const act = useAction();
  const byId = Object.fromEntries(connections.map((c) => [c.id, c]));
  return (
    <>
      <Toolbar placeholder={t("名称 / 哈希")} onSearch={setKw}
        actions={has("platform_admin", "asset_developer") && <Button onClick={() => setOpen(true)}>{t("登记上游 MCP 源")}</Button>} />
      {sources.error && <Alert type="error" message={sources.error} />}
      <ListTable<UpstreamMcpSource> pagination={PAGINATION} rowKey="id" loading={sources.loading}
        dataSource={(sources.data?.items ?? []).filter((s) => matches(s, kw, ["name", "tools_manifest_hash", "mcp_path"]))}
        columns={[
          { title: t("名称"), dataIndex: "name", render: (v: string) => <Typography.Text strong>{v}</Typography.Text> },
          { title: t("系统连接"), dataIndex: "system_connection_id", render: (id: string) => byId[id]?.system_key ?? id.slice(0, 8) },
          { title: t("路径"), dataIndex: "mcp_path", render: (v) => <Typography.Text code>{v}</Typography.Text> },
          { title: t("清单哈希"), dataIndex: "tools_manifest_hash", render: (v: string) => <Typography.Text code>{v.slice(0, 12)}…</Typography.Text> },
          { title: t("状态"), dataIndex: "status", render: (v) => <StateTag state={v} /> },
          {
            title: t("操作"), key: "ops", render: (_, s) => has("platform_admin", "asset_developer") && (
              <Space>
                {s.status === "active" && (
                  <Button size="small" onClick={async () => {
                    if (await act(() => post(`/upstream-mcp-sources/${s.id}/disable`), t("已禁用上游 MCP 源"))) sources.reload();
                  }}>{t("禁用")}</Button>
                )}
                <Button size="small" danger onClick={() => Modal.confirm({
                  title: t("删除上游 MCP 源？"),
                  content: t("仅删除登记记录；已创建的 upstream_mcp 连接器实例仍自持哈希。"),
                  okButtonProps: { danger: true },
                  onOk: async () => {
                    if (await act(() => del(`/upstream-mcp-sources/${s.id}`), t("已删除上游 MCP 源"))) sources.reload();
                  },
                })}>{t("删除")}</Button>
              </Space>
            ),
          },
        ]} />
      <NewUpstreamSource open={open} connections={connections} onClose={() => setOpen(false)} onDone={sources.reload} />
    </>
  );
}

function NewUpstreamSource(p: { open: boolean; connections: Connection[]; onClose: () => void; onDone: () => void }) {
  const [form] = Form.useForm();
  const act = useAction();
  const [pulling, setPulling] = useState(false);
  const [pinnedTools, setPinnedTools] = useState<Record<string, unknown>[]>([]);
  const httpConns = p.connections.filter((c) => c.scheme === "http" || c.scheme === "https");
  const pullHash = async () => {
    try {
      await form.validateFields(["system_connection_id", "mcp_path"]);
    } catch {
      return;
    }
    const cid = form.getFieldValue("system_connection_id") as string;
    const path = (form.getFieldValue("mcp_path") as string) || "/mcp";
    setPulling(true);
    try {
      const r = await act(
        () => post<{ tools_manifest_hash: string; tools: Record<string, unknown>[] }>(
          "/upstream-mcp-sources/preview-hash",
          { system_connection_id: cid, mcp_path: path },
        ),
        t("已拉取清单哈希"),
      );
      if (r) {
        form.setFieldsValue({ tools_manifest_hash: r.tools_manifest_hash });
        setPinnedTools(r.tools ?? []);
      }
    } finally {
      setPulling(false);
    }
  };
  return (
    <Modal open={p.open} title={t("登记上游 MCP 源")} width={560} onCancel={p.onClose} onOk={() => form.submit()} destroyOnClose
      afterClose={() => setPinnedTools([])}>
      <Form form={form} layout="vertical" preserve={false} initialValues={{ mcp_path: "/mcp" }}
        onFinish={async (v) => {
          const body = {
            name: v.name,
            system_connection_id: v.system_connection_id,
            mcp_path: v.mcp_path || "/mcp",
            tools_manifest_hash: String(v.tools_manifest_hash).trim().toLowerCase(),
            pinned_tools: pinnedTools,
          };
          if (await act(() => post("/upstream-mcp-sources", body), t("已登记上游 MCP 源"))) { p.onClose(); p.onDone(); }
        }}>
        <Form.Item name="name" label={t("名称")} rules={[{ required: true, message: t("请输入名称") }]}>
          <Input placeholder="weather_upstream" />
        </Form.Item>
        <Form.Item name="system_connection_id" label={t("系统连接")} rules={[{ required: true, message: t("请选择系统连接") }]}>
          <Select options={httpConns.map((c) => ({ value: c.id, label: `${c.system_key}（${c.env}）` }))}
            placeholder={httpConns.length ? undefined : t("请先登记 HTTP(S) 系统连接")} />
        </Form.Item>
        <Form.Item name="mcp_path" label={t("MCP 路径")} rules={[{ required: true }]}>
          <Input placeholder="/mcp" />
        </Form.Item>
        <Form.Item name="tools_manifest_hash" label={t("清单哈希（SHA-256）")}
          rules={[{ required: true, pattern: /^[0-9a-fA-F]{64}$/, message: t("须为 64 位十六进制") }]}
          extra={<Button type="link" size="small" loading={pulling} onClick={pullHash} style={{ padding: 0 }}>
            {t("从连接拉取哈希")}
          </Button>}>
          <Input placeholder="64 位小写十六进制" />
        </Form.Item>
      </Form>
    </Modal>
  );
}

function NewConnection(p: { open: boolean; onClose: () => void; onDone: () => void }) {
  const [form] = Form.useForm();
  const { companies } = useOrgs();
  const act = useAction();
  const scheme = Form.useWatch("scheme", form);
  const tlsMode = Form.useWatch("tls_mode", form);
  const isDb = scheme === "postgres" || scheme === "mysql";
  const exception = scheme === "http" || tlsMode === "insecure";
  return (
    <Modal open={p.open} title={t("登记系统连接")} width={640} onCancel={p.onClose} onOk={() => form.submit()} destroyOnClose>
      <Form form={form} layout="vertical" preserve={false}
        initialValues={{ scheme: "https", tls_mode: "system_ca", env: "test", tls_check_hostname: true }}
        onValuesChange={(ch) => {
          if (ch.scheme === "postgres") form.setFieldsValue({ base_url: "postgres://host:5432/dbname", tls_mode: "system_ca" });
          if (ch.scheme === "mysql") form.setFieldsValue({ base_url: "mysql://host:3306/dbname", tls_mode: "system_ca" });
          if (ch.scheme === "https") form.setFieldsValue({ base_url: "https://", tls_mode: "system_ca" });
          if (ch.scheme === "http") form.setFieldsValue({ base_url: "http://", tls_mode: undefined });
        }}
        onFinish={async (v) => {
          const body = {
            ...v,
            tls_mode: v.scheme === "http" ? undefined : v.tls_mode,
            pins: v.pin_sha256 ? [{ kind: v.pin_kind ?? "spki", sha256: v.pin_sha256.trim().toLowerCase() }] : [],
            transport_exception_expires_at: v.transport_exception_expires_at?.toISOString(),
          };
          delete body.pin_sha256;
          delete body.pin_kind;
          if (await act(() => post("/connections", body), exception ? t("已登记，待数据 Owner 审批") : t("已登记"))) { p.onClose(); p.onDone(); }
        }}>
        <Space style={{ display: "flex" }} align="start">
          <Form.Item name="company_id" label={t("公司")} rules={[{ required: true }]} style={{ width: 200 }}>
            <Select options={companies.map((c) => ({ value: c.id, label: c.code }))} />
          </Form.Item>
          <Form.Item name="system_key" label={t("系统")} rules={[{ required: true }]}><Input placeholder="mes" /></Form.Item>
          <Form.Item name="env" label={t("环境")}>
            <Select style={{ width: 110 }} options={["sandbox", "test", "staging", "prod"].map((e) => ({ value: e, label: e }))} />
          </Form.Item>
        </Space>
        <Form.Item name="base_url" label={t("地址")} rules={[{
          required: true,
          pattern: isDb ? /^(postgres|mysql):\/\// : /^https?:\/\//,
          message: isDb ? t("以 postgres:// 或 mysql:// 开头（勿含密码）") : t("以 http:// 或 https:// 开头"),
        }]}>
          <Input placeholder={isDb ? "postgres://10.10.1.20:5432/erp_ro" : "https://10.10.1.20:8443"} />
        </Form.Item>
        <Space style={{ display: "flex" }} align="start">
          <Form.Item name="scheme" label={t("协议")}>
            <Select style={{ width: 120 }} options={[
              { value: "https", label: "https" }, { value: "http", label: "http" },
              { value: "postgres", label: "postgres" }, { value: "mysql", label: "mysql" },
            ]} />
          </Form.Item>
          {(scheme === "https" || isDb) && (
            <Form.Item name="tls_mode" label={t("TLS 策略")}>
              <Select style={{ width: 220 }} options={isDb ? [
                { value: "system_ca", label: t("系统 CA") }, { value: "custom_ca", label: t("内部 CA") },
                { value: "insecure", label: t("不校验证书（例外）") },
              ] : [
                { value: "system_ca", label: t("系统 CA") }, { value: "custom_ca", label: t("内部 CA") },
                { value: "pinned", label: t("固定指纹（自签推荐）") }, { value: "insecure", label: t("不校验证书（例外）") }]} />
            </Form.Item>
          )}
          {tlsMode === "insecure" && !isDb && (
            <Form.Item name="tls_check_hostname" label={t("仍校验主机名")} valuePropName="checked"><Switch /></Form.Item>
          )}
        </Space>
        {scheme === "https" && tlsMode === "custom_ca" && (
          <Form.Item name="ca_pem" label={t("内部根 CA（PEM）")} rules={[{ required: true }]}><Input.TextArea rows={4} /></Form.Item>
        )}
        {scheme === "https" && tlsMode === "pinned" && (
          <Space style={{ display: "flex" }} align="start">
            <Form.Item name="pin_kind" label={t("指纹类型")} initialValue="spki">
              <Select style={{ width: 140 }} options={[{ value: "spki", label: t("公钥（推荐）") }, { value: "cert", label: t("整证书") }]} />
            </Form.Item>
            <Form.Item name="pin_sha256" label={t("SHA-256（可先用连接测试取得）")} rules={[{ required: true, pattern: /^[0-9a-fA-F]{64}$/, message: t("64 位十六进制") }]} style={{ flex: 1 }}>
              <Input />
            </Form.Item>
          </Space>
        )}
        {exception && (
          <>
            <Alert type="warning" showIcon style={{ marginBottom: 12 }}
              message={t("传输例外：仅限内网地址、需数据 Owner 审批（生产需两人）、最长 90 天，只能使用服务账号身份。")} />
            <Form.Item name="transport_exception_reason" label={t("例外理由")} rules={[{ required: true }]}><Input /></Form.Item>
            <Form.Item name="transport_exception_expires_at" label={t("到期日")} rules={[{ required: true }]}>
              <DatePicker disabledDate={(d) => d.isBefore(dayjs()) || d.isAfter(dayjs().add(90, "day"))} />
            </Form.Item>
          </>
        )}
        <Form.Item name="credential_ref" label={t("凭据引用（OpenBao 路径，可选）")}><Input placeholder="kv/data/mes/vn01" /></Form.Item>
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
  const cert = result?.chain?.[0];
  return (
    <Drawer open={Boolean(conn)} width={720} title={conn ? t("连接测试 · {0}（{1}）", [conn.system_key, conn.env]) : ""}
      onClose={() => { setResult(null); setDraft(null); onClose(); }} destroyOnClose>
      {conn && (
        <Space direction="vertical" style={{ width: "100%" }}>
          <Space><TransportTag c={conn} /><Typography.Text code>{conn.base_url}</Typography.Text></Space>
          <Button type="primary" loading={busy} onClick={run}>{t("执行测试（真实握手，不携带凭据）")}</Button>
          {result && (
            <>
              <Alert type={result.verdict === "pass" ? "success" : "error"} showIcon
                message={result.verdict === "pass" ? t("通过：{0}", [result.detail ?? ""]) : t("失败：{0}（{1}）", [result.detail ?? "", result.error_code])} />
              <Descriptions size="small" column={1} bordered>
                <Descriptions.Item label={t("解析地址")}>{result.resolved_ip ?? "-"} {result.allowlisted === false && <Tag color="red">{t("不在内网白名单")}</Tag>}</Descriptions.Item>
                <Descriptions.Item label={t("TLS 版本")}>{result.tls_version ?? "-"}</Descriptions.Item>
                {cert && <>
                  <Descriptions.Item label={t("证书主体")}>{cert.subject}</Descriptions.Item>
                  <Descriptions.Item label={t("颁发者")}>{cert.issuer} {cert.self_signed && <Tag>{t("自签")}</Tag>}</Descriptions.Item>
                  <Descriptions.Item label={t("有效期")}>{cert.not_before.slice(0, 10)} ~ {cert.not_after.slice(0, 10)}</Descriptions.Item>
                  <Descriptions.Item label="SAN">{cert.sans.join("，") || "-"}</Descriptions.Item>
                  <Descriptions.Item label={t("主机名匹配")}>{result.hostname_match ? <Tag color="green">{t("匹配")}</Tag> : <Tag color="orange">{t("不匹配")}</Tag>}</Descriptions.Item>
                </>}
                <Descriptions.Item label={t("证书指纹")}><Typography.Text copyable code style={{ fontSize: 11 }}>{result.fingerprint_cert_sha256 ?? "-"}</Typography.Text></Descriptions.Item>
                <Descriptions.Item label={t("公钥指纹")}><Typography.Text copyable code style={{ fontSize: 11 }}>{result.fingerprint_spki_sha256 ?? "-"}</Typography.Text></Descriptions.Item>
              </Descriptions>
              {result.fingerprint_spki_sha256 && result.verdict === "fail" && (
                <Button onClick={async () => { const d = await act(() => post(`/connections/${conn.id}/trust-draft`)); if (d) setDraft(d); }}>
                  {t("固定此证书（生成 pinned 草稿）")}
                </Button>
              )}
              {draft !== null && (
                <Alert type="info" showIcon message={t("配置草稿：按此登记 pinned 连接并走审批后生效")}
                  description={<pre style={{ margin: 0, fontSize: 12 }}>{JSON.stringify(draft, null, 2)}</pre>} />
              )}
            </>
          )}
        </Space>
      )}
    </Drawer>
  );
}
