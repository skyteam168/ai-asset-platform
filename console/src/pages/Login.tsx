import { BgColorsOutlined, GlobalOutlined } from "@ant-design/icons";
import { Alert, Button, Card, Dropdown, Form, Input, Popover, Typography, theme } from "antd";
import { useState } from "react";
import { describeError } from "../api";
import { useAuth } from "../auth";
import { Logo } from "../brand";
import { LANGS, lang, switchLang, t, type Lang } from "../i18n";
import { ThemePanel } from "../theme";

export function LoginPage() {
  const { login } = useAuth();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const { token } = theme.useToken();

  const submit = async (v: { login_name: string; password: string; totp: string }) => {
    setBusy(true);
    setError(null);
    try {
      await login(v.login_name, v.password, v.totp);
    } catch (e) {
      setError(describeError(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{ minHeight: "100vh", display: "flex", background: token.colorBgLayout }}>
      <div
        style={{
          flex: "1 1 46%",
          background: "linear-gradient(150deg, #0f2747 0%, #1f5fa8 100%)",
          color: "#fff",
          display: "flex",
          flexDirection: "column",
          justifyContent: "center",
          padding: "48px 56px",
          minWidth: 0,
        }}
        className="login-brand"
      >
        <Logo on="dark" height={44} />
        <Typography.Title level={2} style={{ color: "#fff", marginTop: 28, marginBottom: 8 }}>
          {t("AI数字资产管理中台")}
        </Typography.Title>
        <Typography.Paragraph style={{ color: "rgba(255,255,255,0.8)", maxWidth: 440, fontSize: 15 }}>
          {t("统一登记、授权与审计集团的 AI 工具、工作流与连接，让各分公司的 Agent 安全地使用旧系统数据。")}
        </Typography.Paragraph>
      </div>
      <div style={{ flex: "1 1 54%", display: "flex", flexDirection: "column", minWidth: 0 }}>
        <div style={{ display: "flex", justifyContent: "flex-end", gap: 4, padding: 16 }}>
          <Dropdown
            menu={{
              selectedKeys: [lang()],
              items: LANGS.map((l) => ({ key: l.value, label: l.label })),
              onClick: (e) => e.key !== lang() && switchLang(e.key as Lang),
            }}
          >
            <Button type="text" icon={<GlobalOutlined />}>{LANGS.find((l) => l.value === lang())?.label}</Button>
          </Dropdown>
          <Popover trigger="click" placement="bottomRight" title={t("主题")} content={<ThemePanel />}>
            <Button type="text" icon={<BgColorsOutlined />} aria-label={t("主题")} />
          </Popover>
        </div>
        <div style={{ flex: 1, display: "grid", placeItems: "center", padding: 24 }}>
          <Card title={t("控制台登录")} style={{ width: "100%", maxWidth: 380 }}>
            {error && <Alert type="error" message={error} showIcon style={{ marginBottom: 16 }} />}
            <Form layout="vertical" onFinish={submit} requiredMark={false}>
              <Form.Item name="login_name" label={t("用户名")} rules={[{ required: true, message: t("请输入用户名") }]}>
                <Input autoComplete="username" autoFocus />
              </Form.Item>
              <Form.Item name="password" label={t("口令")} rules={[{ required: true, message: t("请输入口令") }]}>
                <Input.Password autoComplete="current-password" />
              </Form.Item>
              <Form.Item
                name="totp"
                label={t("动态码")}
                rules={[{ required: true, pattern: /^\d{6}$/, message: t("请输入验证器中的 6 位动态码") }]}
              >
                <Input inputMode="numeric" maxLength={6} autoComplete="one-time-code" />
              </Form.Item>
              <Button type="primary" htmlType="submit" block loading={busy}>
                {t("登录")}
              </Button>
            </Form>
          </Card>
        </div>
      </div>
    </div>
  );
}
