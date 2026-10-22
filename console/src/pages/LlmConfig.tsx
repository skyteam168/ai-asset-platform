import { Alert, Button, Form, Input, InputNumber, Select, Space, Typography } from "antd";
import { useEffect, useState } from "react";
import { get, put } from "../api";
import { useAuth } from "../auth";
import { OrgName, PageCard, useOrgs } from "../components";
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

export function LlmConfigPage() {
  const { has } = useAuth();
  const { companies } = useOrgs();
  const [companyId, setCompanyId] = useState<string | undefined>();
  const canEdit = has("platform_admin");

  useEffect(() => {
    if (!companyId && companies.length) setCompanyId(companies[0].id);
  }, [companies, companyId]);

  if (!canEdit) {
    return (
      <PageCard>
        <Alert type="warning" showIcon message={t("只有平台管理员可以配置公司 LLM 白名单与预算")} />
      </PageCard>
    );
  }

  return (
    <PageCard>
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
    </PageCard>
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
