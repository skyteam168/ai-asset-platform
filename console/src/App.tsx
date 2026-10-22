import {
  AlertOutlined,
  ApartmentOutlined,
  ApiOutlined,
  AuditOutlined,
  BellOutlined,
  BgColorsOutlined,
  BookOutlined,
  CheckSquareOutlined,
  TeamOutlined,
  CloudUploadOutlined,
  ClusterOutlined,
  CloseOutlined,
  DownOutlined,
  KeyOutlined,
  LeftOutlined,
  LinkOutlined,
  LogoutOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  MoonOutlined,
  QuestionCircleOutlined,
  ReloadOutlined,
  RightOutlined,
  SafetyCertificateOutlined,
  SafetyOutlined,
  SearchOutlined,
  SettingOutlined,
  SunOutlined,
  ExperimentOutlined,
  DashboardOutlined,
  ShopOutlined,
  BugOutlined,
  RocketOutlined,
} from "@ant-design/icons";
import { AutoComplete, Avatar, Badge, Button, DatePicker, Dropdown, Form, Input, Layout, Menu, Modal, Select, Space, Spin, Tag, Tooltip, Typography } from "antd";
import dayjs from "dayjs";
import type { MenuProps } from "antd";
import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState, type ComponentType, type ReactNode } from "react";
import { Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { get, post, type Page } from "./api";
import { useAction, useLoad } from "./hooks";
import { useAuth } from "./auth";
import { Logo, Mark, PRODUCT_NAME } from "./brand";
import { OrgProvider } from "./components";
import { HelpDrawer } from "./Help";
import { LANGS, lang, switchLang, t, type Lang } from "./i18n";
import { LoginPage } from "./pages/Login";
import { ThemePanel, useThemePrefs } from "./theme";
import type { Role, Todo } from "./types";

// 各页面按需加载，首屏只下载框架与当前页面的代码；登录页很小且是未登录时的首屏，保持直接引入
function page<K extends string>(load: () => Promise<Record<K, ComponentType>>, name: K) {
  return lazy(() => load().then((m) => ({ default: m[name] })));
}
const ApprovalsPage = page(() => import("./pages/Approvals"), "ApprovalsPage");
const AssetsPage = page(() => import("./pages/Assets"), "AssetsPage");
const AuditPage = page(() => import("./pages/Audit"), "AuditPage");
const CatalogPage = page(() => import("./pages/Catalog"), "CatalogPage");
const ConnectionsPage = page(() => import("./pages/Connections"), "ConnectionsPage");
const ConsumersPage = page(() => import("./pages/Consumers"), "ConsumersPage");
const GrantsPage = page(() => import("./pages/Grants"), "GrantsPage");
const OrgsPage = page(() => import("./pages/Orgs"), "OrgsPage");
const RiskPage = page(() => import("./pages/Risk"), "RiskPage");
const RunsPage = page(() => import("./pages/Runs"), "RunsPage");
const DistributionPage = page(() => import("./pages/Distribution"), "DistributionPage");
const LlmConfigPage = page(() => import("./pages/LlmConfig"), "LlmConfigPage");
const OpsDashboardPage = page(() => import("./pages/OpsDashboard"), "OpsDashboardPage");
const MarketplacePage = page(() => import("./pages/Marketplace"), "MarketplacePage");
const PrismMocksPage = page(() => import("./pages/PrismMocks"), "PrismMocksPage");
const ToolWizardPage = page(() => import("./pages/ToolWizard"), "ToolWizardPage");

type Group = "access" | "governance" | "system";

interface Section {
  path: string;
  label: string;
  icon: ReactNode;
  element: ReactNode;
  roles: Role[];
  group?: Group;
}

const GROUPS: Record<Group, { label: string; icon: ReactNode }> = {
  access: { label: t("接入管理"), icon: <ClusterOutlined /> },
  governance: { label: t("风控与审计"), icon: <SafetyOutlined /> },
  system: { label: t("系统管理"), icon: <SettingOutlined /> },
};

// 菜单按角色裁剪；后端仍会对每个接口做权限与范围检查，这里只影响可见性
export const SECTIONS: Section[] = [
  { path: "/ops", label: t("运营概览"), icon: <DashboardOutlined />, element: <OpsDashboardPage />,
    roles: ["platform_admin", "company_admin", "data_owner", "auditor"] },
  { path: "/approvals", label: t("审批待办"), icon: <CheckSquareOutlined />, element: <ApprovalsPage />,
    roles: ["platform_admin", "data_owner", "business_owner", "company_admin", "asset_developer"] },
  { path: "/assets", label: t("资产与门禁"), icon: <ApiOutlined />, element: <AssetsPage />,
    roles: ["platform_admin", "asset_developer", "data_owner", "business_owner"] },
  { path: "/wizard", label: t("创建 Tool 向导"), icon: <RocketOutlined />, element: <ToolWizardPage />,
    roles: ["platform_admin", "asset_developer"] },
  { path: "/marketplace", label: t("资产市场"), icon: <ShopOutlined />, element: <MarketplacePage />,
    roles: ["platform_admin", "asset_developer", "data_owner", "company_admin"] },
  { path: "/catalog", label: t("资产目录"), icon: <BookOutlined />, element: <CatalogPage />,
    roles: ["platform_admin", "asset_developer", "data_owner", "company_admin"] },
  { path: "/distribution", label: t("分发与 Skill"), icon: <CloudUploadOutlined />, element: <DistributionPage />,
    roles: ["platform_admin", "asset_developer", "company_admin"] },
  { path: "/prism-mocks", label: t("Prism Mock"), icon: <BugOutlined />, element: <PrismMocksPage />,
    roles: ["platform_admin", "asset_developer"], group: "access" },
  { path: "/grants", label: t("授权"), icon: <SafetyCertificateOutlined />, element: <GrantsPage />,
    roles: ["platform_admin", "company_admin"] },
  { path: "/consumers", label: t("消费方与绑定"), icon: <KeyOutlined />, element: <ConsumersPage />,
    roles: ["platform_admin", "company_admin"], group: "access" },
  { path: "/connections", label: t("系统连接"), icon: <LinkOutlined />, element: <ConnectionsPage />,
    roles: ["platform_admin", "asset_developer", "data_owner"], group: "access" },
  { path: "/risk", label: t("风控与计量"), icon: <AlertOutlined />, element: <RiskPage />,
    roles: ["platform_admin", "data_owner", "auditor", "company_admin"], group: "governance" },
  { path: "/llm", label: t("LLM 配置"), icon: <ExperimentOutlined />, element: <LlmConfigPage />,
    roles: ["platform_admin"], group: "governance" },
  { path: "/audit", label: t("审计"), icon: <AuditOutlined />, element: <AuditPage />,
    roles: ["platform_admin", "auditor", "company_admin"], group: "governance" },
  { path: "/orgs", label: t("组织与用户"), icon: <ApartmentOutlined />, element: <OrgsPage />,
    roles: ["platform_admin", "company_admin", "data_owner", "auditor"], group: "system" },
];

const ROLE_LABEL: Record<Role, string> = {
  platform_admin: t("平台管理员"),
  asset_developer: t("资产开发者"),
  data_owner: t("数据 Owner"),
  business_owner: t("业务 Owner"),
  company_admin: t("分公司管理员"),
  auditor: t("审计员"),
};

const LANG_SHORT: Record<Lang, string> = { "zh-CN": "中", "en-US": "EN", "vi-VN": "VI", "km-KH": "ខ្មែរ" };
const TABS_KEY = "aip.tabs";

function menuItems(visible: Section[]): MenuProps["items"] {
  const items: NonNullable<MenuProps["items"]> = [];
  for (const s of visible.filter((x) => !x.group)) items.push({ key: s.path, icon: s.icon, label: s.label });
  for (const g of Object.keys(GROUPS) as Group[]) {
    const children = visible.filter((s) => s.group === g).map((s) => ({ key: s.path, icon: s.icon, label: s.label }));
    if (children.length) items.push({ key: `group:${g}`, icon: GROUPS[g].icon, label: GROUPS[g].label, children });
  }
  return items;
}

/** 已打开页面的页签（存 sessionStorage，刷新后保留） */
function useOpenTabs(visible: Section[], current: Section | undefined) {
  const [tabs, setTabs] = useState<string[]>(() => {
    try {
      return JSON.parse(sessionStorage.getItem(TABS_KEY) ?? "[]") as string[];
    } catch {
      return [];
    }
  });
  const [reloads, setReloads] = useState<Record<string, number>>({});
  const allowed = useMemo(() => new Set(visible.map((s) => s.path)), [visible]);
  useEffect(() => {
    setTabs((prev) => {
      const kept = prev.filter((p) => allowed.has(p));
      return current && !kept.includes(current.path) ? [...kept, current.path] : kept;
    });
  }, [current, allowed]);
  useEffect(() => {
    try {
      sessionStorage.setItem(TABS_KEY, JSON.stringify(tabs));
    } catch {
      // 不可写时只保留在内存
    }
  }, [tabs]);
  return {
    tabs,
    close: (path: string) => {
      const i = tabs.indexOf(path);
      const next = tabs.filter((p) => p !== path);
      setTabs(next);
      return current?.path === path ? (next[i] ?? next[i - 1] ?? null) : undefined;
    },
    reload: (path: string) => setReloads((r) => ({ ...r, [path]: (r[path] ?? 0) + 1 })),
    reloadKey: (path: string) => reloads[path] ?? 0,
  };
}

export function AppShell() {
  const { me, loading, has, logout } = useAuth();
  const location = useLocation();
  const navigate = useNavigate();
  const prefs = useThemePrefs();
  const [collapsed, setCollapsed] = useState(false);
  const visible = useMemo(() => (me ? SECTIONS.filter((s) => has(...s.roles)) : []), [me, has]);
  const current = visible.find((s) => location.pathname.startsWith(s.path));
  const tabs = useOpenTabs(visible, current);
  if (loading) return <Spin fullscreen />;
  if (!me) return <LoginPage />;
  const openKeys = Object.keys(GROUPS).map((g) => `group:${g}`);
  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Layout.Sider className="aip-sider" width={232} collapsedWidth={64} collapsible collapsed={collapsed} trigger={null}
        style={{ position: "sticky", top: 0, height: "100vh" }}>
        <div className="aip-brand" style={collapsed ? { padding: "0 20px" } : undefined}>
          {collapsed ? <Mark size={24} /> : (
            <>
              <Logo on={prefs.mode} height={20} />
              <span className="aip-brand-divider" />
              <span className="aip-brand-name">{PRODUCT_NAME}</span>
            </>
          )}
        </div>
        <Menu
          className="aip-sider-menu"
          mode="inline"
          inlineIndent={20}
          defaultOpenKeys={openKeys}
          selectedKeys={current ? [current.path] : []}
          items={menuItems(visible)}
          onClick={(e) => navigate(e.key)}
        />
        <div className="aip-sider-foot">
          <Button type="text" icon={collapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />} onClick={() => setCollapsed(!collapsed)}
            aria-label={collapsed ? t("展开菜单") : t("收起菜单")}>
            {!collapsed && t("收起菜单")}
          </Button>
        </div>
      </Layout.Sider>
      <Layout>
        <div className="aip-chrome">
          <Layout.Header className="aip-header">
            <span className="aip-page-title">{current?.label ?? PRODUCT_NAME}</span>
            <div style={{ flex: 1 }} />
            <Space size={6} align="center">
              <PageSearch sections={visible} onPick={(p) => navigate(p)} />
              <ServiceStatus />
              <HelpButton />
              <Tooltip title={prefs.mode === "dark" ? t("浅色") : t("深色")}>
                <Button className="aip-icon-btn" type="text" aria-label={t("切换明暗")}
                  icon={prefs.mode === "dark" ? <SunOutlined /> : <MoonOutlined />}
                  onClick={() => prefs.update({ mode: prefs.mode === "dark" ? "light" : "dark" })} />
              </Tooltip>
              <Dropdown menu={{ selectedKeys: [lang()], items: LANGS.map((l) => ({ key: l.value, label: l.label })),
                onClick: (e) => e.key !== lang() && switchLang(e.key as Lang) }}>
                <Button className="aip-icon-btn" type="text" aria-label={t("语言")} style={{ fontSize: 14, fontWeight: 600 }}>
                  {LANG_SHORT[lang()]}
                </Button>
              </Dropdown>
              <Notifications onOpen={() => navigate("/approvals")} enabled={has(...SECTIONS[0].roles)} />
              <UserMenu name={me.display_name} roles={[...new Set(me.roles.map((r) => r.role))]} clearance={me.effective_clearance}
                onLogout={() => void logout()} />
            </Space>
          </Layout.Header>
          <TabStrip
            tabs={tabs.tabs.map((p) => SECTIONS.find((s) => s.path === p)).filter((s): s is Section => Boolean(s))}
            active={current?.path}
            onPick={(p) => navigate(p)}
            onClose={(p) => {
              const next = tabs.close(p);
              if (next !== undefined) navigate(next ?? "/");
            }}
            onReload={() => current && tabs.reload(current.path)}
          />
        </div>
        <Layout.Content className="aip-content">
          <OrgProvider>
            <Suspense fallback={<Spin style={{ display: "block", marginTop: 48 }} />}>
              <Routes>
                {visible.map((s) => (
                  <Route key={s.path} path={`${s.path}/*`} element={<div key={tabs.reloadKey(s.path)}>{s.element}</div>} />
                ))}
                <Route path="/runs/:id" element={<RunsPage />} />
                <Route path="*" element={visible.length ? <Navigate to={visible[0].path} replace /> : <NoAccess />} />
              </Routes>
            </Suspense>
          </OrgProvider>
        </Layout.Content>
      </Layout>
    </Layout>
  );
}

