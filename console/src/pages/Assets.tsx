import { Alert, Button, Card, Drawer, Form, Input, InputNumber, Modal, Select, Space, Steps, Table, Tabs, Tag, Tooltip, Typography } from "antd";
import { useEffect, useState } from "react";
import { get, post, put, type Page } from "../api";
import { useAuth } from "../auth";
import { Clearance, GateEvidence, JsonInput, jsonRule, JsonView, matches, OrgName, PageCard, PAGINATION, parseJson, StateTag, Toolbar, useOrgs } from "../components";
import { useAction, useLoad } from "../hooks";
import type { Asset, GateRecord, ImpactItem, Release, TestCase, VersionDetail, VersionSummary } from "../types";
import { t } from "../i18n";
import { SkillPackagePanel } from "./SkillEditor";

const LIFECYCLE = ["draft", "testing", "in_review", "approved", "canary", "active"];

function AssetDescCell({ asset }: { asset: Asset }) {
  const title = (asset.title || "").trim();
  const desc = (asset.description || "").trim();
  if (!title && !desc) return <Typography.Text type="secondary">—</Typography.Text>;
  return (
    <div style={{ maxWidth: 320, minWidth: 160 }} onClick={(e) => e.stopPropagation()}>
      {title ? (
        <Typography.Text ellipsis style={{ maxWidth: 320, display: "block" }}>
          {title}
        </Typography.Text>
      ) : null}
      {desc ? (
        <Tooltip title={desc}>
          <Typography.Text type="secondary" ellipsis style={{ maxWidth: 320, display: "block", fontSize: 12 }}>
            {desc}
          </Typography.Text>
        </Tooltip>
      ) : null}
    </div>
  );
}

function AssetSystemCell({ asset }: { asset: Asset }) {
  if (!asset.system_key && !asset.system_host) {
    return <Typography.Text type="secondary">—</Typography.Text>;
  }
  return (
    <div>
      <div>{asset.system_key || "—"}</div>
      {asset.system_host ? (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {asset.system_host}
        </Typography.Text>
      ) : null}
    </div>
  );
}

export function AssetsPage() {
  const { has } = useAuth();
  const assets = useLoad(() => get<Page<Asset>>("/assets"), []);
  const [open, setOpen] = useState(false);
  const [selected, setSelected] = useState<Asset | null>(null);
  const [kw, setKw] = useState("");
  return (
    <PageCard>
      <Toolbar placeholder={t("资产名称 / 类型 / 描述 / 系统")} onSearch={setKw}
        actions={has("platform_admin", "asset_developer") && <Button onClick={() => setOpen(true)}>{t("新建资产")}</Button>} />
      {assets.error && <Alert type="error" message={assets.error} />}
      <Table<Asset>
        pagination={PAGINATION}
        rowKey="id"
        loading={assets.loading}
        dataSource={(assets.data?.items ?? []).filter((a) =>
          matches(a, kw, ["name", "type", "title", "description", "connector", "system_key", "system_host"]),
        )}
        onRow={(r) => ({ onClick: () => setSelected(r), style: { cursor: "pointer" } })}
        columns={[
          {
            title: t("名称"),
            dataIndex: "name",
            width: 220,
            ellipsis: true,
            render: (v: string) => <Typography.Link ellipsis style={{ maxWidth: 200 }}>{v}</Typography.Link>,
          },
          { title: t("类型"), dataIndex: "type", width: 100, render: (v) => <Tag>{v}</Tag> },
          { title: t("说明"), key: "desc", width: 280, ellipsis: true, render: (_, r) => <AssetDescCell asset={r} /> },
          {
            title: t("连接器"),
            dataIndex: "connector",
            width: 130,
            ellipsis: true,
            render: (v: string | null | undefined) => v || "—",
          },
          { title: t("系统"), key: "system", width: 150, render: (_, r) => <AssetSystemCell asset={r} /> },
          { title: t("归属"), dataIndex: "owner_org_id", width: 140, render: (v) => <OrgName id={v} /> },
          { title: t("密级"), dataIndex: "classification", width: 70, render: (v) => <Clearance level={v} /> },
          {
            title: t("最新状态"),
            dataIndex: "latest_state",
            width: 110,
            render: (v: string | null | undefined, r) =>
              v ? (
                <span>
                  <StateTag state={v} />
                  {r.latest_semver ? (
                    <Typography.Text type="secondary" style={{ marginLeft: 6, fontSize: 12 }}>
                      @{r.latest_semver}
                    </Typography.Text>
                  ) : null}
                </span>
              ) : (
                "—"
              ),
          },
        ]}
      />
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
          {versionId && (
            <VersionPanel key={versionId} versionId={versionId} assetId={asset.id} assetType={asset.type} onChanged={versions.reload} />
          )}
          <NewVersion asset={asset} open={newOpen} onClose={() => setNewOpen(false)} onDone={versions.reload} />
        </>
      )}
    </Drawer>
  );
}

