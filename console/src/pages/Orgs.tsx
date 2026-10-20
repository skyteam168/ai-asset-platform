import { Alert, Button, Card, Col, Form, Input, InputNumber, Modal, Row, Select, Space, Switch, Table, Tree, Typography } from "antd";
import { useMemo, useState } from "react";
import { get, post, type Page } from "../api";
import { useAuth } from "../auth";
import { Clearance, OrgName, StateTag, useOrgs } from "../components";
import { useAction, useLoad } from "../hooks";
import type { OrgUnit, User } from "../types";

interface TreeNode {
  key: string;
  title: string;
  children: TreeNode[];
}

function buildTree(list: OrgUnit[]): TreeNode[] {
  const nodes = new Map(list.map((o) => [o.id, { key: o.id, title: `${o.code} · ${o.name}${o.status === "disabled" ? "（停用）" : ""}`, children: [] as TreeNode[] }]));
  const roots: TreeNode[] = [];
  list.forEach((o) => {
    const n = nodes.get(o.id)!;
    const parent = o.parent_id ? nodes.get(o.parent_id) : undefined;
    (parent ? parent.children : roots).push(n);
  });
  return roots;
}

function descendants(list: OrgUnit[], root: string): Set<string> {
  const out = new Set([root]);
  let grew = true;
  while (grew) {
    grew = false;
    list.forEach((o) => {
      if (o.parent_id && out.has(o.parent_id) && !out.has(o.id)) {
        out.add(o.id);
        grew = true;
      }
    });
  }
  return out;
}

export function OrgsPage() {
  const { list, byId, reload: reloadOrgs } = useOrgs();
  const { has } = useAuth();
  const users = useLoad(() => get<Page<User>>("/users"), []);
  const [selected, setSelected] = useState<string | null>(null);
  const [orgOpen, setOrgOpen] = useState(false);
  const [userOpen, setUserOpen] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [totpUri, setTotpUri] = useState<string | null>(null);
  const act = useAction();
  const tree = useMemo(() => buildTree(list), [list]);
  const scope = useMemo(() => (selected ? descendants(list, selected) : null), [list, selected]);
  const shown = (users.data?.items ?? []).filter((u) => !scope || scope.has(u.home_org_id));
  const sel = selected ? byId.get(selected) : undefined;
  const canManage = has("platform_admin", "company_admin");

  return (
    <Row gutter={16}>
      <Col span={8}>
        <Card title="组织树" size="small" extra={canManage && sel && sel.type !== "team" && (
          <Button size="small" onClick={() => setOrgOpen(true)}>新建下级</Button>)}>
          {/* defaultExpandAll 只在挂载时生效，等组织数据到了再挂载 */}
          {tree.length > 0 && <Tree treeData={tree} defaultExpandAll selectedKeys={selected ? [selected] : []}
            onSelect={(keys) => setSelected((keys[0] as string) ?? null)} />}
          {sel && canManage && sel.type !== "group" && sel.status === "active" && (
            <Button danger size="small" style={{ marginTop: 12 }} onClick={() => Modal.confirm({
              title: `停用 ${sel.code}？`, content: "整个子树一起停用，其下用户的数据范围随之收缩。",
              onOk: async () => { await act(() => post(`/orgs/${sel.id}/disable`), "已停用"); reloadOrgs(); },
            })}>停用该节点</Button>
          )}
        </Card>
      </Col>
      <Col span={16}>
        <Card title={sel ? `用户（${sel.code} 子树）` : "用户（全部可见）"} size="small" extra={canManage && (
          <Space>
            <Button size="small" onClick={() => setImportOpen(true)}>批量导入</Button>
            <Button size="small" type="primary" onClick={() => setUserOpen(true)}>新建用户</Button>
          </Space>)}>
          {users.error && <Alert type="error" message={users.error} />}
          <Table<User> rowKey="id" size="small" loading={users.loading} dataSource={shown} pagination={{ pageSize: 15 }}
            columns={[
              { title: "姓名", dataIndex: "display_name" },
              { title: "登录名", dataIndex: "login_name", render: (v) => v ?? "-" },
              { title: "工号", dataIndex: "employee_no", render: (v) => v ?? "-" },
              { title: "主归属", dataIndex: "home_org_id", render: (v) => <OrgName id={v} /> },
              { title: "有效密级", dataIndex: "effective_clearance", render: (v) => <Clearance level={v} /> },
              { title: "状态", dataIndex: "status", render: (v) => <StateTag state={v} /> },
            ]} />
        </Card>
      </Col>
      <NewOrgModal open={orgOpen} parent={sel} onClose={() => setOrgOpen(false)} onDone={reloadOrgs} />
      <NewUserModal open={userOpen} defaultOrg={selected} onClose={() => setUserOpen(false)}
        onDone={(u) => { users.reload(); if (u.totp_provisioning_uri) setTotpUri(u.totp_provisioning_uri); }} />
      <ImportModal open={importOpen} onClose={() => setImportOpen(false)} onDone={users.reload} />
      <Modal open={Boolean(totpUri)} title="TOTP 绑定信息（只显示这一次）" onOk={() => setTotpUri(null)}
        onCancel={() => setTotpUri(null)} cancelButtonProps={{ style: { display: "none" } }}>
        <Alert type="warning" showIcon message="请让用户立即导入验证器。关闭后无法再次查看。" style={{ marginBottom: 12 }} />
        <Typography.Paragraph copyable code style={{ wordBreak: "break-all" }}>{totpUri}</Typography.Paragraph>
      </Modal>
    </Row>
  );
}

