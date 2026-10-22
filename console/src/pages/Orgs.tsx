import { Alert, Button, Card, Col, DatePicker, Descriptions, Drawer, Form, Input, InputNumber, Modal, Row, Select, Space, Switch, Tabs, Tree, Typography } from "antd";
import dayjs from "dayjs";
import { useMemo, useState } from "react";
import { del, get, post, type Page } from "../api";
import { useAuth } from "../auth";
import { Clearance, OrgName, PAGINATION, StateTag, useOrgs } from "../components";
import { ListTable } from "../ListTable";
import { useAction, useLoad } from "../hooks";
import type { OrgUnit, RoleAssignment, User } from "../types";
import { t } from "../i18n";

interface TreeNode {
  key: string;
  title: string;
  children: TreeNode[];
}

function buildTree(list: OrgUnit[]): TreeNode[] {
  const nodes = new Map(list.map((o) => [o.id, { key: o.id, title: `${o.code} · ${o.name}${o.status === "disabled" ? t("（停用）") : ""}`, children: [] as TreeNode[] }]));
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
  const [drawerUser, setDrawerUser] = useState<User | null>(null);
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
        <Card title={t("组织树")} bordered={false} style={{ borderRadius: 10 }} extra={canManage && sel && sel.type !== "team" && (
          <Button size="small" onClick={() => setOrgOpen(true)}>{t("新建下级")}</Button>)}>
          {/* defaultExpandAll 只在挂载时生效，等组织数据到了再挂载 */}
          {tree.length > 0 && <Tree treeData={tree} defaultExpandAll selectedKeys={selected ? [selected] : []}
            onSelect={(keys) => setSelected((keys[0] as string) ?? null)} />}
          {sel && canManage && sel.type !== "group" && sel.status === "active" && (
            <Button danger size="small" style={{ marginTop: 12 }} onClick={() => Modal.confirm({
              title: t("停用 {0}？", [sel.code]), content: t("整个子树一起停用，其下用户的数据范围随之收缩。"),
              onOk: async () => { await act(() => post(`/orgs/${sel.id}/disable`), t("已停用")); reloadOrgs(); },
            })}>{t("停用该节点")}</Button>
          )}
        </Card>
      </Col>
      <Col span={16}>
        <Card title={sel ? t("用户（{0} 子树）", [sel.code]) : t("用户（全部可见）")} bordered={false} style={{ borderRadius: 10 }} extra={canManage && (
          <Space>
            <Button size="small" onClick={() => setImportOpen(true)}>{t("批量导入")}</Button>
            <Button size="small" type="primary" onClick={() => setUserOpen(true)}>{t("新建用户")}</Button>
          </Space>)}>
          {users.error && <Alert type="error" message={users.error} />}
          <ListTable<User> rowKey="id" loading={users.loading} dataSource={shown} pagination={{ ...PAGINATION, defaultPageSize: 15 }}
            onRow={(r) => ({ onClick: () => setDrawerUser(r), style: { cursor: "pointer" } })}
            columns={[
              { title: t("姓名"), dataIndex: "display_name", render: (v) => <Typography.Link>{v}</Typography.Link> },
              { title: t("登录名"), dataIndex: "login_name", render: (v) => v ?? "-" },
              { title: t("工号"), dataIndex: "employee_no", render: (v) => v ?? "-" },
              { title: t("主归属"), dataIndex: "home_org_id", render: (v) => <OrgName id={v} /> },
              { title: t("有效密级"), dataIndex: "effective_clearance", render: (v) => <Clearance level={v} /> },
              { title: t("状态"), dataIndex: "status", render: (v) => <StateTag state={v} /> },
            ]} />
        </Card>
        {sel?.type === "company" && canManage && (
          <div style={{ marginTop: 16 }}>
            <BusinessRoles companyId={sel.id} companyCode={sel.code} />
          </div>
        )}
      </Col>
      <NewOrgModal open={orgOpen} parent={sel} onClose={() => setOrgOpen(false)} onDone={reloadOrgs} />
      <NewUserModal open={userOpen} defaultOrg={selected} onClose={() => setUserOpen(false)}
        onDone={(u) => { users.reload(); if (u.totp_provisioning_uri) setTotpUri(u.totp_provisioning_uri); }} />
      <ImportModal open={importOpen} onClose={() => setImportOpen(false)} onDone={users.reload} />
      <UserDrawer user={drawerUser} canManage={canManage} orgList={list} onClose={() => setDrawerUser(null)} />
      <Modal open={Boolean(totpUri)} title={t("TOTP 绑定信息（只显示这一次）")} onOk={() => setTotpUri(null)}
        onCancel={() => setTotpUri(null)} cancelButtonProps={{ style: { display: "none" } }}>
        <Alert type="warning" showIcon message={t("请让用户立即导入验证器。关闭后无法再次查看。")} style={{ marginBottom: 12 }} />
        <Typography.Paragraph copyable code style={{ wordBreak: "break-all" }}>{totpUri}</Typography.Paragraph>
      </Modal>
    </Row>
  );
}

