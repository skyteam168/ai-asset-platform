import {
  ApiOutlined,
  BgColorsOutlined,
  LockOutlined,
  SafetyCertificateOutlined,
  SafetyOutlined,
  UserOutlined,
} from "@ant-design/icons";
import { Alert, Button, Dropdown, Form, Input, Popover, Typography, theme } from "antd";
import { useState } from "react";
import { describeError } from "../api";
import { useAuth } from "../auth";
import { Logo, PRODUCT_NAME } from "../brand";
import { LANGS, lang, switchLang, t, type Lang } from "../i18n";
import { ThemePanel, useThemePrefs } from "../theme";

const FEATURES = [
  { icon: <ApiOutlined />, title: t("统一接入"), text: t("旧系统接口登记为标准工具，经 MCP 与 REST 提供给各分公司的 Agent。") },
  { icon: <SafetyCertificateOutlined />, title: t("授权与门禁"), text: t("五方授权交集、质量门禁与审批，上架即受控。") },
  { icon: <SafetyOutlined />, title: t("可审计"), text: t("每次调用进入防篡改审计链，风险规则自动熔断。") },
];

export function LoginPage() {
  const { login } = useAuth();
  const prefs = useThemePrefs();
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
      <div className="login-brand" style={{
        flex: "0 0 44%",
        position: "relative",
        overflow: "hidden",
        color: "#fff",
        padding: "48px 64px",
        display: "flex",
        flexDirection: "column",
        background: `radial-gradient(1200px 600px at 10% 0%, color-mix(in srgb, ${prefs.primary} 75%, #000) 0%, #0f1d3d 60%, #0b1430 100%)`,
      }}>
        <div><Logo on="dark" height={28} /></div>
        <div style={{ flex: 1, display: "flex", flexDirection: "column", justifyContent: "center", maxWidth: 480 }}>
          <Typography.Title level={1} style={{ color: "#fff", margin: 0, fontSize: 34, lineHeight: 1.3 }}>
            {t("AI数字资产管理中台")}
          </Typography.Title>
          <Typography.Paragraph style={{ color: "rgba(255,255,255,0.72)", fontSize: 15, margin: "14px 0 40px" }}>
            {t("统一登记、授权与审计集团的 AI 工具、工作流与连接，让各分公司的 Agent 安全地使用旧系统数据。")}
          </Typography.Paragraph>
          {FEATURES.map((f) => (
            <div key={f.title} style={{ display: "flex", gap: 14, marginBottom: 22 }}>
              <span style={{
                width: 36, height: 36, flex: "none", borderRadius: 10, display: "grid", placeItems: "center", fontSize: 17,
                background: "rgba(255,255,255,0.1)", border: "1px solid rgba(255,255,255,0.14)",
              }}>{f.icon}</span>
              <div>
                <div style={{ fontWeight: 600, fontSize: 15 }}>{f.title}</div>
                <div style={{ color: "rgba(255,255,255,0.66)", fontSize: 13, marginTop: 2 }}>{f.text}</div>
              </div>
            </div>
          ))}
        </div>
        <Typography.Text style={{ color: "rgba(255,255,255,0.45)", fontSize: 12 }}>
          © {new Date().getFullYear()} {t("申洲集团")}
        </Typography.Text>
      </div>
      <div style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0 }}>
        <div style={{ display: "flex", justifyContent: "flex-end", gap: 4, padding: 16 }}>
          <Dropdown
            menu={{
              selectedKeys: [lang()],
              items: LANGS.map((l) => ({ key: l.value, label: l.label })),
              onClick: (e) => e.key !== lang() && switchLang(e.key as Lang),
            }}
          >
            <Button type="text">{LANGS.find((l) => l.value === lang())?.label}</Button>
          </Dropdown>
          <Popover trigger="click" placement="bottomRight" title={t("主题")} content={<ThemePanel />}>
            <Button type="text" icon={<BgColorsOutlined />} aria-label={t("主题")} />
          </Popover>
        </div>
        <div style={{ flex: 1, display: "grid", placeItems: "center", padding: "24px 24px 80px" }}>
          <div style={{ width: "100%", maxWidth: 380 }}>
            <div className="login-mobile-logo" style={{ marginBottom: 28 }}>
              <Logo on={prefs.mode} height={24} />
            </div>
            <Typography.Title level={3} style={{ margin: 0 }}>{t("登录 {0}", [PRODUCT_NAME])}</Typography.Title>
            <Typography.Paragraph type="secondary" style={{ margin: "8px 0 28px" }}>
              {t("使用控制台账号、口令与验证器动态码登录")}
            </Typography.Paragraph>
            {error && <Alert type="error" message={error} showIcon style={{ marginBottom: 16 }} />}
            <Form layout="vertical" size="large" onFinish={submit} requiredMark={false}>
              <Form.Item name="login_name" label={t("用户名")} rules={[{ required: true, message: t("请输入用户名") }]}>
                <Input prefix={<UserOutlined style={{ opacity: 0.45 }} />} autoComplete="username" autoFocus />
              </Form.Item>
              <Form.Item name="password" label={t("口令")} rules={[{ required: true, message: t("请输入口令") }]}>
                <Input.Password prefix={<LockOutlined style={{ opacity: 0.45 }} />} autoComplete="current-password" />
              </Form.Item>
              <Form.Item
                name="totp"
                label={t("动态码")}
                rules={[{ required: true, pattern: /^\d{6}$/, message: t("请输入验证器中的 6 位动态码") }]}
              >
                <Input prefix={<SafetyOutlined style={{ opacity: 0.45 }} />} inputMode="numeric" maxLength={6}
                  autoComplete="one-time-code" placeholder="000000" style={{ letterSpacing: 4 }} />
              </Form.Item>
              <Button type="primary" htmlType="submit" block loading={busy} style={{ marginTop: 8 }}>
                {t("登录")}
              </Button>
            </Form>
          </div>
        </div>
      </div>
    </div>
  );
}
