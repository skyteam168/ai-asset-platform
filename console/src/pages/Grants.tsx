import { Alert, Button, DatePicker, Form, Input, InputNumber, Modal, Select, Space, Table, Tabs, Typography } from "antd";
import dayjs from "dayjs";
import { useState } from "react";
import { get, post, type Page } from "../api";
import { useAuth } from "../auth";
import { Clearance, OrgName, StateTag, useOrgs } from "../components";
import { useAction, useLoad } from "../hooks";
import type { Asset, Ceiling, Consumer, Grant } from "../types";

interface UsageRequest { id: string; company_id: string; consumer_id: string; asset_id: string; env: string; reason: string; status: string }

const ENVS = ["test", "staging", "prod"].map((e) => ({ value: e, label: e }));

export function GrantsPage() {
  const { has } = useAuth();
  const assets = useLoad(() => get<Page<Asset>>("/assets"), []);
  const consumers = useLoad(() => get<Page<Consumer>>("/consumers"), []);
  const assetName = (id: string) => assets.data?.items.find((a) => a.id === id)?.name ?? id.slice(0, 8);
  const consumerName = (id: string) => consumers.data?.items.find((c) => c.id === id)?.client_id ?? id.slice(0, 8);
  const lookups = { assets: assets.data?.items ?? [], consumers: consumers.data?.items ?? [], assetName, consumerName };
  return (
    <>
      <Typography.Title level={4}>授权</Typography.Title>
      <Tabs items={[
        { key: "ceilings", label: "授权上限（集团 → 分公司）", children: <Ceilings {...lookups} admin={has("platform_admin")} /> },
        { key: "grants", label: "授权", children: <Grants {...lookups} /> },
        { key: "usage", label: "使用申请", children: <Usage {...lookups} /> },
        { key: "review", label: "访问复核", children: <Review /> },
      ]} />
    </>
  );
}

interface Lookups { assets: Asset[]; consumers: Consumer[]; assetName: (id: string) => string; consumerName: (id: string) => string }

function Ceilings(p: Lookups & { admin: boolean }) {
  const list = useLoad(() => get<Page<Ceiling>>("/grant-ceilings"), []);
  const { companies } = useOrgs();
  const [form] = Form.useForm();
  const act = useAction();
  return (
    <>
      {p.admin && (
        <Form form={form} layout="inline" style={{ marginBottom: 12 }} initialValues={{ max_classification: 2 }}
          onFinish={async (v) => {
            const body = { company_id: v.company_id, asset_set: { asset_ids: v.asset_ids }, max_classification: v.max_classification };
            if (await act(() => post("/grant-ceilings", body), "已下发")) { form.resetFields(); list.reload(); }
          }}>
          <Form.Item name="company_id" rules={[{ required: true, message: "必选" }]}>
            <Select placeholder="分公司" style={{ width: 160 }} options={companies.map((c) => ({ value: c.id, label: c.code }))} />
          </Form.Item>
          <Form.Item name="asset_ids" rules={[{ required: true, message: "必选" }]}>
            <Select mode="multiple" placeholder="资产集合" style={{ width: 320 }} options={p.assets.map((a) => ({ value: a.id, label: a.name }))} />
          </Form.Item>
          <Form.Item name="max_classification" label="密级上限"><InputNumber min={1} max={4} /></Form.Item>
          <Button htmlType="submit" type="primary">下发上限</Button>
        </Form>
      )}
      {list.error && <Alert type="error" message={list.error} />}
      <Table<Ceiling> rowKey="id" size="small" loading={list.loading} dataSource={list.data?.items ?? []}
        columns={[
          { title: "分公司", dataIndex: "company_id", render: (v) => <OrgName id={v} /> },
          { title: "资产", dataIndex: "asset_ids", render: (ids: string[]) => ids.map(p.assetName).join("，") },
          { title: "密级上限", dataIndex: "max_classification", render: (v) => <Clearance level={v} /> },
          { title: "状态", dataIndex: "status", render: (v) => <StateTag state={v} /> },
        ]} />
    </>
  );
}

