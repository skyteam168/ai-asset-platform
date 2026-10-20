// 品牌：集团标志 + 产品名（与集团其他中台一致：浅色红标黑字，深色白标浅字）
import { t } from "./i18n";
import type { Mode } from "./theme";

const BASE = import.meta.env.BASE_URL;

export const PLATFORM_NAME = t("申洲集团AI数字资产管理中台");
export const PRODUCT_NAME = t("AI资产中台");

export function Logo({ on, height = 22 }: { on: Mode; height?: number }) {
  return <img src={`${BASE}brand/logo-${on}.png`} alt={t("申洲集团")} style={{ height, width: "auto", display: "block", flex: "none" }} />;
}

export function Mark({ size = 24 }: { size?: number }) {
  return <img src={`${BASE}brand/favicon.png`} alt={t("申洲集团")} style={{ width: size, height: size, display: "block", flex: "none" }} />;
}
