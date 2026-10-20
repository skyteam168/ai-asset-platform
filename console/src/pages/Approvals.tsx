import { Alert, Button, Form, Input, Modal, Radio, Space, Table, Tag, Typography } from "antd";
import { useState } from "react";
import { get, post, type Page } from "../api";
import { JsonInput, JsonView, jsonRule, parseJson } from "../components";
import { useAction, useLoad } from "../hooks";
import type { Approval } from "../types";

const KIND_LABEL: Record<string, string> = { G5: "业务验收（G5）", G6: "安全审核（G6）", usage: "使用申请" };

export function ApprovalsPage() {
  const items = useLoad(() => get<Page<Approval>>("/approvals"), []);
  const [current, setCurrent] = useState<Approval | null>(null);
  return (
    <>
      <Typography.Title level={4}>审批待办</Typography.Title>
      {items.error && <Alert type="error" message={items.error} />}
      <Table<Approval> rowKey="id" size="small" loading={items.loading} dataSource={items.data?.items ?? []}
        locale={{ emptyText: "没有待办" }}
        expandable={{ expandedRowRender: (a) => <JsonView value={a.payload} /> }}
        columns={[
          { title: "类型", dataIndex: "kind", render: (k: string) => <Tag color="blue">{KIND_LABEL[k] ?? k}</Tag> },
          { title: "对象", dataIndex: "subject" },
          { title: "审批角色", dataIndex: "approver_role" },
          { title: "所需人数", dataIndex: "required_approvals" },
          { title: "截止", dataIndex: "due_at", render: (v: string) => v.slice(0, 16).replace("T", " ") },
          { title: "操作", key: "ops", render: (_, a) => <Button size="small" type="primary" onClick={() => setCurrent(a)}>处理</Button> },
        ]} />
      <DecideModal approval={current} onClose={() => setCurrent(null)} onDone={items.reload} />
    </>
  );
}

function DecideModal({ approval, onClose, onDone }: { approval: Approval | null; onClose: () => void; onDone: () => void }) {
  const [form] = Form.useForm();
  const act = useAction();
  const decision = Form.useWatch("decision", form);
  const g5 = approval?.kind === "G5";
  return (
    <Modal open={Boolean(approval)} title={approval ? `${KIND_LABEL[approval.kind] ?? approval.kind} · ${approval.subject}` : ""}
      onCancel={onClose} onOk={() => form.submit()} destroyOnClose>
      <Form form={form} layout="vertical" preserve={false}
        initialValues={{ decision: "approve", comment: "", results: g5 ? JSON.stringify({ accuracy: 0.97, latency_ms: 1200 }, null, 2) : "" }}
        onFinish={async (v) => {
          if (!approval) return;
          const body = { decision: v.decision, comment: v.comment, results: g5 && v.results ? parseJson(v.results) : undefined };
          const r = await act(() => post<{ status: string }>(`/approvals/${approval.id}/decide`, body), "已提交");
          if (r) { onClose(); onDone(); }
        }}>
        <Form.Item name="decision" label="结论">
          <Radio.Group options={[{ value: "approve", label: "通过" }, { value: "reject", label: "驳回" }]} />
        </Form.Item>
        {g5 && decision === "approve" && (
          <Form.Item name="results" label="验收实测结果（JSON，按验收单逐项填写，签字后入证据）" rules={[{ required: true }, jsonRule]}>
            <JsonInput rows={5} />
          </Form.Item>
        )}
        <Form.Item name="comment" label="意见" rules={decision === "reject" ? [{ required: true, message: "驳回需写明理由" }] : []}>
          <Input.TextArea rows={3} />
        </Form.Item>
        <Space><Typography.Text type="secondary">提交后写入审计；不能审批自己发起的申请。</Typography.Text></Space>
      </Form>
    </Modal>
  );
}
