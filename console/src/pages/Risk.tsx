import { Alert, Button, Form, Input, InputNumber, Modal, Segmented, Space, Switch, Tabs, Tag, Typography } from "antd";
import { useState } from "react";
import { get, post, put, query, type Page } from "../api";
import { useAuth } from "../auth";
import { JsonView, OrgName, PageCard, PAGINATION, StateTag } from "../components";
import { ListTable } from "../ListTable";
import { useAction, useLoad } from "../hooks";
import { t } from "../i18n";

interface RiskRule {
  code: string;
  name: string;
  description: string;
  source: string;
  params: { window_s: number; threshold: number; min_count?: number; [k: string]: unknown };
  action: "alert" | "suspend_asset";
  severity: "low" | "medium" | "high";
  enabled: boolean;
}

interface RiskEvent {
  id: string;
  rule_code: string;
  rule_name: string;
  company_id: string | null;
  group_key: Record<string, string>;
  observed: number;
  threshold: number;
  severity: string;
  action: string;
  action_result: Record<string, unknown>;
  status: "open" | "acknowledged" | "resume_requested" | "resolved";
  note: string | null;
  created_at: string;
}

interface UsageRow {
  day: string;
  company_id: string;
  consumer_id: string | null;
  capability_id: string | null;
  calls: number;
  failed: number;
  writes: number;
}

const SEVERITY_COLOR: Record<string, string> = { low: "default", medium: "orange", high: "red" };
const SEVERITY_LABEL: Record<string, string> = { low: t("低"), medium: t("中"), high: t("高") };
const time = (v: string) => v.slice(0, 19).replace("T", " ");

export function RiskPage() {
  return (
    <PageCard>
      <Tabs
        items={[
          { key: "events", label: t("风险事件"), children: <Events /> },
          { key: "rules", label: t("风险规则"), children: <Rules /> },
          { key: "usage", label: t("计量"), children: <Usage /> },
          { key: "cost", label: t("成本分摊"), children: <CostShare /> },
        ]}
      />
    </PageCard>
  );
}

interface CostRow {
  group_by: string;
  dim: string;
  company_id: string | null;
  calls: number;
  writes: number;
}

function CostShare() {
  const [groupBy, setGroupBy] = useState<"company" | "consumer" | "asset">("company");
  const report = useLoad(
    () =>
      get<{ from_day: string; to_day: string; group_by: string; items: CostRow[] }>(
        `/console/usage-report${query({ group_by: groupBy })}`,
      ),
    [groupBy],
  );
  const dimTitle =
    groupBy === "company" ? t("公司") : groupBy === "consumer" ? t("消费方") : t("资产");
  return (
    <>
      <Typography.Paragraph type="secondary">
        {t("按 usage_counter 聚合调用量（公司 / 消费方 / 资产）。默认近 7 个 UTC 日。")}
      </Typography.Paragraph>
      <Space style={{ marginBottom: 12 }}>
        <Typography.Text>{t("分组")}</Typography.Text>
        <Segmented
          value={groupBy}
          onChange={(v) => setGroupBy(v as "company" | "consumer" | "asset")}
          options={[
            { value: "company", label: t("公司") },
            { value: "consumer", label: t("消费方") },
            { value: "asset", label: t("资产") },
          ]}
        />
      </Space>
      {report.error && <Alert type="error" message={report.error} />}
      <ListTable<CostRow>
        pagination={PAGINATION}
        rowKey={(r) => `${r.company_id}-${r.dim}`}
        loading={report.loading}
        dataSource={report.data?.items ?? []}
        locale={{ emptyText: t("这段时间没有调用") }}
        columns={[
          { title: dimTitle, dataIndex: "dim", render: (v: string) => (
            groupBy === "company" ? <OrgName id={v} /> : <Typography.Text code>{v.slice(0, 8)}</Typography.Text>
          ) },
          ...(groupBy !== "company"
            ? [{ title: t("公司"), dataIndex: "company_id" as const, render: (v: string | null) => (v ? <OrgName id={v} /> : "-") }]
            : []),
          { title: t("调用"), dataIndex: "calls", sorter: (a: CostRow, b: CostRow) => a.calls - b.calls },
          { title: t("写操作"), dataIndex: "writes" },
        ]}
      />
    </>
  );
}

