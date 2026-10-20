import { Alert, Button, Card, Drawer, Form, Input, InputNumber, Modal, Select, Space, Steps, Table, Tabs, Tag, Typography } from "antd";
import { useState } from "react";
import { get, post, put, type Page } from "../api";
import { useAuth } from "../auth";
import { Clearance, JsonInput, jsonRule, JsonView, matches, OrgName, PageCard, PAGINATION, parseJson, StateTag, Toolbar, useOrgs } from "../components";
import { useAction, useLoad } from "../hooks";
import type { Asset, GateRecord, TestCase, VersionDetail, VersionSummary } from "../types";
import { t } from "../i18n";

const LIFECYCLE = ["draft", "testing", "in_review", "approved", "canary", "active"];

export function AssetsPage() {
  const { has } = useAuth();
  const assets = useLoad(() => get<Page<Asset>>("/assets"), []);
  const [open, setOpen] = useState(false);
  const [selected, setSelected] = useState<Asset | null>(null);
  const [kw, setKw] = useState("");
  return (
    <PageCard>
      <Toolbar placeholder={t("资产名称 / 类型")} onSearch={setKw}
        actions={has("platform_admin", "asset_developer") && <Button onClick={() => setOpen(true)}>{t("新建资产")}</Button>} />
      {assets.error && <Alert type="error" message={assets.error} />}
      <Table<Asset> pagination={PAGINATION} rowKey="id" loading={assets.loading} dataSource={(assets.data?.items ?? []).filter((a) => matches(a, kw, ["name", "type"]))}
        onRow={(r) => ({ onClick: () => setSelected(r), style: { cursor: "pointer" } })}
        columns={[
          { title: t("名称"), dataIndex: "name", render: (v: string) => <Typography.Link>{v}</Typography.Link> },
          { title: t("类型"), dataIndex: "type", render: (v) => <Tag>{v}</Tag> },
          { title: t("归属"), dataIndex: "owner_org_id", render: (v) => <OrgName id={v} /> },
          { title: t("密级"), dataIndex: "classification", render: (v) => <Clearance level={v} /> },
        ]} />
      <NewAsset open={open} onClose={() => setOpen(false)} onDone={assets.reload} />
      <AssetDrawer asset={selected} onClose={() => setSelected(null)} />
    </PageCard>
  );
}

function NewAsset(p: { open: boolean; onClose: () => void; onDone: () => void }) {
  const [form] = Form.useForm();
  const { list } = useOrgs();
  const act = useAction();
  return (
    <Modal open={p.open} title={t("新建资产")} onCancel={p.onClose} onOk={() => form.submit()} destroyOnClose>
      <Form form={form} layout="vertical" preserve={false} initialValues={{ type: "tool", classification: 2 }}
        onFinish={async (v) => { if (await act(() => post("/assets", v), t("已创建"))) { p.onClose(); p.onDone(); } }}>
        <Form.Item name="name" label={t("名称")} rules={[{ required: true, min: 3 }]}><Input placeholder="mes.work_orders" /></Form.Item>
        <Form.Item name="type" label={t("类型")}>
          <Select options={["tool", "skill", "workflow", "bundle", "prompt", "model_profile", "connector_package"].map((x) => ({ value: x, label: x }))} />
        </Form.Item>
        <Form.Item name="owner_org_id" label={t("归属组织")} rules={[{ required: true }]}>
          <Select options={list.filter((o) => o.type !== "group").map((o) => ({ value: o.id, label: `${o.code} · ${o.name}` }))} />
        </Form.Item>
        <Form.Item name="classification" label={t("数据密级")}><InputNumber min={1} max={4} /></Form.Item>
      </Form>
    </Modal>
  );
}