function Grants(p: Lookups) {
  const list = useLoad(() => get<Page<Grant>>("/grants"), []);
  const ceilings = useLoad(() => get<Page<Ceiling>>("/grant-ceilings"), []);
  const { has } = useAuth();
  const [open, setOpen] = useState(false);
  const [form] = Form.useForm();
  const act = useAction();
  const assetId = Form.useWatch("asset_id", form);
  const usable = (ceilings.data?.items ?? []).filter((c) => c.status === "active" && (!assetId || c.asset_ids.includes(assetId)));
  return (
    <>
      {has("platform_admin", "company_admin") && <Button type="primary" style={{ marginBottom: 12 }} onClick={() => setOpen(true)}>新建授权</Button>}
      {list.error && <Alert type="error" message={list.error} />}
      <Table<Grant> rowKey="id" size="small" loading={list.loading} dataSource={list.data?.items ?? []}
        columns={[
          { title: "资产", dataIndex: "asset_id", render: p.assetName },
          { title: "被授权方", key: "grantee", render: (_, g) => `${g.grantee_type}：${g.grantee_type === "consumer" ? p.consumerName(g.grantee_id) : g.grantee_id.slice(0, 8)}` },
          { title: "环境", dataIndex: "env" },
          { title: "到期", dataIndex: "expires_at", render: (v: string) => v.slice(0, 10) },
          { title: "状态", dataIndex: "status", render: (v) => <StateTag state={v} /> },
        ]} />
      <Modal open={open} title="新建授权" onCancel={() => setOpen(false)} onOk={() => form.submit()} destroyOnClose>
        <Form form={form} layout="vertical" preserve={false}
          initialValues={{ env: "test", grantee_type: "consumer", expires_at: dayjs().add(180, "day") }}
          onFinish={async (v) => {
            const body = { ...v, expires_at: v.expires_at.toISOString() };
            if (await act(() => post("/grants", body), "已授权")) { setOpen(false); list.reload(); }
          }}>
          <Form.Item name="asset_id" label="资产" rules={[{ required: true }]}>
            <Select options={p.assets.map((a) => ({ value: a.id, label: a.name }))} />
          </Form.Item>
          <Form.Item name="ceiling_id" label="依据的授权上限" rules={[{ required: true }]}
            extra="分公司授权不能超出集团下发的上限；没有可选项时请先由平台管理员下发上限。">
            <Select options={usable.map((c) => ({ value: c.id, label: `${c.id.slice(0, 8)} · L${c.max_classification}` }))} />
          </Form.Item>
          <Space style={{ display: "flex" }} align="start">
            <Form.Item name="grantee_type" label="被授权方类型">
              <Select style={{ width: 150 }} options={["consumer", "org_unit", "user", "agent_profile"].map((x) => ({ value: x, label: x }))} />
            </Form.Item>
            <Form.Item noStyle shouldUpdate={(a, b) => a.grantee_type !== b.grantee_type}>
              {({ getFieldValue }) => (
                <Form.Item name="grantee_id" label="被授权方" rules={[{ required: true }]} style={{ width: 280 }}>
                  {getFieldValue("grantee_type") === "consumer"
                    ? <Select options={p.consumers.map((c) => ({ value: c.id, label: c.client_id }))} />
                    : <Input placeholder="UUID" />}
                </Form.Item>
              )}
            </Form.Item>
          </Space>
          <Space>
            <Form.Item name="env" label="环境"><Select style={{ width: 120 }} options={ENVS} /></Form.Item>
            <Form.Item name="expires_at" label="到期日" rules={[{ required: true }]}>
              <DatePicker disabledDate={(d) => d.isBefore(dayjs())} />
            </Form.Item>
          </Space>
        </Form>
      </Modal>
    </>
  );
}

