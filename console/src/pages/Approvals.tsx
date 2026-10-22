import { Alert, Badge, Button, Descriptions, Drawer, Form, Input, Modal, Radio, Space, Tabs, Tag, Typography } from "antd";
import { useState } from "react";
import { Link } from "react-router-dom";
import { get, post, type Page } from "../api";
import { JsonInput, jsonRule, JsonView, PageCard, PAGINATION, parseJson, StateTag } from "../components";
import { ListTable } from "../ListTable";
import { useAction, useLoad } from "../hooks";
import type { Approval, ClearanceRequest, RunView, Todo, TodoConfirmation, TodoTask } from "../types";
import { useAuth } from "../auth";
import { t } from "../i18n";

const KIND_LABEL: Record<string, string> = { G5: t("业务验收（G5）"), G6: t("安全审核（G6）"), usage: t("使用申请") };
const TASK_LABEL: Record<string, string> = { confirm: t("确认"), approve: t("审批"), input: t("补录") };
const time = (v: string | null | undefined) => (v ? v.slice(0, 16).replace("T", " ") : "-");

export function ApprovalsPage() {
  const items = useLoad(() => get<Page<Approval>>("/approvals"), []);
  const todo = useLoad(() => get<Todo>("/todo"), []);
  const clearanceReqs = useLoad(() => get<Page<ClearanceRequest>>("/clearance-requests"), []);
  const [current, setCurrent] = useState<Approval | null>(null);
  const confirmations = todo.data?.confirmations ?? [];
  const tasks = todo.data?.tasks ?? [];
  const { has } = useAuth();
  return (
    <PageCard>
      <Tabs items={[
        {
          key: "gates",
          label: <Badge count={items.data?.items.length ?? 0} size="small" offset={[8, 0]}>{t("门禁与使用申请")}</Badge>,
          children: (
            <>
              {items.error && <Alert type="error" message={items.error} />}
              <ListTable<Approval> pagination={PAGINATION} rowKey="id" loading={items.loading} dataSource={items.data?.items ?? []}
                locale={{ emptyText: t("没有待办") }}
                expandable={{ expandedRowRender: (a) => <JsonView value={a.payload} /> }}
                columns={[
                  { title: t("类型"), dataIndex: "kind", render: (k: string) => <Tag color="blue">{KIND_LABEL[k] ?? k}</Tag> },
                  { title: t("对象"), dataIndex: "subject" },
                  { title: t("审批角色"), dataIndex: "approver_role" },
                  { title: t("所需人数"), dataIndex: "required_approvals" },
                  { title: t("截止"), dataIndex: "due_at", render: time },
                  { title: t("操作"), key: "ops", render: (_, a) => <Button size="small" type="primary" onClick={() => setCurrent(a)}>{t("处理")}</Button> },
                ]} />
            </>
          ),
        },
        {
          key: "confirm",
          label: <Badge count={confirmations.length} size="small" offset={[8, 0]}>{t("调用确认")}</Badge>,
          children: <Confirmations rows={confirmations} loading={todo.loading} error={todo.error} onDone={todo.reload} />,
        },
        {
          key: "tasks",
          label: <Badge count={tasks.length} size="small" offset={[8, 0]}>{t("工作流任务")}</Badge>,
          children: <Tasks rows={tasks} loading={todo.loading} error={todo.error} onDone={todo.reload} />,
        },
        {
          key: "clearance",
          label: <Badge count={clearanceReqs.data?.items.filter((r) => r.status === "pending").length ?? 0} size="small" offset={[8, 0]}>{t("密级申请")}</Badge>,
          children: <ClearanceRequests list={clearanceReqs} canDecide={has("data_owner", "platform_admin")} onDone={clearanceReqs.reload} />,
        },
      ]} />
      <DecideModal approval={current} onClose={() => setCurrent(null)} onDone={items.reload} />
    </PageCard>
  );
}

