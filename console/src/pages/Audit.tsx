import { Alert, Button, DatePicker, Form, Input, InputNumber, Table, Tag, Typography } from "antd";
import type { Dayjs } from "dayjs";
import { useState } from "react";
import { get, post, query, type Page } from "../api";
import { useAuth } from "../auth";
import { JsonView, OrgName } from "../components";
import { useAction, useLoad } from "../hooks";
import type { AuditRecord } from "../types";

interface Filters { kind?: string; trace_id?: string; range?: [Dayjs, Dayjs] | null; limit?: number }
interface VerifyResult { ok: boolean; problems: string[]; shards: number; records: number }

function toParams(f: Filters) {
  return {
    kind: f.kind || undefined,
    trace_id: f.trace_id || undefined,
    since: f.range?.[0]?.startOf("day").toISOString(),
    until: f.range?.[1]?.endOf("day").toISOString(),
    limit: f.limit ?? 100,
  };
}

export function AuditPage() {
  const { has } = useAuth();
  const [filters, setFilters] = useState<Filters>({ limit: 100 });
  const logs = useLoad(() => get<Page<AuditRecord>>(`/audit-logs${query(toParams(filters))}`), [filters]);
  const [verify, setVerify] = useState<VerifyResult | null>(null);
  const act = useAction();
  return (
    <>
      <Typography.Title level={4}>审计</Typography.Title>
      <Form layout="inline" style={{ marginBottom: 12 }} initialValues={filters} onFinish={(v: Filters) => setFilters({ ...v })}>
        <Form.Item name="kind"><Input placeholder="事件类型，如 tool.invoke" allowClear /></Form.Item>
        <Form.Item name="trace_id"><Input placeholder="trace_id" allowClear style={{ width: 280 }} /></Form.Item>
        <Form.Item name="range"><DatePicker.RangePicker /></Form.Item>
        <Form.Item name="limit" label="条数"><InputNumber min={1} max={500} /></Form.Item>
        <Button htmlType="submit" type="primary">查询</Button>
        {has("auditor", "platform_admin") && (
          <Button style={{ marginLeft: 8 }} onClick={async () => {
            const r = await act(() => post<VerifyResult>("/audit-logs/verify"));
            if (r) setVerify(r);
          }}>校验哈希链与锚点</Button>
        )}
      </Form>
      {verify && (
        <Alert style={{ marginBottom: 12 }} showIcon closable onClose={() => setVerify(null)}
          type={verify.ok ? "success" : "error"}
          message={verify.ok ? `完整：${verify.shards} 个分片，${verify.records} 条记录，哈希链与签名锚点全部校验通过` : `发现 ${verify.problems.length} 处问题`}
          description={verify.problems.map((x) => <div key={x}>{x}</div>)} />
      )}
      {logs.error && <Alert type="error" message={logs.error} />}
      <Table<AuditRecord> rowKey={(r) => `${r.shard_id}-${r.seq}`} size="small" loading={logs.loading}
        dataSource={logs.data?.items ?? []} pagination={{ pageSize: 20 }}
        expandable={{ expandedRowRender: (r) => <JsonView value={r.record} /> }}
        footer={() => (logs.data?.has_more ? "结果已截断，请缩小时间范围或提高条数上限" : null)}
        columns={[
          { title: "时间", dataIndex: "occurred_at", render: (v: string) => v.slice(0, 19).replace("T", " ") },
          { title: "类型", dataIndex: "kind", render: (v) => <Tag>{v}</Tag> },
          { title: "公司", dataIndex: "company_id", render: (v) => (v ? <OrgName id={v} /> : "-") },
          { title: "trace_id", key: "trace", render: (_, r) => <Typography.Text code copyable>{String(r.record.trace_id ?? "-")}</Typography.Text> },
          { title: "分片 / 序号", key: "pos", render: (_, r) => `${r.shard_id} / ${r.seq}` },
          { title: "哈希", dataIndex: "hash", render: (v: string) => <Typography.Text code>{v.slice(0, 12)}…</Typography.Text> },
        ]} />
    </>
  );
}