function Usage(p: Lookups) {
  const list = useLoad(() => get<Page<UsageRequest>>("/usage-requests"), []);
  const [form] = Form.useForm();
  const act = useAction();
  return (
    <>
      <Form form={form} layout="inline" style={{ marginBottom: 12 }} initialValues={{ env: "test" }}
        onFinish={async (v) => {
          if (await act(() => post("/usage-requests", v), "已提交，等待数据 Owner 审批")) { form.resetFields(); list.reload(); }
        }}>
        <Form.Item name="consumer_id" rules={[{ required: true, message: "必选" }]}>
          <Select placeholder="消费方" style={{ width: 180 }} options={p.consumers.map((c) => ({ value: c.id, label: c.client_id }))} />
        </Form.Item>
        <Form.Item name="asset_id" rules={[{ required: true, message: "必选" }]}>
          <Select placeholder="资产" style={{ width: 220 }} options={p.assets.map((a) => ({ value: a.id, label: a.name }))} />
        </Form.Item>
        <Form.Item name="env"><Select style={{ width: 100 }} options={ENVS} /></Form.Item>
        <Form.Item name="reason" rules={[{ required: true, min: 4, message: "至少 4 个字" }]}><Input placeholder="用途说明" /></Form.Item>
        <Button htmlType="submit" type="primary">提交申请</Button>
      </Form>
      {list.error && <Alert type="error" message={list.error} />}
      <Table<UsageRequest> rowKey="id" size="small" loading={list.loading} dataSource={list.data?.items ?? []}
        columns={[
          { title: "消费方", dataIndex: "consumer_id", render: p.consumerName },
          { title: "资产", dataIndex: "asset_id", render: p.assetName },
          { title: "环境", dataIndex: "env" },
          { title: "用途", dataIndex: "reason" },
          { title: "公司", dataIndex: "company_id", render: (v) => <OrgName id={v} /> },
          { title: "状态", dataIndex: "status", render: (v) => <StateTag state={v} /> },
        ]} />
    </>
  );
}

function Review() {
  const { companies } = useOrgs();
  const [form] = Form.useForm();
  const act = useAction();
  const [campaign, setCampaign] = useState<string | null>(null);
  const [revoked, setRevoked] = useState<number | null>(null);
  return (
    <Space direction="vertical" style={{ width: "100%" }}>
      <Alert type="info" showIcon message="复核活动把即将到期与长期未使用的授权列为待复核项，分发给数据 Owner 确认；关闭活动时未确认的授权自动回收。" />
      <Form form={form} layout="inline" initialValues={{ expiring_days: 30, unused_days: 90 }}
        onFinish={async (v) => {
          const r = await act(() => post<{ id: string }>("/access-reviews", v), "已发起");
          if (r) { setCampaign(r.id); setRevoked(null); }
        }}>
        <Form.Item name="company_id" rules={[{ required: true, message: "必选" }]}>
          <Select placeholder="分公司" style={{ width: 160 }} options={companies.map((c) => ({ value: c.id, label: c.code }))} />
        </Form.Item>
        <Form.Item name="expiring_days" label="到期天数内"><InputNumber min={1} /></Form.Item>
        <Form.Item name="unused_days" label="未使用天数"><InputNumber min={1} /></Form.Item>
        <Button htmlType="submit" type="primary">发起复核</Button>
      </Form>
      <Space>
        <Input placeholder="复核活动 ID" style={{ width: 340 }} value={campaign ?? ""} onChange={(e) => setCampaign(e.target.value || null)} />
        <Button danger disabled={!campaign} onClick={() => Modal.confirm({
          title: "关闭复核活动？", content: "未被确认的授权会被回收，回收经快照推送到数据面。",
          onOk: async () => {
            const r = await act(() => post<{ revoked: number }>(`/access-reviews/${campaign}/close`));
            if (r) setRevoked(r.revoked);
          },
        })}>关闭并回收</Button>
      </Space>
      {revoked !== null && <Alert type="success" showIcon message={`已关闭，回收授权 ${revoked} 条`} />}
    </Space>
  );
}