function Events() {
  const [status, setStatus] = useState<string>("open");
  const events = useLoad(() => get<Page<RiskEvent>>(`/risk-events${query({ status: status === "all" ? undefined : status })}`), [status]);
  const { has } = useAuth();
  const act = useAction();
  const [current, setCurrent] = useState<{ ev: RiskEvent; kind: "ack" | "resume" } | null>(null);
  const canHandle = has("platform_admin", "data_owner");
  return (
    <>
      <Space style={{ marginBottom: 12 }} wrap>
        <Segmented
          value={status}
          onChange={(v) => setStatus(String(v))}
          options={[
            { value: "open", label: t("待处理") },
            { value: "resume_requested", label: t("恢复复核中") },
            { value: "all", label: t("全部") },
          ]}
        />
        {has("platform_admin") && (
          <Button onClick={async () => {
            const r = await act(() => post<{ created: unknown[] }>("/risk-scan"));
            if (r) {
              Modal.info({ title: t("扫描完成，新事件 {0} 条", [r.created.length]) });
              events.reload();
            }
          }}>{t("立即扫描")}</Button>
        )}
      </Space>
      <Alert type="info" showIcon style={{ marginBottom: 12 }}
        message={t("自动熔断的能力在恢复前一直拒绝调用；恢复需写明修复情况，并由另一位数据 Owner 在审批待办中复核。")} />
      {events.error && <Alert type="error" message={events.error} />}
      <ListTable<RiskEvent> pagination={PAGINATION} rowKey="id" loading={events.loading} dataSource={events.data?.items ?? []}
        locale={{ emptyText: t("没有风险事件") }}
        expandable={{ expandedRowRender: (e) => <JsonView value={{ group_key: e.group_key, action_result: e.action_result, note: e.note }} /> }}
        columns={[
          { title: t("时间"), dataIndex: "created_at", render: time },
          { title: t("规则"), dataIndex: "rule_name", render: (v: string) => t(v) },
          { title: t("级别"), dataIndex: "severity", render: (v: string) => <Tag color={SEVERITY_COLOR[v]}>{SEVERITY_LABEL[v] ?? v}</Tag> },
          { title: t("对象"), dataIndex: "group_key", render: (k: Record<string, string>) => (
            <Space size={4} wrap>{Object.entries(k).map(([f, v]) => <Typography.Text key={f} code>{f}={String(v).slice(0, 36)}</Typography.Text>)}</Space>
          ) },
          { title: t("观测值 / 阈值"), key: "obs", render: (_, e) => `${e.observed} / ${e.threshold}` },
          { title: t("公司"), dataIndex: "company_id", render: (v: string | null) => (v ? <OrgName id={v} /> : "-") },
          { title: t("动作"), dataIndex: "action", render: (v: string) => (v === "suspend_asset" ? <Tag color="red">{t("自动熔断")}</Tag> : <Tag>{t("告警")}</Tag>) },
          { title: t("状态"), dataIndex: "status", render: (v: string) => <StateTag state={v} /> },
          { title: t("操作"), key: "ops", render: (_, e) => canHandle && (e.status === "open" || e.status === "acknowledged") && (
            <Space>
              {e.status === "open" && <Button size="small" onClick={() => setCurrent({ ev: e, kind: "ack" })}>{e.action === "alert" ? t("关闭") : t("确认")}</Button>}
              {e.action === "suspend_asset" && <Button size="small" type="primary" onClick={() => setCurrent({ ev: e, kind: "resume" })}>{t("申请恢复")}</Button>}
            </Space>
          ) },
        ]} />
      <NoteModal
        open={Boolean(current)}
        title={current?.kind === "resume" ? t("申请恢复（需另一位数据 Owner 复核）") : t("确认风险事件")}
        label={current?.kind === "resume" ? t("修复情况") : t("处理说明")}
        onClose={() => setCurrent(null)}
        onSubmit={async (note) => {
          if (!current) return;
          const path = `/risk-events/${current.ev.id}/${current.kind === "resume" ? "request-resume" : "acknowledge"}`;
          if (await act(() => post(path, { note }), t("已提交"))) {
            setCurrent(null);
            events.reload();
          }
        }}
      />
    </>
  );
}