/** 功能搜索：Ctrl / ⌘ + K 聚焦，按名称跳转页面 */
function PageSearch({ sections, onPick }: { sections: Section[]; onPick: (path: string) => void }) {
  const [value, setValue] = useState("");
  const ref = useRef<{ focus: () => void } | null>(null);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        ref.current?.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  const options = sections
    .filter((s) => !value || s.label.toLowerCase().includes(value.toLowerCase()) || s.path.includes(value.toLowerCase()))
    .map((s) => ({ value: s.path, label: <Space>{s.icon}{s.label}</Space> }));
  const mac = typeof navigator !== "undefined" && /Mac/.test(navigator.platform);
  return (
    <AutoComplete
      ref={ref as never}
      value={value}
      options={options}
      onSearch={setValue}
      onSelect={(v: string) => {
        setValue("");
        onPick(v);
      }}
      popupMatchSelectWidth={250}
    >
      <Input className="aip-search" prefix={<SearchOutlined style={{ opacity: 0.55 }} />} placeholder={t("搜索功能…")}
        aria-label={t("搜索功能")} suffix={<span className="aip-kbd">{mac ? "⌘ K" : "Ctrl K"}</span>} />
    </AutoComplete>
  );
}

/** 服务状态：每 30 秒探测控制面健康检查 */
function ServiceStatus() {
  const [ok, setOk] = useState<boolean | null>(null);
  useEffect(() => {
    let stop = false;
    const probe = async () => {
      try {
        const r = await fetch("/healthz", { cache: "no-store" });
        if (!stop) setOk(r.ok);
      } catch {
        if (!stop) setOk(false);
      }
    };
    void probe();
    const id = window.setInterval(() => void probe(), 30_000);
    return () => {
      stop = true;
      window.clearInterval(id);
    };
  }, []);
  if (ok === null) return null;
  return (
    <span className={`aip-status ${ok ? "ok" : "down"}`} role="status">
      <i />
      {ok ? t("服务正常") : t("服务异常")}
    </span>
  );
}

