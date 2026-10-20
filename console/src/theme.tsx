// 主题：明 / 暗、品牌主色、紧凑模式；偏好存 localStorage，切换即时生效（Ant Design 5 的 algorithm 与 token）。
import { MoonOutlined, SunOutlined } from "@ant-design/icons";
import { App as AntApp, ConfigProvider, Segmented, Space, Switch, Tooltip, Typography, theme as antdTheme } from "antd";
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

// 申洲蓝取自集团标志；其余为可选强调色
export const PRESETS: { color: string; label: string }[] = [
  { color: "#1f5fa8", label: t("申洲蓝") },
  { color: "#0e7c86", label: t("青绿") },
  { color: "#5b3fbf", label: t("紫") },
  { color: "#c2410c", label: t("橙") },
];

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
    document.documentElement.dataset.theme = prefs.mode;
    document.documentElement.style.colorScheme = prefs.mode;
  }, [prefs]);
  const value = useMemo<ThemeState>(
    () => ({ ...prefs, update: (patch) => setPrefs((p) => ({ ...p, ...patch })) }),
    [prefs],
  );
  const algorithm = [
    prefs.mode === "dark" ? antdTheme.darkAlgorithm : antdTheme.defaultAlgorithm,
    ...(prefs.compact ? [antdTheme.compactAlgorithm] : []),
  ];
  return (
    <Ctx.Provider value={value}>
      <ConfigProvider locale={ANTD_LOCALE[lang()]} theme={{ algorithm, token: { colorPrimary: prefs.primary, borderRadius: 6 } }}>
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

/** 主题面板：明 / 暗、品牌主色、紧凑模式（即时生效并记住选择） */
export function ThemePanel() {
  const prefs = useThemePrefs();
  return (
    <Space direction="vertical" size="middle" style={{ width: 240 }}>
      <Segmented
        block
        value={prefs.mode}
        onChange={(v) => prefs.update({ mode: v as "light" | "dark" })}
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
                style={{
                  width: 24, height: 24, borderRadius: "50%", background: p.color, cursor: "pointer",
                  border: prefs.primary === p.color ? "2px solid currentColor" : "2px solid transparent",
                  outline: prefs.primary === p.color ? `2px solid ${p.color}` : "none", outlineOffset: 1,
                }}
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
