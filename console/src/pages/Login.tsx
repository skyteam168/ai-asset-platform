import {
  BgColorsOutlined,
  LockOutlined,
  SafetyOutlined,
  UserOutlined,
} from "@ant-design/icons";
import { Alert, Button, Dropdown, Form, Input, Popover, Tag, Typography } from "antd";
import { useState } from "react";
import { describeError } from "../api";
import { useAuth } from "../auth";
import { Logo, PRODUCT_NAME } from "../brand";
import { LANGS, lang, switchLang, t, type Lang } from "../i18n";
import { ThemePanel, useThemePrefs } from "../theme";
import { LoginHub } from "./LoginHub";

export function LoginPage() {
  const { login } = useAuth();
  const prefs = useThemePrefs();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

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
    <div
      className="login-stage"
      style={{
        ["--login-primary" as string]: prefs.primary,
        ["--login-primary-soft" as string]: `color-mix(in srgb, ${prefs.primary} 55%, #0a1020)`,
        ["--login-primary-deep" as string]: `color-mix(in srgb, ${prefs.primary} 72%, #05080f)`,
      }}
    >
      <div className="login-stage-glow" aria-hidden />
      <div className="login-stage-grid" aria-hidden />
      <div className="login-stage-scan" aria-hidden />

      <header className="login-stage-top">
        <Logo on="dark" height={28} />
        <div className="login-stage-tools">
          <Dropdown
            menu={{
              selectedKeys: [lang()],
              items: LANGS.map((l) => ({ key: l.value, label: l.label })),
              onClick: (e) => e.key !== lang() && switchLang(e.key as Lang),
            }}
          >
            <Button type="text" className="login-stage-tool-btn">
              {LANGS.find((l) => l.value === lang())?.label}
            </Button>
          </Dropdown>
          <Popover trigger="click" placement="bottomRight" title={t("主题")} content={<ThemePanel />}>
            <Button type="text" className="login-stage-tool-btn" icon={<BgColorsOutlined />} aria-label={t("主题")} />
          </Popover>
        </div>
      </header>

      <div className="login-stage-body">
        <section className="login-stage-copy">
          <div className="login-stage-kicker">{t("AI 智造 · 集团能力底座")}</div>
          <Typography.Title level={1} className="login-stage-title">
            {t("AI数字资产管理中台")}
          </Typography.Title>
          <Typography.Paragraph className="login-stage-lead">
            {t("集团的 AI 转换插座：把旧系统、数据库与 API 接进来，再安全分发给各 Agent 去执行任务。")}
          </Typography.Paragraph>
          <Typography.Paragraph className="login-stage-sub">
            {t("统一登记、授权与审计；Agent 只使用有权访问的能力与数据。")}
          </Typography.Paragraph>
          <div className="login-stage-tags">
            <Tag className="login-stage-tag">{t("产线数据接入")}</Tag>
            <Tag className="login-stage-tag">{t("门禁与授权")}</Tag>
            <Tag className="login-stage-tag">{t("Agent 安全取用")}</Tag>
          </div>
        </section>

        <div className="login-stage-canvas">
          <LoginHub />
        </div>

        <aside className={`login-stage-panel login-stage-panel-${prefs.mode}`}>
          <div className="login-mobile-logo">
            <Logo on={prefs.mode} height={24} />
          </div>
          <Typography.Title level={3} style={{ margin: 0 }}>
            {t("登录 {0}", [PRODUCT_NAME])}
          </Typography.Title>
          <Typography.Paragraph type="secondary" style={{ margin: "8px 0 22px" }}>
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
              <Input
                prefix={<SafetyOutlined style={{ opacity: 0.45 }} />}
                inputMode="numeric"
                maxLength={6}
                autoComplete="one-time-code"
                placeholder="000000"
                style={{ letterSpacing: 4 }}
              />
            </Form.Item>
            <Button type="primary" htmlType="submit" block loading={busy} style={{ marginTop: 8 }}>
              {t("登录")}
            </Button>
          </Form>
          <p className="login-stage-panel-hint">{t("登录后按角色进入审批、资产、接入与风控等生产治理能力。")}</p>
        </aside>
      </div>

      <footer className="login-stage-foot">
        <span>
          © {new Date().getFullYear()} {t("申洲集团")}
        </span>
        <span className="login-stage-foot-note">{t("旧系统与 API 作为插头接入治理底座，经授权与门禁后，安全供给各 AI Agent。")}</span>
      </footer>
    </div>
  );
}
