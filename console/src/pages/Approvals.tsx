import { Alert, Badge, Button, Descriptions, Drawer, Form, Input, Modal, Radio, Space, Table, Tabs, Tag, Typography } from "antd";
import { useState } from "react";
import { get, post, type Page } from "../api";
import { JsonInput, JsonView, StateTag, jsonRule, parseJson } from "../components";
import { useAction, useLoad } from "../hooks";
import type { Approval, RunView, Todo, TodoConfirmation, TodoTask } from "../types";

const KIND_LABEL: Record<string, string> = { G5: "业务验收（G5）", G6: "安全审核（G6）", usage: "使用申请" };
const TASK_LABEL: Record<string, string> = { confirm: "确认", approve: "审批", input: "补录" };
const time = (v: string | null | undefined) => (v ? v.slice(0, 16).replace("T", " ") : "-");

export function ApprovalsPage() {
  const items = useLoad(() => get<Page<Approval>>("/approvals"), []);
  const todo = useLoad(() => get<Todo>("/todo"), []);
  const [current, setCurrent] = useState<Approval | null>(null);
  const confirmations = todo.data?.confirmations ?? [];
  const tasks = todo.data?.tasks ?? [];
  return (
    <>
      <Typography.Title level={4}>审批待办</Typography.Title>
      <Tabs items={[
        {
          key: "gates",
          label: <Badge count={items.data?.items.length ?? 0} size="small" offset={[8, 0]}>门禁与使用申请</Badge>,
          children: (
            <>
              {items.error && <Alert type="error" message={items.error} />}
              <Table<Approval> rowKey="id" size="small" loading={items.loading} dataSource={items.data?.items ?? []}
                locale={{ emptyText: "没有待办" }}
                expandable={{ expandedRowRender: (a) => <JsonView value={a.payload} /> }}
                columns={[
                  { title: "类型", dataIndex: "kind", render: (k: string) => <Tag color="blue">{KIND_LABEL[k] ?? k}</Tag> },
                  { title: "对象", dataIndex: "subject" },
                  { title: "审批角色", dataIndex: "approver_role" },
                  { title: "所需人数", dataIndex: "required_approvals" },
                  { title: "截止", dataIndex: "due_at", render: time },
                  { title: "操作", key: "ops", render: (_, a) => <Button size="small" type="primary" onClick={() => setCurrent(a)}>处理</Button> },
                ]} />
            </>
          ),
        },
        {
          key: "confirm",
          label: <Badge count={confirmations.length} size="small" offset={[8, 0]}>调用确认</Badge>,
          children: <Confirmations rows={confirmations} loading={todo.loading} error={todo.error} onDone={todo.reload} />,
        },
        {
          key: "tasks",
          label: <Badge count={tasks.length} size="small" offset={[8, 0]}>工作流任务</Badge>,
          children: <Tasks rows={tasks} loading={todo.loading} error={todo.error} onDone={todo.reload} />,
        },
      ]} />
      <DecideModal approval={current} onClose={() => setCurrent(null)} onDone={items.reload} />
    </>
  );
}

function Confirmations(p: { rows: TodoConfirmation[]; loading: boolean; error: string | null; onDone: () => void }) {
  const [current, setCurrent] = useState<TodoConfirmation | null>(null);
  return (
    <>
      <Alert type="info" showIcon style={{ marginBottom: 12 }}
        message="写操作在执行前冻结了参数（只显示参数摘要）。通过后平台按冻结内容执行一次；审批人不能是被代表的用户本人。" />
      {p.error && <Alert type="error" message={p.error} />}
      <Table<TodoConfirmation> rowKey="id" size="small" loading={p.loading} dataSource={p.rows} locale={{ emptyText: "没有待确认的调用" }}
        columns={[
          { title: "类型", dataIndex: "kind", render: (k: string) => <Tag color={k === "approval" ? "orange" : "blue"}>{k === "approval" ? "审批" : "本人确认"}</Tag> },
          { title: "内容", dataIndex: "summary" },
          { title: "能力", key: "cap", render: (_, c) => <Typography.Text code>{c.capability_id}@{c.version}</Typography.Text> },
          { title: "参数摘要", dataIndex: "args_sha256", render: (v: string) => <Typography.Text code>{v.slice(0, 12)}</Typography.Text> },
          { title: "来自 Workflow", dataIndex: "run_id", render: (v: string | null) => (v ? <RunLink id={v} /> : "-") },
          { title: "过期", dataIndex: "expires_at", render: time },
          { title: "操作", key: "ops", render: (_, c) => <Button size="small" type="primary" onClick={() => setCurrent(c)}>处理</Button> },
        ]} />
      <TodoDecide
        title={current ? `调用确认 · ${current.summary}` : ""}
        open={Boolean(current)}
        url={current ? `/todo/confirmations/${current.id}/decide` : ""}
        onClose={() => setCurrent(null)}
        onDone={p.onDone}
      />
    </>
  );
}

