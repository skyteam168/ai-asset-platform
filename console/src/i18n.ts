// 多语言：中文原文即键（zh-CN 不需要字典，缺译时回落中文）。
// 切换语言会写入 localStorage 并重新加载页面，模块级常量（菜单、标签映射）随之按新语言求值。
import dayjs from "dayjs";
import "dayjs/locale/en";
import "dayjs/locale/vi";
import "dayjs/locale/zh-cn";
import { enUS } from "./locales/en-US";
import { kmKH } from "./locales/km-KH";
import { viVN } from "./locales/vi-VN";

export type Lang = "zh-CN" | "en-US" | "vi-VN" | "km-KH";
export const LANGS: { value: Lang; label: string }[] = [
  { value: "zh-CN", label: "简体中文" },
  { value: "en-US", label: "English" },
  { value: "vi-VN", label: "Tiếng Việt" },
  { value: "km-KH", label: "ភាសាខ្មែរ" },
];
const KEY = "aip.lang";
const DICTS: Record<Lang, Record<string, string>> = { "zh-CN": {}, "en-US": enUS, "vi-VN": viVN, "km-KH": kmKH };
// dayjs 无高棉语包时回落英文
const DAYJS: Record<Lang, string> = { "zh-CN": "zh-cn", "en-US": "en", "vi-VN": "vi", "km-KH": "en" };

function initial(): Lang {
  try {
    const saved = localStorage.getItem(KEY) as Lang | null;
    if (saved && saved in DICTS) return saved;
  } catch {
    // 隐私模式等情况下 localStorage 不可用：用默认语言
  }
  return "zh-CN";
}

let current: Lang = initial();
dayjs.locale(DAYJS[current]);

export function lang(): Lang {
  return current;
}

/** 测试与首次渲染之前可直接设置；界面上切换语言用 switchLang（会重新加载）。 */
export function setLang(next: Lang): void {
  current = next;
  dayjs.locale(DAYJS[next]);
}

export function switchLang(next: Lang): void {
  try {
    localStorage.setItem(KEY, next);
  } catch {
    // 不可写时只对本次页面生效
  }
  setLang(next);
  window.location.reload();
}

/** 翻译：key 是中文原文；args 按位置替换 {0}、{1}…… */
export function t(key: string, args?: unknown[]): string {
  const text = DICTS[current][key] ?? key;
  return args ? text.replace(/\{(\d+)\}/g, (m, i: string) => (Number(i) < args.length ? String(args[Number(i)]) : m)) : text;
}
