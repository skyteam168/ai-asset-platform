import { Alert, Button, DatePicker, Form, Input, InputNumber, Table, Tag, Typography } from "antd";
import type { Dayjs } from "dayjs";
import { useState } from "react";
import { get, post, query, type Page } from "../api";
import { useAuth } from "../auth";
import { JsonView, OrgName } from "../components";
import { useAction, useLoad } from "../hooks";
import type { AuditRecord } from "../types";
import { t } from "../i18n";

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
      <Typography.Title level={4}>{t("审计")}</Typography.Title>
      <Form layout="inline" style={{ marginBottom: 12 }} initialValues={filters} onFinish={(v: Filters) => setFilters({ ...v })}>
        <Form.Item name="kind"><Input placeholder={t("事件类型，如 tool.invoke")} allowClear /></Form.Item>
        <Form.Item name="trace_id"><Input placeholder="trace_id" allowClear style={{ width: 280 }} /></Form.Item>
        <Form.Item name="range"><DatePicker.RangePicker /></Form.Item>
        <Form.Item name="limit" label={t("条数")}><InputNumber min={1} max={500} /></Form.Item>
        <Button htmlType="submit" type="primary">{t("查询")}</Button>
        {has("auditor", "platform_admin") && (
          <Button style={{ marginLeft: 8 }} onClick={async () => {
            const r = await act(() => post<VerifyResult>("/audit-logs/verify"));
            if (r) setVerify(r);
          }}>{t("校验哈希链与锚点")}</Button>
        )}
      </Form>
      {verify && (
        <Alert style={{ marginBottom: 12 }} showIcon closable onClose={() => setVerify(null)}
          type={verify.ok ? "success" : "error"}
          message={verify.ok ? t("完整：{0} 个分片，{1} 条记录，哈希链与签名锚点全部校验通过", [verify.shards, verify.records]) : t("发现 {0} 处问题", [verify.problems.length])}
          description={verify.problems.map((x) => <div key={x}>{x}</div>)} />
      )}
      {logs.error && <Alert type="error" message={logs.error} />}
      <Table<AuditRecord> rowKey={(r) => `${r.shard_id}-${r.seq}`} size="small" loading={logs.loading}
        dataSource={logs.data?.items ?? []} pagination={{ pageSize: 20 }}
        expandable={{ expandedRowRender: (r) => <JsonView value={r.record} /> }}
        footer={() => (logs.data?.has_more ? t("结果已截断，请缩小时间范围或提高条数上限") : null)}
        columns={[
          { title: t("时间"), dataIndex: "occurred_at", render: (v: string) => v.slice(0, 19).replace("T", " ") },
          { title: t("类型"), dataIndex: "kind", render: (v) => <Tag>{v}</Tag> },
          { title: t("公司"), dataIndex: "company_id", render: (v) => (v ? <OrgName id={v} /> : "-") },
          { title: "trace_id", key: "trace", render: (_, r) => <Typography.Text code copyable>{String(r.record.trace_id ?? "-")}</Typography.Text> },
          { title: t("分片 / 序号"), key: "pos", render: (_, r) => `${r.shard_id} / ${r.seq}` },
          { title: t("哈希"), dataIndex: "hash", render: (v: string) => <Typography.Text code>{v.slice(0, 12)}…</Typography.Text> },
        ]} />
    </>
  );
}