function Tasks(p: { rows: TodoTask[]; loading: boolean; error: string | null; onDone: () => void }) {
  const [current, setCurrent] = useState<TodoTask | null>(null);
  return (
    <>
      {p.error && <Alert type="error" message={p.error} />}
      <Table<TodoTask> rowKey="id" size="small" loading={p.loading} dataSource={p.rows} locale={{ emptyText: "没有工作流任务" }}
        columns={[
          { title: "类型", dataIndex: "kind", render: (k: string) => <Tag>{TASK_LABEL[k] ?? k}</Tag> },
          { title: "内容", dataIndex: "summary" },
          { title: "节点", dataIndex: "node_id", render: (v: string) => <Typography.Text code>{v}</Typography.Text> },
          { title: "Run", dataIndex: "run_id", render: (v: string) => <RunLink id={v} /> },
          { title: "截止", dataIndex: "deadline", render: time },
          { title: "操作", key: "ops", render: (_, t) => <Button size="small" type="primary" onClick={() => setCurrent(t)}>处理</Button> },
        ]} />
      <TodoDecide
        title={current ? `${TASK_LABEL[current.kind] ?? current.kind} · ${current.summary}` : ""}
        open={Boolean(current)}
        url={current ? `/todo/tasks/${current.id}/decide` : ""}
        input={current?.kind === "input" ? (current.data?.input_schema ?? {}) : undefined}
        onClose={() => setCurrent(null)}
        onDone={p.onDone}
      />
    </>
  );
}

function TodoDecide(p: {
  title: string; open: boolean; url: string; input?: Record<string, unknown>; onClose: () => void; onDone: () => void;
}) {
  const [form] = Form.useForm();
  const act = useAction();
  const decision = Form.useWatch("decision", form);
  return (
    <Modal open={p.open} title={p.title} onCancel={p.onClose} onOk={() => form.submit()} destroyOnClose>
      <Form form={form} layout="vertical" preserve={false} initialValues={{ decision: "approve", comment: "", data: "{}" }}
        onFinish={async (v) => {
          const body = { decision: v.decision, comment: v.comment || undefined, data: p.input ? parseJson(v.data) : undefined };
          if (await act(() => post(p.url, body), "已提交")) { p.onClose(); p.onDone(); }
        }}>
        <Form.Item name="decision" label="结论">
          <Radio.Group options={[{ value: "approve", label: p.input ? "提交" : "通过" }, { value: "reject", label: "驳回" }]} />
        </Form.Item>
        {p.input && decision === "approve" && (
          <Form.Item name="data" label="补录内容（JSON）" rules={[{ required: true }, jsonRule]}
            extra={<pre style={{ margin: 0, fontSize: 11 }}>{JSON.stringify(p.input, null, 2)}</pre>}>
            <JsonInput rows={5} />
          </Form.Item>
        )}
        <Form.Item name="comment" label="意见" rules={decision === "reject" ? [{ required: true, message: "驳回需写明理由" }] : []}>
          <Input.TextArea rows={3} />
        </Form.Item>
      </Form>
    </Modal>
  );
}

function RunLink({ id }: { id: string }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button size="small" type="link" onClick={() => setOpen(true)}>{id.slice(0, 8)}</Button>
      {open && <RunDrawer id={id} onClose={() => setOpen(false)} />}
    </>
  );
}

function RunDrawer({ id, onClose }: { id: string; onClose: () => void }) {
  const run = useLoad(() => get<RunView>(`/runs/${id}`), [id]);
  const v = run.data;
  return (
    <Drawer open width={720} title={`Workflow Run ${id.slice(0, 8)}`} onClose={onClose}>
      {run.error && <Alert type="error" message={run.error} />}
      {v && (
        <>
          <Descriptions size="small" column={2} bordered style={{ marginBottom: 12 }}>
            <Descriptions.Item label="能力">{v.capability_id}@{v.version}</Descriptions.Item>
            <Descriptions.Item label="状态"><StateTag state={v.status} /></Descriptions.Item>
            <Descriptions.Item label="当前节点">{v.current}</Descriptions.Item>
            <Descriptions.Item label="污点">{v.tainted ? <Tag color="red">tainted：{v.taint_sources.join("，")}</Tag> : "否"}</Descriptions.Item>
            <Descriptions.Item label="预算用量" span={2}>
              步数 {v.budget_used.steps}，LLM 调用 {v.budget_used.llm_calls}，token {v.budget_used.tokens}
            </Descriptions.Item>
          </Descriptions>
          {v.error && <Alert type="error" showIcon style={{ marginBottom: 12 }} message={`${v.error.code}：${v.error.message ?? ""}`} />}
          <Table rowKey="node_id" size="small" pagination={false} dataSource={v.steps}
            columns={[
              { title: "节点", dataIndex: "node_id" },
              { title: "类型", dataIndex: "kind" },
              { title: "状态", dataIndex: "status", render: (s: string) => <StateTag state={s} /> },
              { title: "尝试", dataIndex: "attempts" },
              { title: "执行者", dataIndex: "executor" },
            ]} />
          <Space style={{ marginTop: 12 }}><Typography.Text type="secondary">trace_id：{v.trace_id}</Typography.Text></Space>
        </>
      )}
    </Drawer>
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
