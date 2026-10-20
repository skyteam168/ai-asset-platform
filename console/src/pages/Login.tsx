import { Alert, Button, Card, Form, Input } from "antd";
import { useState } from "react";
import { describeError } from "../api";
import { useAuth } from "../auth";

export function LoginPage() {
  const { login } = useAuth();
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
    <div style={{ display: "grid", placeItems: "center", minHeight: "100vh", background: "#f5f7fa" }}>
      <Card title="AI 资产管理中台 · 控制台登录" style={{ width: 380 }}>
        {error && <Alert type="error" message={error} showIcon style={{ marginBottom: 16 }} />}
        <Form layout="vertical" onFinish={submit} requiredMark={false}>
          <Form.Item name="login_name" label="用户名" rules={[{ required: true, message: "请输入用户名" }]}>
            <Input autoComplete="username" autoFocus />
          </Form.Item>
          <Form.Item name="password" label="口令" rules={[{ required: true, message: "请输入口令" }]}>
            <Input.Password autoComplete="current-password" />
          </Form.Item>
          <Form.Item
            name="totp"
            label="动态码"
            rules={[{ required: true, pattern: /^\d{6}$/, message: "请输入验证器中的 6 位动态码" }]}
          >
            <Input inputMode="numeric" maxLength={6} autoComplete="one-time-code" />
          </Form.Item>
          <Button type="primary" htmlType="submit" block loading={busy}>
            登录
          </Button>
        </Form>
      </Card>
    </div>
  );
}
