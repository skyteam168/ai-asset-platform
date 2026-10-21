// 主题：浅色 / 深色、主色、紧凑模式；偏好存 localStorage，切换即时生效（Ant Design 5 的 algorithm 与 token）。
// 配色与集团其他中台（知识库）保持一致：浅底 #f5f6f8 + 白卡片，深底 #121315 + #232428 卡片，主色 #3d6dfa。
import { MoonOutlined, SunOutlined } from "@ant-design/icons";
import {
  App as AntApp,
  ConfigProvider,
  Segmented,
  Space,
  Switch,
  Tooltip,
  Typography,
  theme as antdTheme,
  type ThemeConfig,
} from "antd";
import type { Locale } from "antd/es/locale";
import enUS from "antd/locale/en_US";
import viVN from "antd/locale/vi_VN";
import zhCN from "antd/locale/zh_CN";
import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { lang, t, type Lang } from "./i18n";

export type Mode = "light" | "dark";
export interface ThemePrefs {
  mode: Mode;
  primary: string;
  compact: boolean;
}

export const PRESETS: { color: string; label: string }[] = [
  { color: "#3d6dfa", label: t("经典蓝") },
  { color: "#e5484d", label: t("申洲红") },
  { color: "#12a389", label: t("青绿") },
  { color: "#7c5cfc", label: t("紫") },
];

// 两种模式下的表面色（采样自集团知识库中台）
export const SURFACE: Record<Mode, {
  layout: string; container: string; sider: string; border: string; tableHead: string;
  selected: string; hover: string; subtle: string; text: string; textSecondary: string;
}> = {
  light: {
    layout: "#f5f6f8",
    container: "#ffffff",
    sider: "#fbfcfd",
    border: "#eceef2",
    tableHead: "#f7f8fa",
    selected: "#eef3fe",
    hover: "#f2f3f5",
    subtle: "#f2f3f5",
    text: "#1f2329",
    textSecondary: "#646a73",
  },
  dark: {
    layout: "#121315",
    container: "#1c1d20",
    sider: "#18191c",
    border: "#2c2d31",
    tableHead: "#26272b",
    selected: "#1c2a4c",
    hover: "#26272b",
    subtle: "#25262a",
    text: "#e5e6eb",
    textSecondary: "#a9aeb8",
  },
};

const KEY = "aip.theme";
const DEFAULTS: ThemePrefs = { mode: "light", primary: PRESETS[0].color, compact: false };
const ANTD_LOCALE: Record<Lang, Locale> = { "zh-CN": zhCN, "en-US": enUS, "vi-VN": viVN };

function load(): ThemePrefs {
  try {
    const raw = localStorage.getItem(KEY);
    if (raw) return { ...DEFAULTS, ...(JSON.parse(raw) as Partial<ThemePrefs>) };
  } catch {
    // 不可读时用默认主题
  }
  if (typeof window !== "undefined" && window.matchMedia?.("(prefers-color-scheme: dark)").matches) {
    return { ...DEFAULTS, mode: "dark" };
  }
  return DEFAULTS;
}