/** 新建版本的 Spec 起点与校验所用的 Schema 文件（docs/specs/），按资产类型切换。 */
export const SPEC_TEMPLATES: Record<string, { schema: string; spec: unknown }> = {
  tool: {
    schema: "tool-spec",
    spec: {
      capability_id: "production.mes.query_work_orders", domain: "production", name: "query_work_orders",
      description: t("查询指定工厂的生产工单，按状态过滤，返回工单号、计划数量和状态。"),
      input_schema: { type: "object", properties: { factory_id: { type: "string" } }, required: ["factory_id"] },
      output_schema: { type: "array", items: { type: "object" } },
      connector_ref: { connector: "mes_vn01", operation: "query_work_orders" },
      risk_level: "read", timeout: { connect_ms: 2000, read_ms: 5000, deadline_ms: 8000 }, data_classification: 2,
      identity_mode: "service_account", confirm_policy: "auto",
      scope_binding: { factory_id: { dimension: "company", map: "by_code", system_key: "mes" } },
    },
  },
  skill: {
    schema: "skill-spec",
    spec: {
      name: "query-work-orders",
      description: t("指导模型查询本厂在制工单：先确认工厂，再按状态汇总。"),
      tools: [],
    },
  },
  workflow: {
    schema: "workflow-dsl",
    spec: {
      name: "po_review", capability_id: "agent.finance.po_review", version: "1.0.0",
      description: t("采购单审核：读取采购单，高风险时转人工审批。"),
      input_schema: { type: "object", properties: { po: { type: "string" } }, required: ["po"] },
      budget: { max_steps: 30, max_llm_calls: 3, timeout_s: 600 },
      nodes: [
        { id: "n1", type: "tool", ref: "finance.erp.get_po@1.0.0", args: { po: "$input.po" }, next: "n2" },
        { id: "n2", type: "human_task", kind: "approve", assignee: "role:business_owner@VN01", summary: t("采购单需要业务负责人审批"), next: "n3" },
        { id: "n3", type: "end", output: { po: "$n1.po_no" } },
      ],
    },
  },
  bundle: {
    schema: "bundle-spec",
    spec: { name: "production_basics", description: t("生产常用查询套件：工单、物料与产线状态。"), tools: [] },
  },
  model_profile: { schema: "model-profile", spec: {} },
};

export const specTemplate = (type: string) => JSON.stringify(SPEC_TEMPLATES[type]?.spec ?? {}, null, 2);

function NewVersion(p: { asset: Asset; open: boolean; onClose: () => void; onDone: () => void }) {
  const [form] = Form.useForm();
  const act = useAction();
  const schema = SPEC_TEMPLATES[p.asset.type]?.schema;
  return (
    <Modal open={p.open} title={t("新建版本")} width={760} onCancel={p.onClose} onOk={() => form.submit()} destroyOnClose>
      {(p.asset.type === "tool" || p.asset.type === "workflow") && (
        <Alert type="info" showIcon style={{ marginBottom: 12 }}
          message={t("发布（灰度 / 全量）时出口 exposure 包含 mcp，即自动经数据面 /mcp 暴露为 MCP 工具，无需额外配置。")} />
      )}
      {p.asset.type === "skill" && (
        <Alert type="info" showIcon style={{ marginBottom: 12 }}
          message={t("Skill 版本创建后，在版本面板的「Skill 包」页签在线编写 SKILL.md（或上传 zip）；Skill 经目录与 corp-skill 分发，不经 /mcp。")} />
      )}
      <Form form={form} layout="vertical" preserve={false} initialValues={{ semver: "1.0.0", spec: specTemplate(p.asset.type) }}
        onFinish={async (v) => {
          const r = await act(() => post(`/assets/${p.asset.id}/versions`, { semver: v.semver, spec: parseJson(v.spec) }), t("已创建"));
          if (r) { p.onClose(); p.onDone(); }
        }}>
        <Form.Item name="semver" label={t("版本号")} rules={[{ required: true, pattern: /^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$/, message: t("语义化版本，如 1.2.0") }]}><Input /></Form.Item>
        <Form.Item name="spec" rules={[{ required: true }, jsonRule]}
          label={schema ? t("Spec（JSON，按 docs/specs/{0}.schema.json 校验）", [schema]) : t("Spec（JSON）")}>
          <JsonInput rows={18} />
        </Form.Item>
      </Form>
    </Modal>
  );
}

