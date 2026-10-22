/** 常见 Problem.code 展示译文（H49）；缺省回落 code 本身。 */
import { t } from "./i18n";

const LABELS: Record<string, string> = {
  UNAUTHENTICATED: "未认证",
  PERMISSION_DENIED: "无权限",
  NOT_FOUND: "未找到",
  INVALID_ARGUMENT: "参数无效",
  TOKEN_EXPIRED: "令牌已过期",
  CONFIRMATION_REQUIRED: "需要确认",
  DOWNSTREAM_UNAVAILABLE: "下游不可用",
  INTERNAL: "内部错误",
};

export function errorCodeLabel(code: string | undefined | null): string {
  if (!code) return t("错误码");
  const zh = LABELS[code] ?? code;
  return t(zh);
}