function AssetDrawer({ asset, onClose }: { asset: Asset | null; onClose: () => void }) {
  const versions = useLoad(() => (asset ? get<Page<VersionSummary>>(`/assets/${asset.id}/versions`) : Promise.resolve(null)), [asset?.id]);
  const [versionId, setVersionId] = useState<string | null>(null);
  const [newOpen, setNewOpen] = useState(false);
  const { has } = useAuth();
  return (
    <Drawer open={Boolean(asset)} width={980} title={asset?.name} onClose={() => { setVersionId(null); onClose(); }} destroyOnClose>
      {asset && (
        <>
          <Space style={{ marginBottom: 12 }}>
            {has("platform_admin", "asset_developer") && <Button onClick={() => setNewOpen(true)}>{t("新建版本")}</Button>}
          </Space>
          <Table<VersionSummary> rowKey="id" size="small" pagination={false} loading={versions.loading}
            dataSource={versions.data?.items ?? []}
            rowClassName={(r) => (r.id === versionId ? "ant-table-row-selected" : "")}
            onRow={(r) => ({ onClick: () => setVersionId(r.id), style: { cursor: "pointer" } })}
            columns={[
              { title: t("版本"), dataIndex: "semver" },
              { title: t("状态"), dataIndex: "state", render: (v) => <StateTag state={v} /> },
              { title: t("内容哈希"), dataIndex: "content_hash", render: (v: string) => <Typography.Text code>{v.slice(0, 19)}…</Typography.Text> },
              { title: t("创建"), dataIndex: "created_at", render: (v: string) => v.slice(0, 16).replace("T", " ") },
            ]} />
          {versionId && <VersionPanel key={versionId} versionId={versionId} onChanged={versions.reload} />}
          <NewVersion asset={asset} open={newOpen} onClose={() => setNewOpen(false)} onDone={versions.reload} />
        </>
      )}
    </Drawer>
  );
}

const SPEC_TEMPLATE = JSON.stringify({
  capability_id: "production.mes.query_work_orders", domain: "production", name: "query_work_orders",
  description: t("查询指定工厂的生产工单，按状态过滤，返回工单号、计划数量和状态。"),
  input_schema: { type: "object", properties: { factory_id: { type: "string" } }, required: ["factory_id"] },
  output_schema: { type: "array", items: { type: "object" } },
  connector_ref: { connector: "mes_vn01", operation: "query_work_orders" },
  risk_level: "read", timeout: { connect_ms: 2000, read_ms: 5000, deadline_ms: 8000 }, data_classification: 2,
  identity_mode: "service_account", confirm_policy: "auto",
  scope_binding: { factory_id: { dimension: "company", map: "by_code", system_key: "mes" } },
}, null, 2);

function NewVersion(p: { asset: Asset; open: boolean; onClose: () => void; onDone: () => void }) {
  const [form] = Form.useForm();
  const act = useAction();
  return (
    <Modal open={p.open} title={t("新建版本")} width={760} onCancel={p.onClose} onOk={() => form.submit()} destroyOnClose>
      <Form form={form} layout="vertical" preserve={false} initialValues={{ semver: "1.0.0", spec: SPEC_TEMPLATE }}
        onFinish={async (v) => {
          const r = await act(() => post(`/assets/${p.asset.id}/versions`, { semver: v.semver, spec: parseJson(v.spec) }), t("已创建"));
          if (r) { p.onClose(); p.onDone(); }
        }}>
        <Form.Item name="semver" label={t("版本号")} rules={[{ required: true, pattern: /^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$/, message: t("语义化版本，如 1.2.0") }]}><Input /></Form.Item>
        <Form.Item name="spec" label={t("Spec（JSON，按 docs/specs/tool-spec.schema.json 校验）")} rules={[{ required: true }, jsonRule]}>
          <JsonInput rows={18} />
        </Form.Item>
      </Form>
    </Modal>
  );
}

