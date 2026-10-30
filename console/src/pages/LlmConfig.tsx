import { Alert, Button, Form, Input, InputNumber, Select, Space, Tabs, Tag, Typography } from "antd";
import { useEffect, useState } from "react";
import { get, post, put } from "../api";
import { useAuth } from "../auth";
import { OrgName, PageCard, PAGINATION, useOrgs } from "../components";
import { ListTable } from "../ListTable";
import { useAction, useLoad } from "../hooks";
import { t } from "../i18n";

interface Allowlist {
  company_id: string;
  names: string[];
}

interface Budget {
  company_id: string;
  daily_tokens: number;
}

interface Usage {
  company_id: string;
  usage_date: string;
  tokens_used: number;
  daily_tokens: number;
}

interface Provider {
  base_url: string;
  credential_ref: string;
  models_path: string;
  has_api_key: boolean;
  api_key_hint: string | null;
  synced_at: string | null;
  synced_count: number;
}

interface UpstreamModel {
  id: string;
  owned_by?: string | null;
  suggested_profile: string;
}

interface ModelProfileRow {
  asset_id: string;
  name: string;
  version: string | null;
  model: string | null;
  base_url: string | null;
  status: string | null;
}

export function LlmConfigPage() {
  const { has } = useAuth();
  const canEdit = has("platform_admin");

  if (!canEdit) {
    return (
      <PageCard>
        <Alert type="warning" showIcon message={t("只有平台管理员可以配置公司 LLM 白名单与预算")} />
      </PageCard>
    );
  }

  return (
    <PageCard>
      <Typography.Paragraph type="secondary" style={{ marginTop: 0 }}>
        {t("配置 OpenAI 兼容提供方后，可同步 /models、登记 model_profile，并按公司设置白名单与日预算。AI 辅助生成描述、选择评测、Skill 指导评测均经 LLM Gateway。")}
      </Typography.Paragraph>
      <Tabs
        items={[
          { key: "provider", label: t("提供方"), children: <ProviderPanel /> },
          { key: "profiles", label: t("模型档案"), children: <ProfilesPanel /> },
          { key: "company", label: t("公司策略"), children: <CompanyStrategyPanel /> },
        ]}
      />
    </PageCard>
  );
}

function ProviderPanel() {
  const act = useAction();
  const provider = useLoad(() => get<Provider>("/llm/provider"), []);
  const synced = useLoad(() => get<{ items: UpstreamModel[] }>("/llm/provider/synced-models"), []);
  const [form] = Form.useForm();

  useEffect(() => {
    if (provider.data) {
      form.setFieldsValue({
        base_url: provider.data.base_url || "https://ws-85igwljfmr2dmaqu.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1",
        models_path: provider.data.models_path || "/models",
        api_key: "",
      });
    }
  }, [provider.data, form]);

  return (
    <>
      {provider.error && <Alert type="error" message={provider.error} style={{ marginBottom: 12 }} />}
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message={t("密钥写入 OpenBao，不回显明文；留空表示不修改已保存密钥。也可改用环境变量 AIP_LLM_API_KEY。")}
      />
      <Form
        form={form}
        layout="vertical"
        onFinish={async (v) => {
          const body: { base_url: string; models_path: string; api_key?: string } = {
            base_url: String(v.base_url).trim(),
            models_path: String(v.models_path || "/models").trim() || "/models",
          };
          const key = String(v.api_key || "").trim();
          if (key) body.api_key = key;
          const ok = await act(() => put("/llm/provider", body), t("已保存提供方"));
          if (ok) {
            form.setFieldValue("api_key", "");
            provider.reload();
          }
        }}
      >
        <Form.Item
          name="base_url"
          label={t("接口根地址（OpenAI 兼容）")}
          rules={[{ required: true }]}
          extra={t("示例：…/compatible-mode/v1；同步时请求 {根地址}/models")}
        >
          <Input placeholder="https://…/compatible-mode/v1" />
        </Form.Item>
        <Form.Item name="models_path" label={t("模型列表路径")} initialValue="/models">
          <Input placeholder="/models" />
        </Form.Item>
        <Form.Item
          name="api_key"
          label={t("API Key")}
          extra={
            provider.data?.has_api_key
              ? t("已配置密钥（{0}）", [provider.data.api_key_hint || "***"])
              : t("尚未配置密钥")
          }
        >
          <Input.Password placeholder={t("粘贴密钥，仅本次保存写入")} autoComplete="new-password" />
        </Form.Item>
        <Space wrap>
          <Button type="primary" htmlType="submit" loading={provider.loading}>
            {t("保存提供方")}
          </Button>
          <Button
            onClick={async () => {
              const ok = await act(() => post("/llm/provider/sync-models", {}), t("已同步模型列表"));
              if (ok) {
                provider.reload();
                synced.reload();
              }
            }}
          >
            {t("同步模型")}
          </Button>
        </Space>
      </Form>
      {provider.data?.synced_at ? (
        <Typography.Paragraph type="secondary" style={{ marginTop: 16 }}>
          {t("最近同步：{0}，共 {1} 个模型", [provider.data.synced_at, provider.data.synced_count])}
        </Typography.Paragraph>
      ) : null}
      <Typography.Title level={5} style={{ marginTop: 8 }}>
        {t("上游模型")}
      </Typography.Title>
      <ListTable
        size="small"
        rowKey="id"
        loading={synced.loading}
        dataSource={synced.data?.items ?? []}
        pagination={PAGINATION}
        locale={{ emptyText: t("尚未同步：保存提供方后点击「同步模型」") }}
        columns={[
          { title: t("模型"), dataIndex: "id", ellipsis: true },
          { title: t("建议档案名"), dataIndex: "suggested_profile", width: 220 },
          { title: t("提供方"), dataIndex: "owned_by", width: 140, render: (v: string | null) => v || "—" },
        ]}
      />
    </>
  );
}

