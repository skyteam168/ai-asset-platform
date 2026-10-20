import { Alert, Table, Tabs, Tag, Typography } from "antd";
import { useState } from "react";
import { get } from "../api";
import { matches, PageCard, PAGINATION, Toolbar } from "../components";
import { useLoad } from "../hooks";
import { t } from "../i18n";

interface CatalogTool {
  capability_id: string;
  version: string;
  title: string;
  description: string;
  risk_level: string;
  egress_class: string;
  data_classification: number;
  bundles: string[];
}

interface CatalogBundle {
  name: string;
  capability_id: string;
  version: string;
  description: string;
  tools: string[];
}

interface CatalogData {
  items: CatalogTool[];
  bundles: CatalogBundle[];
}

const RISK_COLOR: Record<string, string> = { read: "green", write: "orange", danger: "red" };

export function CatalogPage() {
  const data = useLoad(() => get<CatalogData>("/console/catalog"), []);
  const [kw, setKw] = useState("");
  return (
    <PageCard>
      <Toolbar placeholder={t("能力 / 套件 / 描述")} onSearch={setKw} />
      {data.error && <Alert type="error" message={data.error} style={{ marginBottom: 12 }} />}
      <Tabs
        items={[
          {
            key: "tools",
            label: t("能力目录"),
            children: (
              <Table<CatalogTool>
                pagination={PAGINATION}
                rowKey="capability_id"
                loading={data.loading}
                dataSource={(data.data?.items ?? []).filter((r) =>
                  matches(r, kw, ["capability_id", "title", "description"]),
                )}
                columns={[
                  {
                    title: t("能力"),
                    dataIndex: "capability_id",
                    render: (v: string, r) => (
                      <span>
                        <Typography.Text strong>{v}</Typography.Text>
                        <Typography.Text type="secondary"> @{r.version}</Typography.Text>
                      </span>
                    ),
                  },
                  { title: t("标题"), dataIndex: "title" },
                  {
                    title: t("风险"),
                    dataIndex: "risk_level",
                    width: 90,
                    render: (v: string) => <Tag color={RISK_COLOR[v] ?? "default"}>{v}</Tag>,
                  },
                  { title: t("密级"), dataIndex: "data_classification", width: 70 },
                  {
                    title: t("外发"),
                    dataIndex: "egress_class",
                    width: 90,
                    render: (v: string) => (v === "external" ? <Tag color="red">{v}</Tag> : v),
                  },
                  {
                    title: t("所属套件"),
                    dataIndex: "bundles",
                    render: (v: string[]) => (v.length ? v.map((b) => <Tag key={b}>{b}</Tag>) : "—"),
                  },
                ]}
              />
            ),
          },
          {
            key: "bundles",
            label: t("套件"),
            children: (
              <Table<CatalogBundle>
                pagination={PAGINATION}
                rowKey="name"
                loading={data.loading}
                dataSource={(data.data?.bundles ?? []).filter((r) =>
                  matches(r, kw, ["name", "description", "capability_id"]),
                )}
                expandable={{
                  expandedRowRender: (r) => (
                    <Typography.Paragraph style={{ margin: 0 }}>
                      {t("成员")}：{r.tools.join(", ") || "—"}
                      <br />
                      {r.description}
                    </Typography.Paragraph>
                  ),
                }}
                columns={[
                  { title: t("套件名"), dataIndex: "name" },
                  { title: t("版本"), dataIndex: "version", width: 90 },
                  {
                    title: t("成员数"),
                    width: 90,
                    render: (_, r) => r.tools.length,
                  },
                  { title: t("说明"), dataIndex: "description", ellipsis: true },
                ]}
              />
            ),
          },
        ]}
      />
    </PageCard>
  );
}