function VersionPanel({ versionId, onChanged }: { versionId: string; onChanged: () => void }) {
  const detail = useLoad(() => get<VersionDetail>(`/versions/${versionId}`), [versionId]);
  const gates = useLoad(() => get<{ items: GateRecord[] }>(`/versions/${versionId}/gates`), [versionId]);
  const { has } = useAuth();
  const act = useAction();
  const [labResult, setLabResult] = useState<{ passed: boolean; cases: { name: string; passed: boolean; detail: string; status: string }[] } | null>(null);
  const refresh = () => { detail.reload(); gates.reload(); onChanged(); };
  const v = detail.data;
  if (!v) return detail.error ? <Alert type="error" message={detail.error} /> : null;
  const dev = has("platform_admin", "asset_developer");
  const step = Math.max(LIFECYCLE.indexOf(v.state), 0);
  return (
    <Card size="small" style={{ marginTop: 16 }} title={t("版本 {0}", [v.semver])} extra={<StateTag state={v.state} />}>
      <Steps size="small" current={step} status={v.state === "suspended" ? "error" : undefined}
        items={LIFECYCLE.map((s) => ({ title: s }))} style={{ marginBottom: 16 }} />
      <Space style={{ marginBottom: 12 }} wrap>
        {dev && v.state === "draft" && (
          <Button type="primary" onClick={async () => {
            const r = await act(() => post<{ passed: boolean; problems: string[] }>(`/versions/${versionId}/submit`));
            if (r) {
              Modal[r.passed ? "success" : "error"]({ title: r.passed ? t("G0 通过，进入 testing") : t("G0 未通过"),
                content: r.problems.map((x) => <div key={x}>· {x}</div>) });
              refresh();
            }
          }}>{t("提交（G0 静态检查）")}</Button>
        )}
        {dev && v.state === "testing" && (
          <Button type="primary" onClick={async () => {
            const r = await act(() => post<typeof labResult>(`/versions/${versionId}/run-tests`));
            if (r) { setLabResult(r); refresh(); }
          }}>{t("运行门禁测试（G1 / G2 / G3）")}</Button>
        )}
        {has("platform_admin") && v.state === "approved" && <RolloutButton versionId={versionId} kind="canary" onDone={refresh} />}
        {has("platform_admin") && v.state === "canary" && <RolloutButton versionId={versionId} kind="promote" onDone={refresh} />}
      </Space>
      {labResult && (
        <Alert style={{ marginBottom: 12 }} type={labResult.passed ? "success" : "error"} showIcon
          message={labResult.passed ? t("G1 / G2 / G3 通过，已发起 G5 业务验收与 G6 安全审核") : t("G1 / G2 / G3 未通过，已退回 draft")}
          description={labResult.cases.map((c) => (
            <div key={c.name}>{c.passed ? "✔" : "✘"} {c.name}（{c.status}）{c.detail && `：${c.detail}`}</div>))} />
      )}
      <Tabs items={[
        { key: "gates", label: t("门禁记录"), children: (
          <Table<GateRecord> rowKey={(r) => `${r.gate}-${r.created_at}`} size="small" pagination={false} dataSource={gates.data?.items ?? []}
            expandable={{ expandedRowRender: (r) => <JsonView value={r.evidence} /> }}
            columns={[
              { title: t("门"), dataIndex: "gate" },
              { title: t("结果"), dataIndex: "status", render: (s) => <StateTag state={s} /> },
              { title: t("时间"), dataIndex: "created_at", render: (v: string) => v.slice(0, 19).replace("T", " ") },
            ]} />) },
        { key: "cases", label: t("门禁用例（{0}）", [v.test_cases.length]), children: <Cases v={v} editable={dev && v.state === "draft"} onDone={refresh} /> },
        { key: "acceptance", label: t("验收单"), children: <Acceptance v={v} editable={v.state === "draft"} onDone={refresh} /> },
        { key: "spec", label: "Spec", children: <JsonView value={v.spec} /> },
      ]} />
    </Card>
  );
}

function RolloutButton({ versionId, kind, onDone }: { versionId: string; kind: "canary" | "promote"; onDone: () => void }) {
  const [open, setOpen] = useState(false);
  const [form] = Form.useForm();
  const { companies } = useOrgs();
  const act = useAction();
  return (
    <>
      <Button type="primary" onClick={() => setOpen(true)}>{kind === "canary" ? t("灰度发布（G7）") : t("全量（G8）")}</Button>
      <Modal open={open} title={kind === "canary" ? t("灰度发布") : t("全量发布")} onCancel={() => setOpen(false)} onOk={() => form.submit()} destroyOnClose>
        <Form form={form} layout="vertical" preserve={false} initialValues={{ env: "test", exposure: ["rest", "mcp"], min_calls: 20 }}
          onFinish={async (v) => {
            const scope = kind === "canary" ? { company_ids: v.company_ids } : { group: true };
            const r = await act(() => post<{ passed?: boolean; calls?: number; error_rate?: number }>(
              `/versions/${versionId}/${kind}`, { env: v.env, scope, exposure: v.exposure, min_calls: v.min_calls }));
            if (r) {
              if (kind === "promote" && !r.passed) Modal.warning({ title: t("灰度指标未达标"), content: t("调用 {0} 次，错误率 {1}", [r.calls, r.error_rate]) });
              setOpen(false); onDone();
            }
          }}>
          <Form.Item name="env" label={t("环境")}><Select options={["test", "staging", "prod"].map((e) => ({ value: e, label: e }))} /></Form.Item>
          {kind === "canary" ? (
            <Form.Item name="company_ids" label={t("灰度范围（公司）")} rules={[{ required: true }]}>
              <Select mode="multiple" options={companies.map((c) => ({ value: c.id, label: c.code }))} />
            </Form.Item>
          ) : (
            <Form.Item name="min_calls" label={t("灰度期最少调用次数")}><InputNumber min={0} /></Form.Item>
          )}
          <Form.Item name="exposure" label={t("出口")}>
            <Select mode="multiple" options={["rest", "mcp", "a2a"].map((e) => ({ value: e, label: e }))} />
          </Form.Item>
        </Form>
      </Modal>
    </>
  );
}

