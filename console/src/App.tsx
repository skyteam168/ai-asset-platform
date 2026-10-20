import {
  ApartmentOutlined,
  ApiOutlined,
  AuditOutlined,
  CheckSquareOutlined,
  KeyOutlined,
  LinkOutlined,
  SafetyCertificateOutlined,
} from "@ant-design/icons";
import { Button, Layout, Menu, Space, Spin, Tag, Typography } from "antd";
import type { ReactNode } from "react";
import { Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "./auth";
import { OrgProvider } from "./components";
import { ApprovalsPage } from "./pages/Approvals";
import { AssetsPage } from "./pages/Assets";
import { AuditPage } from "./pages/Audit";
import { ConnectionsPage } from "./pages/Connections";
import { ConsumersPage } from "./pages/Consumers";
import { GrantsPage } from "./pages/Grants";
import { LoginPage } from "./pages/Login";
import { OrgsPage } from "./pages/Orgs";
import type { Role } from "./types";

interface Section {
  path: string;
  label: string;
  icon: ReactNode;
  element: ReactNode;
  roles: Role[];
}

// 菜单按角色裁剪；后端仍会对每个接口做权限与范围检查，这里只影响可见性
export const SECTIONS: Section[] = [
  { path: "/orgs", label: "组织与用户", icon: <ApartmentOutlined />, element: <OrgsPage />,
    roles: ["platform_admin", "company_admin", "data_owner", "auditor"] },
  { path: "/consumers", label: "消费方与绑定", icon: <KeyOutlined />, element: <ConsumersPage />,
    roles: ["platform_admin", "company_admin"] },
  { path: "/connections", label: "系统连接", icon: <LinkOutlined />, element: <ConnectionsPage />,
    roles: ["platform_admin", "asset_developer", "data_owner"] },
  { path: "/assets", label: "资产与门禁", icon: <ApiOutlined />, element: <AssetsPage />,
    roles: ["platform_admin", "asset_developer", "data_owner", "business_owner"] },
  { path: "/approvals", label: "审批待办", icon: <CheckSquareOutlined />, element: <ApprovalsPage />,
    roles: ["platform_admin", "data_owner", "business_owner", "company_admin"] },
  { path: "/grants", label: "授权", icon: <SafetyCertificateOutlined />, element: <GrantsPage />,
    roles: ["platform_admin", "company_admin"] },
  { path: "/audit", label: "审计", icon: <AuditOutlined />, element: <AuditPage />,
    roles: ["platform_admin", "auditor", "company_admin"] },
];

const ROLE_LABEL: Record<Role, string> = {
  platform_admin: "平台管理员",
  asset_developer: "资产开发者",
  data_owner: "数据 Owner",
  business_owner: "业务 Owner",
  company_admin: "分公司管理员",
  auditor: "审计员",
};

export function AppShell() {
  const { me, loading, has, logout } = useAuth();
  const location = useLocation();
  const navigate = useNavigate();
  if (loading) return <Spin fullscreen />;
  if (!me) return <LoginPage />;
  const visible = SECTIONS.filter((s) => has(...s.roles));
  const current = visible.find((s) => location.pathname.startsWith(s.path));
  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Layout.Sider width={200} theme="light" style={{ borderRight: "1px solid #f0f0f0" }}>
        <Typography.Title level={5} style={{ padding: "16px 20px", margin: 0 }}>
          AI 资产管理中台
        </Typography.Title>
        <Menu
          mode="inline"
          selectedKeys={current ? [current.path] : []}
          items={visible.map((s) => ({ key: s.path, icon: s.icon, label: s.label }))}
          onClick={(e) => navigate(e.key)}
        />
      </Layout.Sider>
      <Layout>
        <Layout.Header style={{ background: "#fff", borderBottom: "1px solid #f0f0f0", padding: "0 24px" }}>
          <Space style={{ float: "right" }}>
            <span>{me.display_name}</span>
            {[...new Set(me.roles.map((r) => r.role))].map((r) => (
              <Tag key={r}>{ROLE_LABEL[r]}</Tag>
            ))}
            <Tag color="blue">密级 L{me.effective_clearance}</Tag>
            <Button size="small" onClick={() => void logout()}>
              退出
            </Button>
          </Space>
        </Layout.Header>
        <Layout.Content style={{ padding: 24 }}>
          <OrgProvider>
          <Routes>
            {visible.map((s) => (
              <Route key={s.path} path={`${s.path}/*`} element={s.element} />
            ))}
            <Route path="*" element={visible.length ? <Navigate to={visible[0].path} replace /> : <NoAccess />} />
          </Routes>
          </OrgProvider>
        </Layout.Content>
      </Layout>
    </Layout>
  );
}

function NoAccess() {
  return <Typography.Paragraph>当前账号没有可用的控制台功能，请联系平台管理员分配角色。</Typography.Paragraph>;
}
