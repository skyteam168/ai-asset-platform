/** 简易接入向导：登记连接 → 鉴权（无/Cookie）→ 手工接口点测 → 勾选生成 Tool 草稿。 */
import {
  Alert, Button, DatePicker, Form, Input, Radio, Select, Space, Steps, Table, Tag, Typography,
} from "antd";
import dayjs from "dayjs";
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { get, post, put, type Page } from "../api";
import { JsonView, PageCard, useOrgs } from "../components";
import { useAction, useLoad } from "../hooks";
import { t } from "../i18n";
import type { Connection } from "../types";

interface OpRow {
  key: string;
  operation: string;
  method: string;
  path: string;
  success_when?: string;
  data: string;
  title?: string;
  description?: string;
  selected: boolean;
  sample?: unknown;
  tryError?: string;
}

interface DraftOut {
  connector_config: Record<string, unknown>;
  tools: { capability_id: string; description?: string }[];
  warnings: string[];
  connector_instance_id?: string | null;
  drafts?: { asset_id: string; version_id: string; capability_id: string; state?: string }[];
}

type AuthMode = "none" | "cookie";

/** 中文系统名等 → 合法小写标识；无法派生时回落 fallback。 */
function toIdent(raw: string, fallback: string): string {
  const cleaned = raw.toLowerCase().replace(/[^a-z0-9_]+/g, "_").replace(/^_+|_+$/g, "").replace(/_+/g, "_");
  if (/^[a-z][a-z0-9_]*$/.test(cleaned)) return cleaned.slice(0, 64);
  return fallback;
}