function Confirmations(p: { rows: TodoConfirmation[]; loading: boolean; error: string | null; onDone: () => void }) {
  const [current, setCurrent] = useState<TodoConfirmation | null>(null);
  return (
    <>
      <Alert type="info" showIcon style={{ marginBottom: 12 }}
        message={t("写操作在执行前冻结了参数（只显示参数摘要）。通过后平台按冻结内容执行一次；审批人不能是被代表的用户本人。")} />
      {p.error && <Alert type="error" message={p.error} />}
      <ListTable<TodoConfirmation> pagination={PAGINATION} rowKey="id" loading={p.loading} dataSource={p.rows} locale={{ emptyText: t("没有待确认的调用") }}
        columns={[
          { title: t("类型"), dataIndex: "kind", render: (k: string) => <Tag color={k === "approval" ? "orange" : "blue"}>{k === "approval" ? t("审批") : t("本人确认")}</Tag> },
          { title: t("内容"), dataIndex: "summary" },
          { title: t("能力"), key: "cap", render: (_, c) => <Typography.Text code>{c.capability_id}@{c.version}</Typography.Text> },
          { title: t("参数摘要"), dataIndex: "args_sha256", render: (v: string) => <Typography.Text code>{v.slice(0, 12)}</Typography.Text> },
          { title: t("来自 Workflow"), dataIndex: "run_id", render: (v: string | null) => (v ? <RunLink id={v} /> : "-") },
          { title: t("过期"), dataIndex: "expires_at", render: time },
          { title: t("操作"), key: "ops", render: (_, c) => <Button size="small" type="primary" onClick={() => setCurrent(c)}>{t("处理")}</Button> },
        ]} />
      <TodoDecide
        title={current ? t("调用确认 · {0}", [current.summary]) : ""}
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
      <ListTable<TodoTask> pagination={PAGINATION} rowKey="id" loading={p.loading} dataSource={p.rows} locale={{ emptyText: t("没有工作流任务") }}
        columns={[
          { title: t("类型"), dataIndex: "kind", render: (k: string) => <Tag>{TASK_LABEL[k] ?? k}</Tag> },
          { title: t("内容"), dataIndex: "summary" },
          { title: t("节点"), dataIndex: "node_id", render: (v: string) => <Typography.Text code>{v}</Typography.Text> },
          { title: "Run", dataIndex: "run_id", render: (v: string) => <RunLink id={v} /> },
          { title: t("截止"), dataIndex: "deadline", render: time },
          { title: t("操作"), key: "ops", render: (_, task) => <Button size="small" type="primary" onClick={() => setCurrent(task)}>{t("处理")}</Button> },
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
          if (await act(() => post(p.url, body), t("已提交"))) { p.onClose(); p.onDone(); }
        }}>
        <Form.Item name="decision" label={t("结论")}>
          <Radio.Group options={[{ value: "approve", label: p.input ? t("提交") : t("通过") }, { value: "reject", label: t("驳回") }]} />
        </Form.Item>
        {p.input && decision === "approve" && (
          <Form.Item name="data" label={t("补录内容（JSON）")} rules={[{ required: true }, jsonRule]}
            extra={<pre style={{ margin: 0, fontSize: 11 }}>{JSON.stringify(p.input, null, 2)}</pre>}>
            <JsonInput rows={5} />
          </Form.Item>
        )}
        <Form.Item name="comment" label={t("意见")} rules={decision === "reject" ? [{ required: true, message: t("驳回需写明理由") }] : []}>
          <Input.TextArea rows={3} />
        </Form.Item>
      </Form>
    </Modal>
  );
}

function RunLink({ id }: { id: string }) {
  const [open, setOpen] = useState(false);
  return (
    <Space size={4}>
      <Link to={`/runs/${id}`}>{id.slice(0, 8)}</Link>
      <Button size="small" type="link" onClick={() => setOpen(true)}>{t("抽屉")}</Button>
      {open && <RunDrawer id={id} onClose={() => setOpen(false)} />}
    </Space>
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
            <Descriptions.Item label={t("能力")}>{v.capability_id}@{v.version}</Descriptions.Item>
            <Descriptions.Item label={t("状态")}><StateTag state={v.status} /></Descriptions.Item>
            <Descriptions.Item label={t("当前节点")}>{v.current}</Descriptions.Item>
            <Descriptions.Item label={t("污点")}>{v.tainted ? <Tag color="red">tainted：{v.taint_sources.join("，")}</Tag> : t("否")}</Descriptions.Item>
            <Descriptions.Item label={t("预算用量")} span={2}>
              {t("步数 {0}，LLM 调用 {1}，token {2}", [v.budget_used.steps, v.budget_used.llm_calls, v.budget_used.tokens])}
            </Descriptions.Item>
          </Descriptions>
          {v.error && <Alert type="error" showIcon style={{ marginBottom: 12 }} message={`${v.error.code}：${v.error.message ?? ""}`} />}
          <ListTable rowKey="node_id" size="small" pagination={false} dataSource={v.steps}
            columns={[
              { title: t("节点"), dataIndex: "node_id" },
              { title: t("类型"), dataIndex: "kind" },
              { title: t("状态"), dataIndex: "status", render: (s: string) => <StateTag state={s} /> },
              { title: t("尝试"), dataIndex: "attempts" },
              { title: t("执行者"), dataIndex: "executor" },
            ]} />
          <Space style={{ marginTop: 12 }}><Typography.Text type="secondary">trace_id：{v.trace_id}</Typography.Text></Space>
        </>
      )}
    </Drawer>
  );
}