interface BizRole {
  company_id: string;
  code: string;
  name: string;
  description: string;
}

function BusinessRoles({ companyId, companyCode }: { companyId: string; companyCode: string }) {
  const roles = useLoad(() => get<Page<BizRole>>(`/companies/${companyId}/business-roles`), [companyId]);
  const [open, setOpen] = useState(false);
  const [form] = Form.useForm();
  const act = useAction();
  return (
    <Card
      title={t("业务角色（{0}）", [companyCode])}
      bordered={false}
      style={{ borderRadius: 10 }}
      extra={<Button size="small" type="primary" onClick={() => setOpen(true)}>{t("新建业务角色")}</Button>}
    >
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message={t("用于 Workflow 人工任务分派（如 role:purchase_manager@公司）；不授予控制台管理权限。")}
      />
      {roles.error && <Alert type="error" message={roles.error} style={{ marginBottom: 12 }} />}
      <ListTable<BizRole>
        size="small"
        rowKey="code"
        loading={roles.loading}
        pagination={false}
        dataSource={roles.data?.items ?? []}
        locale={{ emptyText: t("尚未登记业务角色") }}
        columns={[
          { title: t("编码"), dataIndex: "code", render: (v: string) => <Typography.Text code>{v}</Typography.Text> },
          { title: t("名称"), dataIndex: "name" },
          { title: t("说明"), dataIndex: "description", render: (v: string) => v || "-" },
          {
            title: t("操作"),
            key: "ops",
            render: (_, r) => (
              <Button
                size="small"
                danger
                onClick={() => Modal.confirm({
                  title: t("删除业务角色 {0}？", [r.code]),
                  onOk: async () => {
                    if (await act(() => del(`/companies/${companyId}/business-roles/${r.code}`), t("已删除"))) {
                      roles.reload();
                    }
                  },
                })}
              >
                {t("删除")}
              </Button>
            ),
          },
        ]}
      />
      <Modal open={open} title={t("新建业务角色")} onCancel={() => setOpen(false)} onOk={() => form.submit()} destroyOnClose>
        <Form
          form={form}
          layout="vertical"
          preserve={false}
          onFinish={async (v) => {
            if (await act(
              () => post(`/companies/${companyId}/business-roles`, v),
              t("已创建业务角色"),
            )) {
              setOpen(false);
              roles.reload();
            }
          }}
        >
          <Form.Item
            name="code"
            label={t("编码")}
            rules={[{ required: true, pattern: /^[a-z][a-z0-9_]{1,63}$/, message: t("小写字母开头，仅含 a-z / 0-9 / _") }]}
          >
            <Input placeholder="purchase_manager" />
          </Form.Item>
          <Form.Item name="name" label={t("名称")} rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="description" label={t("说明")}><Input /></Form.Item>
        </Form>
      </Modal>
    </Card>
  );
}