function ProfilesPanel() {
  const { companies } = useOrgs();
  const act = useAction();
  const profiles = useLoad(() => get<{ items: ModelProfileRow[] }>("/llm/model-profiles"), []);
  const synced = useLoad(() => get<{ items: UpstreamModel[] }>("/llm/provider/synced-models"), []);
  const [form] = Form.useForm();

  useEffect(() => {
    if (companies.length && !form.getFieldValue("owner_org_id")) {
      form.setFieldsValue({ owner_org_id: companies[0].id, version: "1.0.0" });
    }
  }, [companies, form]);

  return (
    <>
      {profiles.error && <Alert type="error" message={profiles.error} style={{ marginBottom: 12 }} />}
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message={t("从同步列表选模型创建 model_profile 草稿；白名单填档案名（不含 @版本）。选择评测请写 name@semver。")}
      />
      <Form
        form={form}
        layout="vertical"
        style={{ maxWidth: 640 }}
        onFinish={async (v) => {
          const body = {
            model_id: String(v.model_id).trim(),
            name: v.name ? String(v.name).trim() : undefined,
            version: String(v.version || "1.0.0").trim(),
            owner_org_id: v.owner_org_id,
            temperature: Number(v.temperature ?? 0),
            max_tokens: Number(v.max_tokens ?? 1024),
            add_to_allowlist_company_id: v.add_to_allowlist_company_id || undefined,
          };
          const ok = await act(() => post("/llm/model-profiles", body), t("已创建模型档案"));
          if (ok) {
            profiles.reload();
            form.setFieldValue("name", undefined);
          }
        }}
      >
        <Form.Item name="model_id" label={t("上游模型 id")} rules={[{ required: true }]}>
          <Select
            showSearch
            allowClear
            placeholder={t("从同步列表选择，或手动输入")}
            options={(synced.data?.items ?? []).map((m) => ({
              value: m.id,
              label: m.id,
            }))}
            onChange={(id) => {
              const hit = (synced.data?.items ?? []).find((m) => m.id === id);
              if (hit) form.setFieldValue("name", hit.suggested_profile);
            }}
          />
        </Form.Item>
        <Form.Item name="name" label={t("档案名（资产名）")} extra={t("留空则按模型 id 自动生成")}>
          <Input placeholder="deepseek_v4_1_flash" />
        </Form.Item>
        <Space wrap size="large">
          <Form.Item name="version" label={t("版本")} initialValue="1.0.0" rules={[{ required: true }]}>
            <Input style={{ width: 120 }} />
          </Form.Item>
          <Form.Item name="temperature" label={t("温度")} initialValue={0}>
            <InputNumber min={0} max={2} step={0.1} style={{ width: 100 }} />
          </Form.Item>
          <Form.Item name="max_tokens" label={t("max_tokens")} initialValue={1024}>
            <InputNumber min={1} max={128000} style={{ width: 120 }} />
          </Form.Item>
        </Space>
        <Form.Item name="owner_org_id" label={t("归属组织")} rules={[{ required: true }]}>
          <Select
            style={{ minWidth: 280 }}
            options={companies.map((c) => ({ value: c.id, label: `${c.code} · ${c.name}` }))}
          />
        </Form.Item>
        <Form.Item name="add_to_allowlist_company_id" label={t("同时加入公司白名单（可选）")}>
          <Select
            allowClear
            style={{ minWidth: 280 }}
            options={companies.map((c) => ({ value: c.id, label: `${c.code} · ${c.name}` }))}
            placeholder={t("不加入")}
          />
        </Form.Item>
        <Button type="primary" htmlType="submit">
          {t("创建模型档案")}
        </Button>
      </Form>
      <Typography.Title level={5} style={{ marginTop: 24 }}>
        {t("已登记档案")}
      </Typography.Title>
      <ListTable
        size="small"
        rowKey="asset_id"
        loading={profiles.loading}
        dataSource={profiles.data?.items ?? []}
        pagination={PAGINATION}
        locale={{ emptyText: t("尚未创建 model_profile") }}
        columns={[
          {
            title: t("档案引用"),
            key: "ref",
            render: (_: unknown, r: ModelProfileRow) =>
              r.version ? `${r.name}@${r.version}` : r.name,
          },
          { title: t("上游模型"), dataIndex: "model", ellipsis: true },
          { title: t("状态"), dataIndex: "status", width: 100, render: (v: string | null) => (v ? <Tag>{v}</Tag> : "—") },
          { title: t("接口根"), dataIndex: "base_url", ellipsis: true },
        ]}
      />
    </>
  );
}

