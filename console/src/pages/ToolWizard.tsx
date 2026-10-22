/** 创建 Tool 五步向导（O2）：整页入口，复用 ApiImportDrawer。 */
import { Alert, Typography } from "antd";
import { get, type Page } from "../api";
import { PageCard } from "../components";
import { useLoad } from "../hooks";
import type { Connection } from "../types";
import { t } from "../i18n";
import { ApiImportDrawer } from "./ApiImport";

export function ToolWizardPage() {
  const conns = useLoad(() => get<Page<Connection>>("/connections"), []);
  return (
    <PageCard>
      <Typography.Title level={4} style={{ marginTop: 0 }}>{t("创建 Tool 向导")}</Typography.Title>
      <Typography.Paragraph type="secondary">
        {t("五步：粘贴文档 → 预览草稿 → 真连接点测 → 确认描述 → 落库为草稿。落库后到资产与门禁补门禁用例。")}
      </Typography.Paragraph>
      {conns.error && <Alert type="error" message={conns.error} style={{ marginBottom: 12 }} />}
      {!conns.loading && (
        <ApiImportDrawer
          open
          embedded
          connections={conns.data?.items ?? []}
          onClose={() => undefined}
          onDone={() => conns.reload()}
        />
      )}
    </PageCard>
  );
}