function VersionPanel({
  versionId,
  assetId,
  assetType,
  onChanged,
}: {
  versionId: string;
  assetId: string;
  assetType: string;
  onChanged: () => void;
}) {
  const detail = useLoad(() => get<VersionDetail>(`/versions/${versionId}`), [versionId]);
  const gates = useLoad(() => get<{ items: GateRecord[] }>(`/versions/${versionId}/gates`), [versionId]);
  const releases = useLoad(() => get<{ items: Release[] }>(`/assets/${assetId}/releases`), [assetId]);
  const { has } = useAuth();
  const act = useAction();
  const [labResult, setLabResult] = useState<{ passed: boolean; cases: { name: string; passed: boolean; detail: string; status: string }[] } | null>(null);
  const refresh = () => { detail.reload(); gates.reload(); onChanged(); releases.reload(); };
  const v = detail.data;
  if (!v) return detail.error ? <Alert type="error" message={detail.error} /> : null;
  const dev = has("platform_admin", "asset_developer");
  const step = Math.max(LIFECYCLE.indexOf(v.state), 0);
  // find active release for this version that has a prev to roll back to
  const activeRelease = (releases.data?.items ?? []).find((r) => r.version_id === versionId && r.status === "active" && r.prev_release_id);
  const tabs = [
    { key: "gates", label: t("门禁记录"), children: (
      <Table<GateRecord> rowKey={(r) => `${r.gate}-${r.created_at}`} size="small" pagination={false} dataSource={gates.data?.items ?? []}
        expandable={{ expandedRowRender: (r) => <GateEvidence value={r.evidence} /> }}
        columns={[
          { title: t("门"), dataIndex: "gate" },
          { title: t("结果"), dataIndex: "status", render: (s) => <StateTag state={s} /> },
          { title: t("时间"), dataIndex: "created_at", render: (v: string) => v.slice(0, 19).replace("T", " ") },
        ]} />) },
    { key: "cases", label: t("门禁用例（{0}）", [v.test_cases.length]), children: <Cases v={v} editable={dev && v.state === "draft"} onDone={refresh} /> },
    { key: "acceptance", label: t("验收单"), children: <Acceptance v={v} editable={v.state === "draft"} onDone={refresh} /> },
    ...(assetType === "skill"
      ? [{
          key: "skill",
          label: t("Skill 包"),
          children: (
            <SkillPackagePanel
              versionId={versionId}
              assetId={v.asset_id}
              semver={v.semver}
              spec={v.spec}
              editable={dev && v.state === "draft"}
              onSaved={refresh}
            />
          ),
        }]
      : []),
    ...(assetType === "bundle" || assetType === "skill"
      ? [{
          key: "selection-eval",
          label: assetType === "skill" ? t("指导效果评测") : t("选择评测"),
          children: <SelectionEvalEditor versionId={versionId} editable={dev && v.state === "draft"} />,
        }]
      : []),
    { key: "impact", label: t("影响"), children: <ImpactTab versionId={versionId} /> },
    { key: "spec", label: "Spec", children: <JsonView value={v.spec} /> },
  ];
  return (
    <Card size="small" style={{ marginTop: 16 }} title={t("版本 {0}", [v.semver])} extra={<StateTag state={v.state} />}>
      <Steps size="small" current={step} status={v.state === "suspended" ? "error" : undefined}
        items={LIFECYCLE.map((s) => ({ title: s }))} style={{ marginBottom: 16 }} />
      <Space style={{ marginBottom: 12 }} wrap>
        {dev && v.state === "draft" && (
          <Button type="primary" onClick={async () => {
            const r = await act(() => post<{ passed: boolean; problems: string[]; pii_suggestions?: { field: string; kind: string }[] }>(
              `/versions/${versionId}/submit`));
            if (r) {
              const pii = r.pii_suggestions?.length
                ? (
                  <>
                    <div style={{ marginTop: 8 }}>{t("PII 建议（请确认后写入 Spec.pii_fields）")}</div>
                    {r.pii_suggestions.map((s) => (
                      <div key={`${s.field}-${s.kind}`}>· {s.field}（{s.kind}）</div>
                    ))}
                  </>
                )
                : null;
              Modal[r.passed ? "success" : "error"]({
                title: r.passed ? t("G0 通过，进入 testing") : t("G0 未通过"),
                content: (
                  <>
                    {r.problems.map((x) => <div key={x}>· {x}</div>)}
                    {pii}
                  </>
                ),
              });
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
        {has("platform_admin") && activeRelease && (
          <Button danger onClick={() => Modal.confirm({
            title: t("一键回滚？"),
            content: t("将回滚到上一个发布版本，当前灰度或全量版本将下线。"),
            onOk: async () => { if (await act(() => post(`/releases/${activeRelease.id}/rollback`), t("回滚已发起"))) refresh(); },
          })}>{t("一键回滚")}</Button>
        )}
      </Space>
      {labResult && (
        <Alert style={{ marginBottom: 12 }} type={labResult.passed ? "success" : "error"} showIcon
          message={labResult.passed ? t("G1 / G2 / G3 通过，已发起 G5 业务验收与 G6 安全审核") : t("G1 / G2 / G3 未通过，已退回 draft")}
          description={labResult.cases.map((c) => (
            <div key={c.name}>{c.passed ? "✔" : "✘"} {c.name}（{c.status}）{c.detail && `：${c.detail}`}</div>))} />
      )}
      <Tabs items={tabs} defaultActiveKey={assetType === "skill" ? "skill" : undefined} />
    </Card>
  );
}

interface SelectionEvalCase {
  name: string;
  question: string;
  expect_capability_id: string | null;
}

interface SelectionEval {
  version_id: string;
  model_profiles: string[];
  cases: SelectionEvalCase[];
}

function SelectionEvalEditor({ versionId, editable }: { versionId: string; editable: boolean }) {
  const data = useLoad(() => get<SelectionEval>(`/versions/${versionId}/selection-eval`), [versionId]);
  const act = useAction();
  const [form] = Form.useForm();
  useEffect(() => {
    if (data.data) {
      form.setFieldsValue({
        model_profiles: (data.data.model_profiles ?? []).join("\n"),
        cases_json: JSON.stringify(data.data.cases ?? [], null, 2),
      });
    }
  }, [data.data, form]);
  if (data.error) return <Alert type="error" message={data.error} />;
  if (!editable) {
    return data.data ? <JsonView value={data.data} /> : <Alert type="info" message={t("尚未配置选择评测")} />;
  }
  return (
    <Form
      form={form}
      layout="vertical"
      onFinish={async (v) => {
        let cases: SelectionEvalCase[];
        try {
          cases = parseJson(v.cases_json) as SelectionEvalCase[];
          if (!Array.isArray(cases)) throw new Error("not array");
        } catch {
          Modal.error({ title: t("用例 JSON 无效") });
          return;
        }
        const model_profiles = String(v.model_profiles ?? "")
          .split("\n")
          .map((s: string) => s.trim())
          .filter(Boolean);
        if (await act(
          () => put(`/versions/${versionId}/selection-eval`, { model_profiles, cases }),
          t("已保存选择评测"),
        )) {
          data.reload();
        }
      }}
    >
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message={t("套件 G2 要求至少 20 条用例、1–2 个 model_profile（name@semver）")}
      />
      <Form.Item name="model_profiles" label={t("评测模型（每行一个 name@semver）")} rules={[{ required: true }]}>
        <Input.TextArea rows={2} placeholder="gpt-4o-mini@1.0.0" />
      </Form.Item>
      <Form.Item
        name="cases_json"
        label={t("评测用例（JSON 数组：name / question / expect_capability_id）")}
        rules={[{ required: true }, jsonRule]}
      >
        <JsonInput rows={12} />
      </Form.Item>
      <Button type="primary" htmlType="submit">{t("保存选择评测")}</Button>
    </Form>
  );
}

// ── Impact Tab ────────────────────────────────────────────────────────────────

function ImpactTab({ versionId }: { versionId: string }) {
  const impact = useLoad(() => get<{ items: ImpactItem[] }>(`/versions/${versionId}/impact`), [versionId]);
  return (
    <>
      {impact.error && <Alert type="error" message={impact.error} style={{ marginBottom: 8 }} />}
      <Table<ImpactItem> rowKey="name" size="small" loading={impact.loading} pagination={false}
        locale={{ emptyText: t("无影响项") }}
        dataSource={impact.data?.items ?? []}
        columns={[
          { title: t("名称"), dataIndex: "name", render: (v: string) => <Typography.Text code>{v}</Typography.Text> },
          { title: t("类型"), dataIndex: "asset_type", render: (v: string) => <Tag>{v}</Tag> },
          { title: t("状态"), dataIndex: "state", render: (v: string) => <StateTag state={v} /> },
          { title: t("引用"), dataIndex: "refs", render: (v: string[]) => v?.join(", ") || "-" },
        ]} />
    </>
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
            const r = await act(() => post<{ passed?: boolean; calls?: number; error_rate?: number; release_id?: string }>(
              `/versions/${versionId}/${kind}`, { env: v.env, scope, exposure: v.exposure, min_calls: v.min_calls }));
            if (r) {
              if (kind === "promote" && !r.passed) Modal.warning({ title: t("灰度指标未达标"), content: t("调用 {0} 次，错误率 {1}", [r.calls, r.error_rate]) });
              if (kind === "canary" && r.release_id) Modal.success({ title: t("灰度发布已创建"), content: t("Release ID：{0}", [r.release_id]) });
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
          <Form.Item name="exposure" label={t("出口")} extra={t("包含 mcp 即自动经数据面 /mcp 暴露")}>
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