export function themeConfig(prefs: ThemePrefs): ThemeConfig {
  const s = SURFACE[prefs.mode];
  const dark = prefs.mode === "dark";
  return {
    algorithm: [dark ? antdTheme.darkAlgorithm : antdTheme.defaultAlgorithm, ...(prefs.compact ? [antdTheme.compactAlgorithm] : [])],
    token: {
      colorPrimary: prefs.primary,
      colorInfo: prefs.primary,
      colorLink: prefs.primary,
      colorBgLayout: s.layout,
      colorBgContainer: dark ? "#232428" : s.container,
      colorBgElevated: dark ? "#2a2b30" : "#ffffff",
      colorBorderSecondary: s.border,
      colorBorder: dark ? "#3a3b40" : "#dcdfe6",
      colorText: dark ? "#e5e6eb" : "#1f2329",
      colorTextSecondary: dark ? "#a9aeb8" : "#646a73",
      borderRadius: 6,
      borderRadiusLG: 10,
      controlHeight: 32,
      fontFamily:
        '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Helvetica Neue", Arial, sans-serif',
      wireframe: false,
    },
    components: {
      Layout: { siderBg: s.sider, headerBg: s.container, bodyBg: s.layout, headerHeight: 56, headerPadding: "0 20px" },
      Menu: {
        itemBg: "transparent",
        subMenuItemBg: "transparent",
        itemSelectedBg: s.selected,
        itemSelectedColor: prefs.primary,
        itemHoverBg: s.hover,
        itemBorderRadius: 8,
        itemHeight: 40,
        itemMarginInline: 10,
        iconSize: 16,
        activeBarBorderWidth: 0,
      },
      Table: {
        headerBg: s.tableHead,
        headerColor: dark ? "#e5e6eb" : "#1f2329",
        headerSplitColor: "transparent",
        rowHoverBg: s.hover,
        borderColor: s.border,
        cellPaddingBlock: 13,
      },
      Card: { paddingLG: 20 },
      Tabs: { horizontalItemGutter: 28 },
      Button: { primaryShadow: "none", defaultShadow: "none" },
      Input: { activeShadow: "none" },
      Tag: { defaultBg: s.subtle },
    },
  };
}

interface ThemeState extends ThemePrefs {
  update: (patch: Partial<ThemePrefs>) => void;
}

const Ctx = createContext<ThemeState | null>(null);

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [prefs, setPrefs] = useState<ThemePrefs>(load);
  useEffect(() => {
    try {
      localStorage.setItem(KEY, JSON.stringify(prefs));
    } catch {
      // 不可写时只对本次页面生效
    }
    const root = document.documentElement;
    root.dataset.theme = prefs.mode;
    root.style.colorScheme = prefs.mode;
    root.style.setProperty("--aip-primary", prefs.primary);
    for (const [k, v] of Object.entries(SURFACE[prefs.mode])) root.style.setProperty(`--aip-${k}`, v);
  }, [prefs]);
  const value = useMemo<ThemeState>(() => ({ ...prefs, update: (patch) => setPrefs((p) => ({ ...p, ...patch })) }), [prefs]);
  const config = useMemo(() => themeConfig(prefs), [prefs]);
  return (
    <Ctx.Provider value={value}>
      <ConfigProvider locale={ANTD_LOCALE[lang()]} theme={config} button={{ autoInsertSpace: false }}>
        <AntApp>{children}</AntApp>
      </ConfigProvider>
    </Ctx.Provider>
  );
}

export function useThemePrefs(): ThemeState {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useThemePrefs must be used inside ThemeProvider");
  return ctx;
}

/** 主题面板：浅色 / 深色、主色、紧凑模式（即时生效并记住选择） */
export function ThemePanel() {
  const prefs = useThemePrefs();
  return (
    <Space direction="vertical" size="middle" style={{ width: 240 }}>
      <Segmented
        block
        value={prefs.mode}
        onChange={(v) => prefs.update({ mode: v as Mode })}
        options={[
          { value: "light", label: t("浅色"), icon: <SunOutlined /> },
          { value: "dark", label: t("深色"), icon: <MoonOutlined /> },
        ]}
      />
      <div>
        <Typography.Text type="secondary">{t("主色")}</Typography.Text>
        <Space style={{ marginTop: 8 }}>
          {PRESETS.map((p) => (
            <Tooltip key={p.color} title={p.label}>
              <button
                type="button"
                aria-label={p.label}
                aria-pressed={prefs.primary === p.color}
                onClick={() => prefs.update({ primary: p.color })}
                className="aip-swatch"
                style={{ background: p.color, outlineColor: prefs.primary === p.color ? p.color : "transparent" }}
              />
            </Tooltip>
          ))}
        </Space>
      </div>
      <Space>
        <Switch size="small" checked={prefs.compact} onChange={(v) => prefs.update({ compact: v })} />
        <Typography.Text>{t("紧凑模式")}</Typography.Text>
      </Space>
    </Space>
  );
}
