// 品牌：平台名称与标志（标志为白色透明底，浅色背景下用着色版本）
import { t } from "./i18n";
import type { Mode } from "./theme";

const BASE = import.meta.env.BASE_URL;

export const PLATFORM_NAME = t("申洲集团AI数字资产管理中台");

export function Logo({ on, height = 28 }: { on: Mode; height?: number }) {
  return (
    <img
      src={`${BASE}brand/${on === "dark" ? "logo-white" : "logo-color"}.png`}
      alt={t("申洲集团")}
      style={{ height, display: "block" }}
    />
  );
}

export function Mark({ size = 28 }: { size?: number }) {
  return <img src={`${BASE}brand/favicon.png`} alt={t("申洲集团")} style={{ width: size, height: size, display: "block" }} />;
}