function NoteModal(p: { open: boolean; title: string; label: string; onClose: () => void; onSubmit: (note: string) => void }) {
  const [form] = Form.useForm();
  return (
    <Modal open={p.open} title={p.title} onCancel={p.onClose} onOk={() => form.submit()} destroyOnClose>
      <Form form={form} layout="vertical" preserve={false} onFinish={(v) => p.onSubmit(v.note)}>
        <Form.Item name="note" label={p.label} rules={[{ required: true, min: 4, message: t("至少 4 个字") }]}>
          <Input.TextArea rows={3} />
        </Form.Item>
      </Form>
    </Modal>
  );
}

function Rules() {
  const rules = useLoad(() => get<{ items: RiskRule[] }>("/risk-rules"), []);
  const { has } = useAuth();
  const act = useAction();
  const admin = has("platform_admin");
  const save = async (code: string, patch: Record<string, unknown>) => {
    if (await act(() => put(`/risk-rules/${code}`, patch), t("已保存"))) rules.reload();
  };
  return (
    <>
      {rules.error && <Alert type="error" message={rules.error} />}
      <ListTable<RiskRule> rowKey="code" size="small" pagination={false} loading={rules.loading} dataSource={rules.data?.items ?? []}
        expandable={{ expandedRowRender: (r) => <JsonView value={r.params} /> }}
        columns={[
          { title: t("规则"), key: "name", render: (_, r) => (
            <Space direction="vertical" size={0}>
              <Typography.Text strong>{t(r.name)}</Typography.Text>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>{t(r.description)}</Typography.Text>
            </Space>
          ) },
          { title: t("窗口（秒）"), key: "window", render: (_, r) => (
            admin ? <InputNumber size="small" min={10} defaultValue={r.params.window_s} onBlur={(e) => {
              const v = Number(e.target.value);
              if (v && v !== r.params.window_s) void save(r.code, { params: { window_s: v } });
            }} /> : r.params.window_s
          ) },
          { title: t("阈值"), key: "threshold", render: (_, r) => (
            admin ? <InputNumber size="small" min={0} step={r.params.threshold < 1 ? 0.05 : 1} defaultValue={r.params.threshold}
              onBlur={(e) => {
                const v = Number(e.target.value);
                if (v && v !== r.params.threshold) void save(r.code, { params: { threshold: v } });
              }} /> : r.params.threshold
          ) },
          { title: t("动作"), dataIndex: "action", render: (v: string) => (v === "suspend_asset" ? <Tag color="red">{t("自动熔断")}</Tag> : <Tag>{t("告警")}</Tag>) },
          { title: t("级别"), dataIndex: "severity", render: (v: string) => <Tag color={SEVERITY_COLOR[v]}>{SEVERITY_LABEL[v] ?? v}</Tag> },
          { title: t("启用"), dataIndex: "enabled", render: (v: boolean, r) => (
            <Switch size="small" checked={v} disabled={!admin} onChange={(on) => void save(r.code, { enabled: on })} />
          ) },
        ]} />
    </>
  );
}

function Usage() {
  const [days, setDays] = useState(7);
  const usage = useLoad(() => get<{ items: UsageRow[] }>(`/usage${query({ days })}`), [days]);
  return (
    <>
      <Space style={{ marginBottom: 12 }}>
        <Typography.Text>{t("最近天数")}</Typography.Text>
        <Segmented value={days} onChange={(v) => setDays(Number(v))} options={[1, 7, 30].map((d) => ({ value: d, label: String(d) }))} />
      </Space>
      {usage.error && <Alert type="error" message={usage.error} />}
      <ListTable<UsageRow> pagination={PAGINATION} rowKey={(r) => `${r.day}-${r.company_id}-${r.consumer_id}-${r.capability_id}`}
        loading={usage.loading} dataSource={usage.data?.items ?? []} locale={{ emptyText: t("这段时间没有调用") }}
        columns={[
          { title: t("日期"), dataIndex: "day" },
          { title: t("公司"), dataIndex: "company_id", render: (v: string) => <OrgName id={v} /> },
          { title: t("消费方"), dataIndex: "consumer_id", render: (v: string | null) => <Typography.Text code>{(v ?? "-").slice(0, 8)}</Typography.Text> },
          { title: t("能力"), dataIndex: "capability_id" },
          { title: t("调用"), dataIndex: "calls", sorter: (a, b) => a.calls - b.calls },
          { title: t("失败"), dataIndex: "failed", render: (v: number, r) => (v ? <Typography.Text type="danger">{v}（{Math.round((v / r.calls) * 100)}%）</Typography.Text> : 0) },
          { title: t("写操作"), dataIndex: "writes" },
        ]} />
    </>
  );
}