export function EasyConnectPage() {
  const { companies } = useOrgs();
  const act = useAction();
  const existing = useLoad(() => get<Page<Connection>>("/connections"), []);
  const [step, setStep] = useState(0);
  const [connForm] = Form.useForm();
  const [authForm] = Form.useForm();
  const [connectionId, setConnectionId] = useState<string | null>(null);
  const [useExisting, setUseExisting] = useState(false);
  const [authMode, setAuthMode] = useState<AuthMode>("none");
  const [authCfg, setAuthCfg] = useState<Record<string, unknown>>({ mode: "none" });
  const [authOk, setAuthOk] = useState<string | null>(null);
  const [ops, setOps] = useState<OpRow[]>([
    {
      key: "1",
      operation: "data_report",
      method: "GET",
      path: "/index/welcome/data_report.html",
      success_when: '$.msg == "success"',
      data: "$.data",
      title: t("看板数据"),
      description: t("欢迎页报修统计列表，只读接口用于演示简易接入"),
      selected: true,
    },
  ]);
  const [opForm] = Form.useForm();
  const [result, setResult] = useState<DraftOut | null>(null);
  const scheme = Form.useWatch("scheme", connForm) as string | undefined;
  const exception = scheme === "http";

  const connectorConfig = useMemo(
    () => ({
      auth: authCfg,
      operations: ops.map((o) => ({
        operation: o.operation,
        mode: o.method === "GET" || o.method === "HEAD" ? "read" : "write",
        request: { method: o.method, path: o.path, headers: { Accept: "application/json" } },
        response: {
          data: o.data || "$.data",
          ...(o.success_when ? { success_when: o.success_when } : {}),
        },
      })),
    }),
    [ops, authCfg],
  );

  const createConnection = async () => {
    if (useExisting) {
      const v = await connForm.validateFields(["existing_id"]);
      if (!v.existing_id) return;
      const row = (existing.data?.items ?? []).find((c) => c.id === v.existing_id);
      if (row) {
        connForm.setFieldsValue({
          company_id: row.company_id,
          system_key: row.system_key,
          env: row.env,
          base_url: row.base_url,
          scheme: row.scheme,
        });
      }
      setConnectionId(v.existing_id as string);
      setStep(1);
      return;
    }
    const v = await connForm.validateFields();
    const body = {
      ...v,
      tls_mode: v.scheme === "http" ? undefined : v.tls_mode,
      transport_exception_expires_at: v.transport_exception_expires_at?.toISOString?.() ?? v.transport_exception_expires_at,
      pins: [],
    };
    const created = await act(() => post<Connection>("/connections", body), t("已登记系统连接"));
    if (!created?.id) return;
    setConnectionId(created.id);
    existing.reload();
    setStep(1);
  };

  const saveAuth = async () => {
    if (!connectionId) return;
    if (authMode === "none") {
      setAuthCfg({ mode: "none" });
      setAuthOk(t("已选择无需登录"));
      setStep(2);
      return;
    }
    const v = await authForm.validateFields();
    const cred = await act(
      () => put<{ id: string; credential_ref: string }>(`/connections/${connectionId}/credentials`, {
        username: v.username,
        password: v.password,
      }),
      t("凭据已写入密管"),
    );
    if (!cred) return;
    const auth = {
      mode: "cookie",
      path: v.login_path,
      body_encoding: "form",
      username_field: v.username_field,
      password_field: v.password_field,
      cookie_name: v.cookie_name,
      success_when: v.success_when,
      ttl_s: 7200,
    };
    const probe = await act(() => post<{ ok: boolean; detail?: string; cookie_name?: string; expires_in_s?: number }>(
      `/connections/${connectionId}/test-auth`,
      { auth },
    ));
    if (!probe) return;
    if (!probe.ok) {
      setAuthOk(null);
      return;
    }
    setAuthCfg(auth);
    setAuthOk(t("登录测试通过（Cookie {0}，约 {1} 秒）", [probe.cookie_name ?? "PHPSESSID", String(probe.expires_in_s ?? "?")]));
    setStep(2);
  };

  const addOp = async () => {
    const v = await opForm.validateFields();
    setOps((prev) => [
      ...prev,
      {
        key: `${Date.now()}`,
        operation: v.operation,
        method: v.method,
        path: v.path,
        success_when: v.success_when || undefined,
        data: v.data || "$.data",
        title: v.title,
        description: v.description,
        selected: true,
      },
    ]);
    opForm.resetFields(["operation", "path", "title", "description", "success_when", "data"]);
  };

  const tryOp = async (row: OpRow) => {
    if (!connectionId) return;
    const out = await act(() => post<{ ok: boolean; sample?: unknown; item_count?: number }>(
      "/importer/try-operation",
      {
        connection_id: connectionId,
        connector_config: connectorConfig,
        operation: row.operation,
        params: {},
        allow_write: false,
      },
    ));
    setOps((prev) => prev.map((o) => (
      o.key === row.key
        ? { ...o, sample: out?.sample, tryError: out ? undefined : t("点测失败") }
        : o
    )));
  };

  const persist = async () => {
    if (!connectionId) return;
    const selected = ops.filter((o) => o.selected);
    if (!selected.length) return;
    const sk = String(connForm.getFieldValue("system_key") || "repair");
    const system = toIdent(sk, "repair");
    const connector_name = toIdent(`${system}_easy`, "repair_easy");
    const body = {
      connector_name,
      domain: "facility",
      system,
      connection_id: connectionId,
      auth: authCfg,
      persist: true,
      operations: selected.map((o) => ({
        operation: o.operation,
        method: o.method,
        path: o.path,
        success_when: o.success_when,
        data: o.data,
        title: o.title,
        description: o.description,
      })),
    };
    const r = await act(() => post<DraftOut>("/importer/manual", body), t("已生成 Tool 草稿"));
    if (r) {
      setResult(r);
      setStep(3);
    }
  };

  return (
    <PageCard>
      <Typography.Title level={4} style={{ marginTop: 0 }}>{t("简易接入")}</Typography.Title>
      <Typography.Paragraph type="secondary">
        {t("四步：登记系统 → 鉴权（可选 Cookie 会话）→ 手工添加接口并点测 → 勾选生成 Tool 草稿。上架 MCP 仍须过门禁与授权。")}
      </Typography.Paragraph>
      <Steps
        current={step}
        style={{ marginBottom: 24 }}
        items={[
          { title: t("系统") },
          { title: t("鉴权") },
          { title: t("接口") },
          { title: t("生成") },
        ]}
      />

      {step === 0 && (
        <Form form={connForm} layout="vertical" initialValues={{ scheme: "http", env: "test", tls_mode: "system_ca", system_key: "repair" }}
          onValuesChange={(ch) => {
            if (ch.scheme === "http") connForm.setFieldsValue({ base_url: "http://" });
            if (ch.scheme === "https") connForm.setFieldsValue({ base_url: "https://", tls_mode: "system_ca" });
          }}>
          <Radio.Group
            value={useExisting ? "existing" : "new"}
            onChange={(e) => setUseExisting(e.target.value === "existing")}
            style={{ marginBottom: 16 }}
            options={[
              { value: "new", label: t("新建系统连接") },
              { value: "existing", label: t("选用已有连接") },
            ]}
          />
          {useExisting ? (
            <Form.Item name="existing_id" label={t("已有连接")} rules={[{ required: true, message: t("请选择已有连接") }]}>
              <Select
                loading={existing.loading}
                placeholder={t("系统 / 环境 / 地址")}
                options={(existing.data?.items ?? []).map((c) => ({
                  value: c.id,
                  label: `${c.system_key}（${c.env}） · ${c.base_url} · ${c.status}`,
                }))}
              />
            </Form.Item>
          ) : (
            <>
              <Alert type="info" showIcon style={{ marginBottom: 12 }}
                message={t("系统标识请用英文小写（如 repair）。同一公司+系统+环境只能登记一次；若已登记请改选「选用已有连接」。")} />
              <Space style={{ display: "flex" }} align="start" wrap>
                <Form.Item name="company_id" label={t("公司")} rules={[{ required: true }]} style={{ width: 200 }}>
                  <Select options={companies.map((c) => ({ value: c.id, label: c.code }))} />
                </Form.Item>
                <Form.Item name="system_key" label={t("系统")}
                  rules={[{ required: true, pattern: /^[a-z][a-z0-9_]{1,63}$/, message: t("小写字母开头，仅含 a-z / 0-9 / _") }]}>
                  <Input placeholder="repair" />
                </Form.Item>
                <Form.Item name="env" label={t("环境")}>
                  <Select style={{ width: 120 }} options={["sandbox", "test", "staging", "prod"].map((e) => ({ value: e, label: e }))} />
                </Form.Item>
              </Space>
              <Form.Item name="base_url" label={t("地址")}
                extra={t("只填到主机和端口，不要带 /Index/login 等页面路径")}
                rules={[{
                  required: true,
                  validator: async (_, v: string) => {
                    if (!v || !/^https?:\/\//.test(v)) throw new Error(t("以 http:// 或 https:// 开头"));
                    try {
                      const u = new URL(v);
                      if (u.pathname && u.pathname !== "/") throw new Error(t("地址不要带路径（如登录页）"));
                    } catch (e) {
                      if (e instanceof Error && e.message) throw e;
                      throw new Error(t("地址不合法"));
                    }
                  },
                }]}>
                <Input placeholder="http://10.10.130.199:8082" />
              </Form.Item>
              <Form.Item name="scheme" label={t("协议")}>
                <Select style={{ width: 140 }} options={[{ value: "http", label: "http" }, { value: "https", label: "https" }]} />
              </Form.Item>
              {exception && (
                <>
                  <Alert type="warning" showIcon style={{ marginBottom: 12 }}
                    message={t("传输例外：仅限内网地址、需数据 Owner 审批（生产需两人）、最长 90 天，只能使用服务账号身份。")} />
                  <Form.Item name="transport_exception_reason" label={t("例外理由")} rules={[{ required: true }]}>
                    <Input placeholder={t("内网老系统明文 HTTP")} />
                  </Form.Item>
                  <Form.Item name="transport_exception_expires_at" label={t("到期日")} rules={[{ required: true }]}
                    initialValue={dayjs().add(60, "day")}>
                    <DatePicker disabledDate={(d) => d.isBefore(dayjs()) || d.isAfter(dayjs().add(90, "day"))} />
                  </Form.Item>
                </>
              )}
            </>
          )}
          <Button type="primary" onClick={() => void createConnection()}>{t("下一步：鉴权")}</Button>
        </Form>
      )}

      {step === 1 && (
        <Space direction="vertical" style={{ width: "100%" }} size="middle">
          <Typography.Text type="secondary">{t("连接")}：<Typography.Text code>{connectionId}</Typography.Text></Typography.Text>
          <Radio.Group value={authMode} onChange={(e) => setAuthMode(e.target.value as AuthMode)}
            options={[
              { value: "none", label: t("无需登录") },
              { value: "cookie", label: t("Cookie 会话（form 登录）") },
            ]} />
          {authMode === "cookie" && (
            <Form form={authForm} layout="vertical" initialValues={{
              login_path: "/Index/login/login_do.html",
              username_field: "user_name",
              password_field: "password",
              cookie_name: "PHPSESSID",
              success_when: "$.code == 1",
            }}>
              <Space wrap style={{ display: "flex" }} align="start">
                <Form.Item name="login_path" label={t("登录路径")} rules={[{ required: true }]} style={{ minWidth: 280 }}>
                  <Input />
                </Form.Item>
                <Form.Item name="cookie_name" label={t("Cookie 名")} rules={[{ required: true }]}>
                  <Input style={{ width: 140 }} />
                </Form.Item>
                <Form.Item name="success_when" label={t("成功条件")}>
                  <Input style={{ width: 180 }} />
                </Form.Item>
              </Space>
              <Space wrap style={{ display: "flex" }} align="start">
                <Form.Item name="username_field" label={t("用户名字段")} rules={[{ required: true }]}>
                  <Input style={{ width: 140 }} />
                </Form.Item>
                <Form.Item name="password_field" label={t("密码字段")} rules={[{ required: true }]}>
                  <Input style={{ width: 140 }} />
                </Form.Item>
              </Space>
              <Space wrap style={{ display: "flex" }} align="start">
                <Form.Item name="username" label={t("用户名")} rules={[{ required: true }]}>
                  <Input autoComplete="off" style={{ width: 220 }} />
                </Form.Item>
                <Form.Item name="password" label={t("密码")} rules={[{ required: true }]}>
                  <Input.Password autoComplete="new-password" style={{ width: 220 }} />
                </Form.Item>
              </Space>
              <Alert type="info" showIcon message={t("账号密码只写入 OpenBao，不会出现在日志或接口响应中。")} />
            </Form>
          )}
          {authOk && <Alert type="success" showIcon message={authOk} />}
          <Space>
            <Button onClick={() => setStep(0)}>{t("上一步")}</Button>
            <Button type="primary" onClick={() => void saveAuth()}>
              {authMode === "none" ? t("下一步：接口") : t("保存凭据并测试登录")}
            </Button>
          </Space>
        </Space>
      )}

      {step === 2 && (
        <Space direction="vertical" style={{ width: "100%" }} size="middle">
          <Form form={opForm} layout="inline" initialValues={{ method: "GET", data: "$.data" }}>
            <Form.Item name="operation" rules={[{ required: true, pattern: /^[a-z][a-z0-9_]{1,63}$/ }]} label={t("操作名")}>
              <Input placeholder="data_report" />
            </Form.Item>
            <Form.Item name="method" label={t("方法")}>
              <Select style={{ width: 100 }} options={["GET", "POST", "PUT", "PATCH", "DELETE"].map((m) => ({ value: m, label: m }))} />
            </Form.Item>
            <Form.Item name="path" rules={[{ required: true }]} label={t("路径")}>
              <Input placeholder="/api/..." style={{ width: 260 }} />
            </Form.Item>
            <Form.Item name="success_when" label={t("成功条件")}>
              <Input placeholder='$.msg == "success"' style={{ width: 180 }} />
            </Form.Item>
            <Form.Item name="data" label={t("数据路径")}>
              <Input style={{ width: 120 }} />
            </Form.Item>
            <Form.Item name="title" label={t("标题")}>
              <Input />
            </Form.Item>
            <Button onClick={() => void addOp()}>{t("添加接口")}</Button>
          </Form>
          <Table
            rowKey="key"
            size="small"
            pagination={false}
            dataSource={ops}
            columns={[
              {
                title: t("选用"),
                dataIndex: "selected",
                render: (v: boolean, r) => (
                  <input type="checkbox" checked={v} onChange={(e) => {
                    setOps((prev) => prev.map((o) => (o.key === r.key ? { ...o, selected: e.target.checked } : o)));
                  }} />
                ),
              },
              { title: t("操作名"), dataIndex: "operation", render: (v) => <Typography.Text code>{v}</Typography.Text> },
              { title: t("方法"), dataIndex: "method", render: (v) => <Tag>{v}</Tag> },
              { title: t("路径"), dataIndex: "path", render: (v) => <Typography.Text code>{v}</Typography.Text> },
              {
                title: t("操作"),
                key: "ops",
                render: (_, r) => (
                  <Button size="small" onClick={() => void tryOp(r)} disabled={!connectionId}>{t("点测")}</Button>
                ),
              },
            ]}
            expandable={{
              expandedRowRender: (r) => (
                r.sample !== undefined
                  ? <JsonView value={r.sample} />
                  : <Typography.Text type="secondary">{r.tryError || t("尚未点测")}</Typography.Text>
              ),
            }}
          />
          <Space>
            <Button onClick={() => setStep(1)}>{t("上一步")}</Button>
            <Button type="primary" onClick={() => void persist()} disabled={!ops.some((o) => o.selected)}>
              {t("生成草稿")}
            </Button>
          </Space>
        </Space>
      )}

      {step === 3 && result && (
        <Space direction="vertical" style={{ width: "100%" }} size="middle">
          <Alert type="success" showIcon
            message={t("已落库：连接器 {0}，{1} 个 Tool 草稿", [
              result.connector_instance_id ?? "-",
              String(result.drafts?.length ?? 0),
            ])} />
          {(result.warnings ?? []).map((w) => <Alert key={w} type="warning" message={w} />)}
          <Table
            rowKey="capability_id"
            size="small"
            pagination={false}
            dataSource={result.drafts ?? []}
            columns={[
              { title: t("能力 ID"), dataIndex: "capability_id", render: (v) => <Typography.Text code>{v}</Typography.Text> },
              { title: t("状态"), dataIndex: "state" },
            ]}
          />
          <Typography.Paragraph>
            {t("下一步到「资产与门禁」补测试与审批，通过后授权给消费方，即可经 MCP 调用。")}
            {" "}
            <Link to="/assets">{t("去资产与门禁")}</Link>
          </Typography.Paragraph>
          <Button onClick={() => { setStep(0); setResult(null); setConnectionId(null); }}>{t("再接一个系统")}</Button>
        </Space>
      )}
    </PageCard>
  );
}