function NewOrgModal(p: { open: boolean; parent?: OrgUnit; onClose: () => void; onDone: () => void }) {
  const [form] = Form.useForm();
  const act = useAction();
  const childTypes = p.parent?.type === "group" ? ["company"] : ["department", "team"];
  return (
    <Modal open={p.open} title={t("在 {0} 下新建", [p.parent?.code ?? ""])} onCancel={p.onClose} onOk={() => form.submit()} destroyOnClose>
      <Form form={form} layout="vertical" preserve={false} initialValues={{ type: childTypes[0] }}
        onFinish={async (v) => {
          const r = await act(() => post("/orgs", { ...v, parent_id: p.parent!.id }), t("已创建"));
          if (r) { p.onClose(); p.onDone(); }
        }}>
        <Form.Item name="type" label={t("类型")}><Select options={childTypes.map((x) => ({ value: x, label: x }))} /></Form.Item>
        <Form.Item name="code" label={t("编码")} rules={[{ required: true }]}><Input placeholder={t("如 VN01-WH")} /></Form.Item>
        <Form.Item name="name" label={t("名称")} rules={[{ required: true }]}><Input /></Form.Item>
        <Form.Item name="default_clearance" label={t("默认密级（缺省继承上级）")}><InputNumber min={1} max={4} /></Form.Item>
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
    <Modal open={p.open} title={t("新建用户")} onCancel={p.onClose} onOk={() => form.submit()} destroyOnClose>
      <Form form={form} layout="vertical" preserve={false} initialValues={{ home_org_id: p.defaultOrg, login_enabled: false }}
        onFinish={async (v) => {
          const u = await act(() => post<User>("/users", v), t("已创建"));
          if (u) { p.onClose(); p.onDone(u); }
        }}>
        <Form.Item name="display_name" label={t("姓名")} rules={[{ required: true }]}><Input /></Form.Item>
        <Form.Item name="home_org_id" label={t("主归属")} rules={[{ required: true }]}>
          <Select showSearch optionFilterProp="label"
            options={list.filter((o) => o.type !== "group" && o.status === "active").map((o) => ({ value: o.id, label: `${o.code} · ${o.name}` }))} />
        </Form.Item>
        <Form.Item name="employee_no" label={t("工号")}><Input /></Form.Item>
        <Form.Item name="login_enabled" label={t("可登录控制台")} valuePropName="checked"><Switch /></Form.Item>
        {loginEnabled && (
          <>
            <Form.Item name="login_name" label={t("登录名")} rules={[{ required: true }]}><Input /></Form.Item>
            <Form.Item name="password" label={t("初始口令（至少 12 位）")} rules={[{ required: true, min: 12 }]}><Input.Password /></Form.Item>
          </>
        )}
      </Form>
    </Modal>
  );
}

// ── UserDrawer ────────────────────────────────────────────────────────────────

const PLATFORM_ROLES = ["platform_admin", "asset_developer", "data_owner", "business_owner", "company_admin", "auditor"];

function UserDrawer({ user, canManage, orgList, onClose }: {
  user: User | null; canManage: boolean; orgList: OrgUnit[]; onClose: () => void;
}) {
  if (!user) return null;
  return (
    <Drawer open width={720} title={user.display_name} onClose={onClose} destroyOnClose>
      <Descriptions size="small" column={2} bordered style={{ marginBottom: 12 }}>
        <Descriptions.Item label={t("工号")}>{user.employee_no ?? "-"}</Descriptions.Item>
        <Descriptions.Item label={t("登录名")}>{user.login_name ?? "-"}</Descriptions.Item>
        <Descriptions.Item label={t("有效密级")}><Clearance level={user.effective_clearance} /></Descriptions.Item>
        <Descriptions.Item label={t("状态")}><StateTag state={user.status} /></Descriptions.Item>
      </Descriptions>
      <Tabs items={[
        { key: "roles", label: t("角色"), children: <UserRolesTab userId={user.id} canManage={canManage} orgList={orgList} /> },
        { key: "clearance", label: t("密级申请"), children: <UserClearanceTab userId={user.id} canManage={canManage} /> },
      ]} />
    </Drawer>
  );
}

function UserRolesTab({ userId, canManage, orgList }: { userId: string; canManage: boolean; orgList: OrgUnit[] }) {
  const roles = useLoad(() => get<Page<RoleAssignment>>(`/users/${userId}/roles`), [userId]);
  const [form] = Form.useForm();
  const act = useAction();
  const scopeOrgId = Form.useWatch("scope_org_id", form);
  const selectedOrg = orgList.find((o) => o.id === scopeOrgId);
  const bizRoles = useLoad(
    () => (selectedOrg?.type === "company" ? get<Page<{ code: string; name: string }>>(`/companies/${selectedOrg.id}/business-roles`) : Promise.resolve(null)),
    [selectedOrg?.id],
  );
  const roleOptions = [
    ...PLATFORM_ROLES.map((r) => ({ value: r, label: r })),
    ...(bizRoles.data?.items ?? []).map((r) => ({ value: r.code, label: `${r.code}（${r.name}）` })),
  ];
  return (
    <Space direction="vertical" style={{ width: "100%" }}>
      <ListTable<RoleAssignment> rowKey="id" size="small" loading={roles.loading} pagination={false}
        dataSource={roles.data?.items ?? []}
        locale={{ emptyText: t("尚无角色") }}
        columns={[
          { title: t("角色"), dataIndex: "role", render: (v: string) => <Typography.Text code>{v}</Typography.Text> },
          { title: t("范围组织"), dataIndex: "scope_org_id", render: (v) => <OrgName id={v} /> },
          { title: t("到期"), dataIndex: "expires_at", render: (v: string | null) => (v ? v.slice(0, 10) : t("永久")) },
        ]}
      />
      {canManage && (
        <Form form={form} layout="inline" style={{ marginTop: 8 }}
          onFinish={async (v) => {
            const body = { role: v.role, scope_org_id: v.scope_org_id, expires_at: v.expires_at ? v.expires_at.toISOString() : undefined };
            if (await act(() => post(`/users/${userId}/roles`, body), t("已分配角色"))) { form.resetFields(); roles.reload(); }
          }}>
          <Form.Item name="role" rules={[{ required: true }]}>
            <Select placeholder={t("角色")} style={{ width: 200 }} options={roleOptions} showSearch optionFilterProp="label" />
          </Form.Item>
          <Form.Item name="scope_org_id" rules={[{ required: true }]}>
            <Select placeholder={t("范围组织")} style={{ width: 200 }} showSearch optionFilterProp="label"
              options={orgList.filter((o) => o.status === "active").map((o) => ({ value: o.id, label: `${o.code} · ${o.name}` }))} />
          </Form.Item>
          <Form.Item name="expires_at">
            <DatePicker placeholder={t("到期日（可选）")} disabledDate={(d) => d.isBefore(dayjs())} />
          </Form.Item>
          <Button htmlType="submit" type="primary">{t("分配角色")}</Button>
        </Form>
      )}
    </Space>
  );
}