function NewOrgModal(p: { open: boolean; parent?: OrgUnit; onClose: () => void; onDone: () => void }) {
  const [form] = Form.useForm();
  const act = useAction();
  const childTypes = p.parent?.type === "group" ? ["company"] : ["department", "team"];
  return (
    <Modal open={p.open} title={`在 ${p.parent?.code ?? ""} 下新建`} onCancel={p.onClose} onOk={() => form.submit()} destroyOnClose>
      <Form form={form} layout="vertical" preserve={false} initialValues={{ type: childTypes[0] }}
        onFinish={async (v) => {
          const r = await act(() => post("/orgs", { ...v, parent_id: p.parent!.id }), "已创建");
          if (r) { p.onClose(); p.onDone(); }
        }}>
        <Form.Item name="type" label="类型"><Select options={childTypes.map((t) => ({ value: t, label: t }))} /></Form.Item>
        <Form.Item name="code" label="编码" rules={[{ required: true }]}><Input placeholder="如 VN01-WH" /></Form.Item>
        <Form.Item name="name" label="名称" rules={[{ required: true }]}><Input /></Form.Item>
        <Form.Item name="default_clearance" label="默认密级（缺省继承上级）"><InputNumber min={1} max={4} /></Form.Item>
      </Form>
    </Modal>
  );
}

function NewUserModal(p: { open: boolean; defaultOrg: string | null; onClose: () => void; onDone: (u: User) => void }) {
  const [form] = Form.useForm();
  const { list } = useOrgs();
  const act = useAction();
  const loginEnabled = Form.useWatch("login_enabled", form);
  return (
    <Modal open={p.open} title="新建用户" onCancel={p.onClose} onOk={() => form.submit()} destroyOnClose>
      <Form form={form} layout="vertical" preserve={false} initialValues={{ home_org_id: p.defaultOrg, login_enabled: false }}
        onFinish={async (v) => {
          const u = await act(() => post<User>("/users", v), "已创建");
          if (u) { p.onClose(); p.onDone(u); }
        }}>
        <Form.Item name="display_name" label="姓名" rules={[{ required: true }]}><Input /></Form.Item>
        <Form.Item name="home_org_id" label="主归属" rules={[{ required: true }]}>
          <Select showSearch optionFilterProp="label"
            options={list.filter((o) => o.type !== "group" && o.status === "active").map((o) => ({ value: o.id, label: `${o.code} · ${o.name}` }))} />
        </Form.Item>
        <Form.Item name="employee_no" label="工号"><Input /></Form.Item>
        <Form.Item name="login_enabled" label="可登录控制台" valuePropName="checked"><Switch /></Form.Item>
        {loginEnabled && (
          <>
            <Form.Item name="login_name" label="登录名" rules={[{ required: true }]}><Input /></Form.Item>
            <Form.Item name="password" label="初始口令（至少 12 位）" rules={[{ required: true, min: 12 }]}><Input.Password /></Form.Item>
          </>
        )}
      </Form>
    </Modal>
  );
}

interface ImportResult { batch_id: string; dry_run: boolean; created: number; unchanged: number; errors: { line: number; message: string }[] }

function ImportModal(p: { open: boolean; onClose: () => void; onDone: () => void }) {
  const [content, setContent] = useState("display_name,employee_no,home_org_code,email\n");
  const [result, setResult] = useState<ImportResult | null>(null);
  const act = useAction();
  const run = async (dry: boolean) => {
    const r = await act(() => post<ImportResult>("/users/import", { format: "csv", content, dry_run: dry }), dry ? undefined : "已导入");
    if (r) { setResult(r); if (!dry) p.onDone(); }
  };
  return (
    <Modal open={p.open} title="批量导入用户（CSV）" width={720} onCancel={() => { setResult(null); p.onClose(); }}
      footer={<Space><Button onClick={() => run(true)}>预览（不落库）</Button>
        <Button type="primary" disabled={!result?.dry_run || result.errors.length > 0} onClick={() => run(false)}>确认导入</Button></Space>}>
      <Input.TextArea rows={8} value={content} onChange={(e) => { setContent(e.target.value); setResult(null); }}
        style={{ fontFamily: "Consolas, monospace" }} />
      {result && (
        <Alert style={{ marginTop: 12 }} type={result.errors.length ? "warning" : "success"} showIcon
          message={`${result.dry_run ? "预览" : "已导入"}：新建 ${result.created}，已存在 ${result.unchanged}，错误 ${result.errors.length}；批次 ${result.batch_id}`}
          description={result.errors.map((e) => <div key={e.line}>第 {e.line} 行：{e.message}</div>)} />
      )}
    </Modal>
  );
}
