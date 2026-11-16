import { Alert, Button, Form, Input, InputNumber, Space, Switch, Typography } from "antd";
import { useEffect } from "react";
import { get, post, put } from "../api";
import { useAuth } from "../auth";
import { PageCard } from "../components";
import { useAction, useLoad } from "../hooks";
import { t } from "../i18n";

interface MailSettings {
  enabled: boolean;
  host: string;
  port: number;
  user: string;
  mail_from: string;
  starttls: boolean;
  subject_template: string;
  public_base_url: string;
  credential_ref: string;
  has_password: boolean;
}

export function MailConfigPage() {
  const { has } = useAuth();
  const canEdit = has("platform_admin");
  const settings = useLoad(() => get<MailSettings>("/notify/mail-settings"), []);
  const act = useAction();
  const [form] = Form.useForm();
  const [testForm] = Form.useForm();

  useEffect(() => {
    if (!settings.data) return;
    form.setFieldsValue({
      ...settings.data,
      password: "",
      public_base_url: settings.data.public_base_url || window.location.origin,
    });
  }, [settings.data, form]);

  if (!canEdit) {
    return (
      <PageCard>
        <Alert type="warning" showIcon message={t("只有平台管理员可以配置邮件发送")} />
      </PageCard>
    );
  }

  return (
    <PageCard>
      <Typography.Paragraph type="secondary" style={{ marginTop: 0 }}>
        {t("配置 SMTP 后，审批待办会邮件通知可处理角色，并抄送 data_owner；正文含一键打开链接。密码写入保险箱，页面不回显明文。")}
      </Typography.Paragraph>
      {settings.error && <Alert type="error" message={settings.error} style={{ marginBottom: 12 }} />}
      <Form
        form={form}
        layout="vertical"
        style={{ maxWidth: 560 }}
        onFinish={async (v) => {
          const body = {
            enabled: Boolean(v.enabled),
            host: v.host || "",
            port: v.port || 587,
            user: v.user || "",
            mail_from: v.mail_from || v.user || "",
            starttls: v.starttls !== false,
            subject_template: v.subject_template || "审批待办：{kind}",
            public_base_url: (v.public_base_url || window.location.origin).replace(/\/$/, ""),
            password: v.password?.trim() ? v.password : undefined,
          };
          const r = await act(() => put<MailSettings>("/notify/mail-settings", body), t("已保存邮件配置"));
          if (r) {
            form.setFieldsValue({ password: "" });
            settings.reload();
          }
        }}
      >
        <Form.Item name="enabled" label={t("启用控制台 SMTP")} valuePropName="checked">
          <Switch />
        </Form.Item>
        <Form.Item noStyle shouldUpdate={(prev, cur) => prev.enabled !== cur.enabled}>
          {() => (
            <Form.Item
              name="host"
              label={t("SMTP 主机")}
              rules={form.getFieldValue("enabled") ? [{ required: true, message: t("请填写 SMTP 主机") }] : []}
            >
              <Input placeholder="smtp.example.com" autoComplete="off" />
            </Form.Item>
          )}
        </Form.Item>
        <Form.Item name="port" label={t("端口")} rules={[{ required: true }]}>
          <InputNumber min={1} max={65535} style={{ width: "100%" }} />
        </Form.Item>
        <Form.Item name="user" label={t("发件账号")}>
          <Input autoComplete="off" />
        </Form.Item>
        <Form.Item name="mail_from" label={t("发件人地址")}>
          <Input placeholder="noreply@example.com" autoComplete="off" />
        </Form.Item>
        <Form.Item
          name="password"
          label={settings.data?.has_password ? t("SMTP 密码（留空则不修改）") : t("SMTP 密码")}
          rules={settings.data?.has_password ? [] : [{ required: true, message: t("请填写 SMTP 密码") }]}
        >
          <Input.Password autoComplete="new-password" />
        </Form.Item>
        <Form.Item name="starttls" label="STARTTLS" valuePropName="checked">
          <Switch />
        </Form.Item>
        <Form.Item
          name="subject_template"
          label={t("邮件标题模板")}
          extra={t("可用占位符 kind（如 transport_exception / G5 / G6）")}
        >
          <Input placeholder="审批待办：{kind}" />
        </Form.Item>
        <Form.Item
          name="public_base_url"
          label={t("控制台根地址")}
          extra={t("保存时默认取当前浏览器域名，用于拼审批链接；反向代理不一致时可改。")}
        >
          <Input placeholder="https://aip.example.com" />
        </Form.Item>
        {settings.data?.credential_ref && (
          <Typography.Paragraph type="secondary">
            {t("凭据引用")}：<Typography.Text code>{settings.data.credential_ref}</Typography.Text>
            {settings.data.has_password ? ` · ${t("已配置密码")}` : ""}
          </Typography.Paragraph>
        )}
        <Space>
          <Button type="primary" htmlType="submit">{t("保存")}</Button>
          <Button onClick={() => settings.reload()}>{t("重新加载")}</Button>
        </Space>
      </Form>

      <Typography.Title level={5} style={{ marginTop: 28 }}>{t("测试发送")}</Typography.Title>
      <Form
        form={testForm}
        layout="inline"
        onFinish={async (v) => {
          await act(() => post("/notify/mail-settings/test", { to: v.to }), t("测试邮件已发送"));
        }}
      >
        <Form.Item name="to" rules={[{ required: true, type: "email", message: t("请填写有效邮箱") }]}>
          <Input style={{ width: 280 }} placeholder="you@example.com" />
        </Form.Item>
        <Button htmlType="submit">{t("发送测试邮件")}</Button>
      </Form>
    </PageCard>
  );
}