function CompanyStrategyPanel() {
  const { companies } = useOrgs();
  const [companyId, setCompanyId] = useState<string | undefined>();

  useEffect(() => {
    if (!companyId && companies.length) setCompanyId(companies[0].id);
  }, [companies, companyId]);

  return (
    <>
      <Space style={{ marginBottom: 16 }} wrap>
        <Typography.Text>{t("公司")}</Typography.Text>
        <Select
          style={{ minWidth: 240 }}
          value={companyId}
          onChange={setCompanyId}
          options={companies.map((c) => ({ value: c.id, label: `${c.code} · ${c.name}` }))}
          placeholder={t("选择公司")}
        />
        {companyId && <OrgName id={companyId} />}
      </Space>
      {!companyId ? (
        <Alert type="info" message={t("请先选择公司")} />
      ) : (
        <CompanyLlmPanel key={companyId} companyId={companyId} />
      )}
    </>
  );
}

function CompanyLlmPanel({ companyId }: { companyId: string }) {
  const allow = useLoad(() => get<Allowlist>(`/companies/${companyId}/llm-allowlist`), [companyId]);
  const budget = useLoad(() => get<Budget>(`/companies/${companyId}/llm-budget`), [companyId]);
  const usage = useLoad(() => get<Usage>(`/companies/${companyId}/llm-usage`), [companyId]);
  const act = useAction();
  const [form] = Form.useForm();

  useEffect(() => {
    if (allow.data && budget.data) {
      form.setFieldsValue({
        names: (allow.data.names ?? []).join("\n"),
        daily_tokens: budget.data.daily_tokens ?? 0,
      });
    }
  }, [allow.data, budget.data, form]);

  const err = allow.error || budget.error || usage.error;
  return (
    <>
      {err && <Alert type="error" message={err} style={{ marginBottom: 12 }} />}
      {usage.data && (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          message={t("当日用量：{0} / {1} token（{2}）", [
            usage.data.tokens_used,
            usage.data.daily_tokens,
            usage.data.usage_date,
          ])}
        />
      )}
      <Form
        form={form}
        layout="vertical"
        onFinish={async (v) => {
          const names = String(v.names ?? "")
            .split("\n")
            .map((s: string) => s.trim())
            .filter(Boolean);
          const ok1 = await act(() => put(`/companies/${companyId}/llm-allowlist`, { names }), t("已保存白名单"));
          const ok2 = await act(
            () => put(`/companies/${companyId}/llm-budget`, { daily_tokens: Number(v.daily_tokens) }),
            t("已保存预算"),
          );
          if (ok1 || ok2) {
            allow.reload();
            budget.reload();
            usage.reload();
          }
        }}
      >
        <Form.Item
          name="names"
          label={t("模型白名单（每行一个 name，对应 model_profile 资产名）")}
          extra={t("空列表表示该公司禁止调用 Gateway")}
        >
          <Input.TextArea rows={6} placeholder={"gpt-4o-mini\ninternal-qwen"} />
        </Form.Item>
        <Form.Item name="daily_tokens" label={t("日 token 预算")} rules={[{ required: true }]}>
          <InputNumber min={0} style={{ width: 240 }} />
        </Form.Item>
        <Button type="primary" htmlType="submit" loading={allow.loading || budget.loading}>
          {t("保存")}
        </Button>
      </Form>
      <Typography.Title level={5} style={{ marginTop: 24 }}>
        {t("当前白名单")}
      </Typography.Title>
      <ListTable
        size="small"
        rowKey="name"
        pagination={false}
        loading={allow.loading}
        dataSource={(allow.data?.names ?? []).map((name) => ({ name }))}
        locale={{ emptyText: t("尚未配置模型") }}
        columns={[{ title: t("模型名"), dataIndex: "name" }]}
      />
    </>
  );
}
