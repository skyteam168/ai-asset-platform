import {
  ApartmentOutlined,
  ApiOutlined,
  AuditOutlined,
  BgColorsOutlined,
  CheckSquareOutlined,
  GlobalOutlined,
  KeyOutlined,
  LinkOutlined,
  LogoutOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  SafetyCertificateOutlined,
} from "@ant-design/icons";
import { Button, Divider, Dropdown, Layout, Menu, Popover, Space, Spin, Tag, Tooltip, Typography, theme } from "antd";
import { lazy, Suspense, useState, type ComponentType, type ReactNode } from "react";
import { Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "./auth";
import { OrgProvider } from "./components";
import { LoginPage } from "./pages/Login";
import type { Role } from "./types";
import { Logo, Mark, PLATFORM_NAME } from "./brand";
import { LANGS, lang, switchLang, t } from "./i18n";
import { ThemePanel, useThemePrefs } from "./theme";

// 各页面按需加载，首屏只下载框架与当前页面的代码；登录页很小且是未登录时的首屏，保持直接引入
function page<K extends string>(load: () => Promise<Record<K, ComponentType>>, name: K) {
  return lazy(() => load().then((m) => ({ default: m[name] })));
}
const ApprovalsPage = page(() => import("./pages/Approvals"), "ApprovalsPage");
const AssetsPage = page(() => import("./pages/Assets"), "AssetsPage");
const AuditPage = page(() => import("./pages/Audit"), "AuditPage");
const ConnectionsPage = page(() => import("./pages/Connections"), "ConnectionsPage");
const ConsumersPage = page(() => import("./pages/Consumers"), "ConsumersPage");
const GrantsPage = page(() => import("./pages/Grants"), "GrantsPage");
const OrgsPage = page(() => import("./pages/Orgs"), "OrgsPage");

interface Section {
  path: string;
  label: string;
  icon: ReactNode;
  element: ReactNode;
  roles: Role[];
}

// 菜单按角色裁剪；后端仍会对每个接口做权限与范围检查，这里只影响可见性
export const SECTIONS: Section[] = [
  { path: "/orgs", label: t("组织与用户"), icon: <ApartmentOutlined />, element: <OrgsPage />,
    roles: ["platform_admin", "company_admin", "data_owner", "auditor"] },
  { path: "/consumers", label: t("消费方与绑定"), icon: <KeyOutlined />, element: <ConsumersPage />,
    roles: ["platform_admin", "company_admin"] },
  { path: "/connections", label: t("系统连接"), icon: <LinkOutlined />, element: <ConnectionsPage />,
    roles: ["platform_admin", "asset_developer", "data_owner"] },
  { path: "/assets", label: t("资产与门禁"), icon: <ApiOutlined />, element: <AssetsPage />,
    roles: ["platform_admin", "asset_developer", "data_owner", "business_owner"] },
  { path: "/approvals", label: t("审批待办"), icon: <CheckSquareOutlined />, element: <ApprovalsPage />,
    roles: ["platform_admin", "data_owner", "business_owner", "company_admin", "asset_developer"] },
  { path: "/grants", label: t("授权"), icon: <SafetyCertificateOutlined />, element: <GrantsPage />,
    roles: ["platform_admin", "company_admin"] },
  { path: "/audit", label: t("审计"), icon: <AuditOutlined />, element: <AuditPage />,
    roles: ["platform_admin", "auditor", "company_admin"] },
];

const ROLE_LABEL: Record<Role, string> = {
  platform_admin: t("平台管理员"),
  asset_developer: t("资产开发者"),
  data_owner: t("数据 Owner"),
  business_owner: t("业务 Owner"),
  company_admin: t("分公司管理员"),
  auditor: t("审计员"),
};

export function AppShell() {
  const { me, loading, has, logout } = useAuth();
  const location = useLocation();
  const navigate = useNavigate();
  const prefs = useThemePrefs();
  const { token } = theme.useToken();
  const [collapsed, setCollapsed] = useState(false);
  if (loading) return <Spin fullscreen />;
  if (!me) return <LoginPage />;
  const visible = SECTIONS.filter((s) => has(...s.roles));
  const current = visible.find((s) => location.pathname.startsWith(s.path));
  // 侧栏始终用深色品牌底（白色标志清晰）；内容区随明 / 暗主题
  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Layout.Sider width={232} theme="dark" collapsible collapsed={collapsed} trigger={null}
        style={{ background: "#0f2747" }}>
        <div style={{ height: 64, display: "flex", alignItems: "center", gap: 10, padding: collapsed ? "0 22px" : "0 18px" }}>
          {collapsed ? <Mark size={28} /> : <Logo on="dark" height={26} />}
        </div>
        {!collapsed && (
          <Typography.Text style={{ color: "rgba(255,255,255,0.85)", display: "block", padding: "0 18px 14px", fontSize: 13 }}>
            {t("AI数字资产管理中台")}
          </Typography.Text>
        )}
        <Menu
          theme="dark"
          mode="inline"
          style={{ background: "transparent" }}
          selectedKeys={current ? [current.path] : []}
          items={visible.map((s) => ({ key: s.path, icon: s.icon, label: s.label }))}
          onClick={(e) => navigate(e.key)}
        />
      </Layout.Sider>
      <Layout>
        <Layout.Header
          style={{
            background: token.colorBgContainer,
            borderBottom: `1px solid ${token.colorBorderSecondary}`,
            padding: "0 16px 0 8px",
            display: "flex",
            alignItems: "center",
            gap: 8,
          }}
        >
          <Button type="text" aria-label={collapsed ? t("展开菜单") : t("收起菜单")}
            icon={collapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />} onClick={() => setCollapsed(!collapsed)} />
          <Typography.Text strong style={{ fontSize: 15 }}>{PLATFORM_NAME}</Typography.Text>
          <div style={{ flex: 1 }} />
          <Space size="middle">
            <Dropdown
              menu={{
                selectedKeys: [lang()],
                items: LANGS.map((l) => ({ key: l.value, label: l.label })),
                onClick: (e) => e.key !== lang() && switchLang(e.key as (typeof LANGS)[number]["value"]),
              }}
            >
              <Button type="text" icon={<GlobalOutlined />} aria-label={t("语言")}>
                {LANGS.find((l) => l.value === lang())?.label}
              </Button>
            </Dropdown>
            <Popover trigger="click" placement="bottomRight" title={t("主题")} content={<ThemePanel />}>
              <Button type="text" icon={<BgColorsOutlined />} aria-label={t("主题")} />
            </Popover>
            <Divider type="vertical" />
            <span>{me.display_name}</span>
            {[...new Set(me.roles.map((r) => r.role))].map((r) => (
              <Tag key={r}>{ROLE_LABEL[r]}</Tag>
            ))}
            <Tag color={prefs.primary}>{t("密级 L{0}", [me.effective_clearance])}</Tag>
            <Tooltip title={t("退出")}>
              <Button type="text" icon={<LogoutOutlined />} aria-label={t("退出")} onClick={() => void logout()} />
            </Tooltip>
          </Space>
        </Layout.Header>
        <Layout.Content style={{ padding: 24 }}>
          <OrgProvider>
          <Suspense fallback={<Spin style={{ display: "block", marginTop: 48 }} />}>
          <Routes>
            {visible.map((s) => (
              <Route key={s.path} path={`${s.path}/*`} element={s.element} />
            ))}
            <Route path="*" element={visible.length ? <Navigate to={visible[0].path} replace /> : <NoAccess />} />
          </Routes>
          </Suspense>
          </OrgProvider>
        </Layout.Content>
      </Layout>
    </Layout>
  );
}

function NoAccess() {
  return <Typography.Paragraph>{t("当前账号没有可用的控制台功能，请联系平台管理员分配角色。")}</Typography.Paragraph>;
}