function UserClearanceTab({ userId, canManage }: { userId: string; canManage: boolean }) {
  const [form] = Form.useForm();
  const act = useAction();
  return (
    <Space direction="vertical" style={{ width: "100%" }}>
      <Alert type="info" showIcon message={t("密级申请提交后由数据 Owner 审批；审批结果在「审批待办 → 密级申请」。")} style={{ marginBottom: 8 }} />
      {canManage && (
        <Form form={form} layout="vertical" style={{ maxWidth: 480 }}
          onFinish={async (v) => {
            const body = { user_id: userId, requested_level: v.requested_level, reason: v.reason, expires_at: v.expires_at ? v.expires_at.toISOString() : undefined };
            if (await act(() => post("/clearance-requests", body), t("已提交密级申请"))) form.resetFields();
          }}>
          <Form.Item name="requested_level" label={t("申请密级")} rules={[{ required: true }]}>
            <Select options={[1, 2, 3, 4].map((n) => ({ value: n, label: `L${n}` }))} />
          </Form.Item>
          <Form.Item name="reason" label={t("申请理由")} rules={[{ required: true, min: 4, message: t("至少 4 个字") }]}>
            <Input.TextArea rows={3} />
          </Form.Item>
          <Form.Item name="expires_at" label={t("到期日（可选）")}>
            <DatePicker disabledDate={(d) => d.isBefore(dayjs())} />
          </Form.Item>
          <Button htmlType="submit" type="primary">{t("提交密级申请")}</Button>
        </Form>
      )}
    </Space>
  );
}

interface ImportResult { batch_id: string; dry_run: boolean; created: number; unchanged: number; errors: { line: number; message: string }[] }

function ImportModal(p: { open: boolean; onClose: () => void; onDone: () => void }) {
  const [content, setContent] = useState("display_name,employee_no,home_org_code,email\n");
  const [result, setResult] = useState<ImportResult | null>(null);
  const act = useAction();
  const run = async (dry: boolean) => {
    const r = await act(() => post<ImportResult>("/users/import", { format: "csv", content, dry_run: dry }), dry ? undefined : t("已导入"));
    if (r) { setResult(r); if (!dry) p.onDone(); }
  };
  return (
    <Modal open={p.open} title={t("批量导入用户（CSV）")} width={720} onCancel={() => { setResult(null); p.onClose(); }}
      footer={<Space><Button onClick={() => run(true)}>{t("预览（不落库）")}</Button>
        <Button type="primary" disabled={!result?.dry_run || result.errors.length > 0} onClick={() => run(false)}>{t("确认导入")}</Button></Space>}>
      <Input.TextArea rows={8} value={content} onChange={(e) => { setContent(e.target.value); setResult(null); }}
        style={{ fontFamily: "Consolas, monospace" }} />
      {result && (
        <Alert style={{ marginTop: 12 }} type={result.errors.length ? "warning" : "success"} showIcon
          message={t("{0}：新建 {1}，已存在 {2}，错误 {3}；批次 {4}", [result.dry_run ? "预览" : "已导入", result.created, result.unchanged, result.errors.length, result.batch_id])}
          description={result.errors.map((e) => <div key={e.line}>{t("第 {0} 行：{1}", [e.line, e.message])}</div>)} />
      )}
    </Modal>
  );
}