function HelpButton() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <Tooltip title={t("帮助")}>
        <Button className="aip-icon-btn" type="text" icon={<QuestionCircleOutlined />} aria-label={t("帮助")} onClick={() => setOpen(true)} />
      </Tooltip>
      <HelpDrawer open={open} onClose={() => setOpen(false)} />
    </>
  );
}

/** 待办提醒：调用确认、工作流任务与门禁 / 使用申请审批的数量 */
function Notifications({ onOpen, enabled }: { onOpen: () => void; enabled: boolean }) {
  const [count, setCount] = useState(0);
  const refresh = useCallback(async () => {
    if (!enabled) return;
    try {
      const [todo, approvals] = await Promise.all([get<Todo>("/todo"), get<Page<unknown>>("/approvals")]);
      setCount((todo.confirmations?.length ?? 0) + (todo.tasks?.length ?? 0) + (approvals.items?.length ?? 0));
    } catch {
      // 提醒失败不影响页面
    }
  }, [enabled]);
  useEffect(() => {
    void refresh();
    const id = window.setInterval(() => void refresh(), 60_000);
    return () => window.clearInterval(id);
  }, [refresh]);
  return (
    <Tooltip title={t("待办 {0} 项", [count])}>
      <Badge count={count} size="small" offset={[-6, 6]}>
        <Button className="aip-icon-btn" type="text" icon={<BellOutlined />} aria-label={t("待办提醒")} onClick={onOpen} disabled={!enabled} />
      </Badge>
    </Tooltip>
  );
}

