/** 资产市场（H48）：消费视角的目录浏览，复用 console catalog。 */
import { Alert, Tag, Typography } from "antd";
import { useMemo, useState } from "react";
import { get } from "../api";
import { PageCard, PAGINATION, Toolbar } from "../components";
import { ListTable } from "../ListTable";
import { useLoad } from "../hooks";
import { t } from "../i18n";

interface CatalogTool {
  capability_id: string;
  version: string;
  title: string;
  description: string;
  risk_level: string;
}

interface CatalogData {
  items: CatalogTool[];
}

const RISK_COLOR: Record<string, string> = { read: "green", write: "orange", danger: "red" };

export function MarketplacePage() {
  const [q, setQ] = useState("");
  const { data, error } = useLoad(
    () => get<CatalogData>(`/console/catalog${q.trim() ? `?q=${encodeURIComponent(q.trim())}` : ""}`),
    [q],
  );
  const items = useMemo(() => data?.items ?? [], [data]);
  return (
    <PageCard>
      <Typography.Title level={4} style={{ marginTop: 0 }}>
        {t("资产市场")}
      </Typography.Title>
      <Typography.Paragraph type="secondary">
        {t("浏览可发现的能力（运维目录同源；授权与可见性以数据面为准）。")}
      </Typography.Paragraph>
      {error ? <Alert type="error" message={error} style={{ marginBottom: 16 }} /> : null}
      <Toolbar placeholder={t("搜索能力或描述")} onSearch={setQ} />
      <ListTable
        rowKey={(r) => `${r.capability_id}@${r.version}`}
        dataSource={items}
        columns={[
          { title: t("能力"), dataIndex: "capability_id", width: 220 },
          { title: t("版本"), dataIndex: "version", width: 100 },
          { title: t("标题"), dataIndex: "title", ellipsis: true },
          {
            title: t("风险"),
            dataIndex: "risk_level",
            width: 90,
            render: (v: string) => <Tag color={RISK_COLOR[v] || "default"}>{v}</Tag>,
          },
          {
            title: t("描述"),
            dataIndex: "description",
            render: (v: string) => (
              <Typography.Text type="secondary" ellipsis style={{ maxWidth: 320 }}>
                {v || "—"}
              </Typography.Text>
            ),
          },
        ]}
        pagination={PAGINATION}
      />
    </PageCard>
  );
}