const CASE_TEMPLATE = {
  args: JSON.stringify({ factory_id: "F001" }, null, 2),
  principal: JSON.stringify({ company: "VN01", scope: "company" }, null, 2),
  expect: JSON.stringify({ status: "succeeded" }, null, 2),
};

function Cases({ v, editable, onDone }: { v: VersionDetail; editable: boolean; onDone: () => void }) {
  const [form] = Form.useForm();
  const act = useAction();
  return (
    <>
      <Table<TestCase> rowKey="id" size="small" pagination={false} dataSource={v.test_cases}
        expandable={{ expandedRowRender: (c) => <JsonView value={{ args: c.args, principal: c.principal, expect: c.expect }} /> }}
        columns={[
          { title: t("名称"), dataIndex: "name" },
          { title: t("类型"), dataIndex: "kind", render: (k) => <Tag color={k === "permission" ? "orange" : "blue"}>{k === "permission" ? t("权限") : t("功能")}</Tag> },
          { title: t("期望"), key: "expect", render: (_, c) => JSON.stringify(c.expect) },
        ]} />
      {editable && (
        <Form form={form} layout="vertical" style={{ marginTop: 12 }} initialValues={{ kind: "functional", ...CASE_TEMPLATE }}
          onFinish={async (x) => {
            const body = { name: x.name, kind: x.kind, args: parseJson(x.args), principal: parseJson(x.principal), expect: parseJson(x.expect) };
            if (await act(() => post(`/versions/${v.id}/test-cases`, body), t("已添加"))) { form.resetFields(); onDone(); }
          }}>
          <Space align="start">
            <Form.Item name="name" label={t("用例名")} rules={[{ required: true }]}><Input /></Form.Item>
            <Form.Item name="kind" label={t("类型")}>
              <Select style={{ width: 120 }} options={[{ value: "functional", label: t("功能") }, { value: "permission", label: t("权限") }]} />
            </Form.Item>
          </Space>
          <Space align="start" style={{ display: "flex" }}>
            <Form.Item name="args" label={t("参数")} rules={[jsonRule]}><JsonInput rows={4} /></Form.Item>
            <Form.Item name="principal" label={t("调用者画像")} rules={[jsonRule]}><JsonInput rows={4} /></Form.Item>
            <Form.Item name="expect" label={t("期望")} rules={[jsonRule]}><JsonInput rows={4} /></Form.Item>
          </Space>
          <Button htmlType="submit">{t("添加用例")}</Button>
        </Form>
      )}
    </>
  );
}

function Acceptance({ v, editable, onDone }: { v: VersionDetail; editable: boolean; onDone: () => void }) {
  const [form] = Form.useForm();
  const act = useAction();
  const sheet = v.acceptance;
  if (!editable) {
    return sheet ? (
      <>
        {sheet.signed_at && <Alert type="success" showIcon message={t("已由业务 Owner 签字：{0}", [sheet.signed_at.slice(0, 19).replace("T", " ")])} style={{ marginBottom: 8 }} />}
        <JsonView value={sheet} />
      </>
    ) : <Alert type="info" message={t("没有验收单模板")} />;
  }
  return (
    <Form form={form} layout="vertical"
      initialValues={{ scenarios: (sheet?.template.scenarios as string[] | undefined)?.join("\n") ?? "", accuracy: 0.95, max_latency_ms: 3000 }}
      onFinish={async (x) => {
        const template = { scenarios: String(x.scenarios).split("\n").map((s) => s.trim()).filter(Boolean), accuracy: x.accuracy, max_latency_ms: x.max_latency_ms };
        if (await act(() => put(`/versions/${v.id}/acceptance`, template), t("已保存"))) onDone();
      }}>
      <Form.Item name="scenarios" label={t("目标场景（每行一个）")} rules={[{ required: true }]}><Input.TextArea rows={3} /></Form.Item>
      <Space>
        <Form.Item name="accuracy" label={t("准确率目标")}><InputNumber min={0} max={1} step={0.01} /></Form.Item>
        <Form.Item name="max_latency_ms" label={t("耗时上限（ms）")}><InputNumber min={0} /></Form.Item>
      </Space>
      <Button htmlType="submit">{t("保存验收单模板")}</Button>
    </Form>
  );
}