function UserMenu(p: { name: string; roles: Role[]; clearance: number; onLogout: () => void }) {
  const prefs = useThemePrefs();
  const [themeOpen, setThemeOpen] = useState(false);
  const [delegateOpen, setDelegateOpen] = useState(false);
  return (
    <>
      <Dropdown
        trigger={["click"]}
        dropdownRender={(menu) => (
          <div className="ant-dropdown-menu" style={{ padding: 0, minWidth: 220 }}>
            <div style={{ padding: "12px 16px 8px" }}>
              <Typography.Text strong>{p.name}</Typography.Text>
              <div style={{ marginTop: 6 }}>
                {p.roles.map((r) => <Tag key={r} style={{ marginBottom: 4 }}>{ROLE_LABEL[r]}</Tag>)}
                <Tag color={prefs.primary}>{t("密级 L{0}", [p.clearance])}</Tag>
              </div>
            </div>
            {menu}
          </div>
        )}
        menu={{
          items: [
            { type: "divider" },
            { key: "delegate", icon: <TeamOutlined />, label: t("审批代理人"), onClick: () => setDelegateOpen(true) },
            { key: "theme", icon: <BgColorsOutlined />, label: t("界面设置"), onClick: () => setThemeOpen(true) },
            { key: "logout", icon: <LogoutOutlined />, label: t("退出"), danger: true, onClick: p.onLogout },
          ],
        }}
      >
        <span className="aip-user" role="button" tabIndex={0} aria-label={t("账号菜单")}>
          <Avatar size={28} style={{ background: prefs.primary, fontSize: 13 }}>{p.name.slice(0, 1)}</Avatar>
          <span>{p.name}</span>
          <DownOutlined style={{ fontSize: 10, opacity: 0.6 }} />
        </span>
      </Dropdown>
      <Modal open={themeOpen} title={t("界面设置")} footer={null} width={300} onCancel={() => setThemeOpen(false)}>
        <ThemePanel />
      </Modal>
      <DelegateModal open={delegateOpen} onClose={() => setDelegateOpen(false)} />
    </>
  );
}

function DelegateModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [form] = Form.useForm();
  const act = useAction();
  const users = useLoad(() => get<Page<{ id: string; display_name: string }>>("/users"), []);
  return (
    <Modal open={open} title={t("设置审批代理人")} onCancel={onClose} onOk={() => form.submit()} destroyOnClose>
      <Form form={form} layout="vertical" preserve={false}
        onFinish={async (v) => {
          const body = { delegate_user_id: v.delegate_user_id, valid_to: v.valid_to.toISOString() };
          if (await act(() => post("/approval-delegates", body), t("代理人已设置"))) { onClose(); form.resetFields(); }
        }}>
        <Form.Item name="delegate_user_id" label={t("代理人")} rules={[{ required: true }]}>
          <Select showSearch optionFilterProp="label" placeholder={t("选择用户")}
            options={(users.data?.items ?? []).map((u) => ({ value: u.id, label: u.display_name }))} />
        </Form.Item>
        <Form.Item name="valid_to" label={t("有效期至（最多 60 天）")} rules={[{ required: true }]}>
          <DatePicker disabledDate={(d) => d.isBefore(dayjs()) || d.isAfter(dayjs().add(60, "day"))} />
        </Form.Item>
      </Form>
    </Modal>
  );
}

function TabStrip(p: {
  tabs: Section[];
  active?: string;
  onPick: (path: string) => void;
  onClose: (path: string) => void;
  onReload: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const scroll = (dx: number) => ref.current?.scrollBy({ left: dx });
  return (
    <div className="aip-tabs">
      <Button type="text" size="small" icon={<LeftOutlined />} aria-label={t("向左滚动")} onClick={() => scroll(-240)} />
      <div className="aip-tabs-scroll" ref={ref} role="tablist">
        {p.tabs.map((s) => (
          <button key={s.path} type="button" role="tab" aria-selected={p.active === s.path}
            className={`aip-tab${p.active === s.path ? " active" : ""}`} onClick={() => p.onPick(s.path)}>
            {s.label}
            {p.tabs.length > 1 && (
              <span className="aip-tab-close" role="button" aria-label={t("关闭 {0}", [s.label])}
                onClick={(e) => {
                  e.stopPropagation();
                  p.onClose(s.path);
                }}>
                <CloseOutlined />
              </span>
            )}
          </button>
        ))}
      </div>
      <Button type="text" size="small" icon={<RightOutlined />} aria-label={t("向右滚动")} onClick={() => scroll(240)} />
      <Tooltip title={t("刷新当前页")}>
        <Button type="text" size="small" icon={<ReloadOutlined />} aria-label={t("刷新当前页")} onClick={p.onReload} />
      </Tooltip>
    </div>
  );
}

function NoAccess() {
  return <Typography.Paragraph>{t("当前账号没有可用的控制台功能，请联系平台管理员分配角色。")}</Typography.Paragraph>;
}

export { ThemePanel };