// ── 密级申请 ──────────────────────────────────────────────────────────────────

function ClearanceRequests({ list, canDecide, onDone }: {
  list: { data: Page<ClearanceRequest> | null; loading: boolean; error: string | null };
  canDecide: boolean;
  onDone: () => void;
}) {
  const [deciding, setDeciding] = useState<ClearanceRequest | null>(null);
  const [form] = Form.useForm();
  const act = useAction();
  return (
    <>
      {list.error && <Alert type="error" message={list.error} style={{ marginBottom: 8 }} />}
      <ListTable<ClearanceRequest> pagination={PAGINATION} rowKey="id" loading={list.loading} dataSource={list.data?.items ?? []}
        locale={{ emptyText: t("没有密级申请") }}
        columns={[
          { title: t("用户"), dataIndex: "user_id", render: (v: string) => <Typography.Text code>{v.slice(0, 8)}</Typography.Text> },
          { title: t("申请密级"), dataIndex: "requested_level", render: (v: number) => `L${v}` },
          { title: t("申请理由"), dataIndex: "reason", ellipsis: true },
          { title: t("到期"), dataIndex: "expires_at", render: (v: string | null) => (v ? v.slice(0, 10) : "-") },
          { title: t("状态"), dataIndex: "status", render: (v) => <StateTag state={v} /> },
          {
            title: t("操作"), key: "ops",
            render: (_, r) => canDecide && r.status === "pending"
              ? <Button size="small" type="primary" onClick={() => setDeciding(r)}>{t("审批")}</Button>
              : null,
          },
        ]}
      />
      <Modal open={Boolean(deciding)} title={t("审批密级申请")} onCancel={() => setDeciding(null)} onOk={() => form.submit()} destroyOnClose>
        <Form form={form} layout="vertical" preserve={false} initialValues={{ decision: "approve" }}
          onFinish={async (v) => {
            if (!deciding) return;
            if (await act(() => post(`/clearance-requests/${deciding.id}/decide`, { decision: v.decision, comment: v.comment || undefined }), t("已提交"))) {
              setDeciding(null); onDone();
            }
          }}>
          <Form.Item name="decision" label={t("结论")}>
            <Radio.Group options={[{ value: "approve", label: t("批准") }, { value: "reject", label: t("驳回") }]} />
          </Form.Item>
          <Form.Item name="comment" label={t("意见")}>
            <Input.TextArea rows={3} />
          </Form.Item>
        </Form>
      </Modal>
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
          const r = await act(() => post<{ status: string }>(`/approvals/${approval.id}/decide`, body), t("已提交"));
          if (r) { onClose(); onDone(); }
        }}>
        <Form.Item name="decision" label={t("结论")}>
          <Radio.Group options={[{ value: "approve", label: t("通过") }, { value: "reject", label: t("驳回") }]} />
        </Form.Item>
        {g5 && decision === "approve" && (
          <Form.Item name="results" label={t("验收实测结果（JSON，按验收单逐项填写，签字后入证据）")} rules={[{ required: true }, jsonRule]}>
            <JsonInput rows={5} />
          </Form.Item>
        )}
        <Form.Item name="comment" label={t("意见")} rules={decision === "reject" ? [{ required: true, message: t("驳回需写明理由") }] : []}>
          <Input.TextArea rows={3} />
        </Form.Item>
        <Space><Typography.Text type="secondary">{t("提交后写入审计；不能审批自己发起的申请。")}</Typography.Text></Space>
      </Form>
    </Modal>
  );
}
