import { Alert, Table, Tabs, Tag, Tooltip, Typography } from "antd";
import { useMemo, useState } from "react";
import { get } from "../api";
import { matchOrgSystem, matches, OrgName, OrgSystemFilter, PageCard, PAGINATION, Toolbar } from "../components";
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
  connector?: string | null;
  system_key?: string | null;
  system_host?: string | null;
  company_id?: string | null;
  owner_org_id?: string | null;
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

function DescCell({ title, description }: { title?: string; description?: string }) {
  const desc = (description || "").trim();
  const head = (title || "").trim();
  if (!head && !desc) return <Typography.Text type="secondary">—</Typography.Text>;
  return (
    <div style={{ maxWidth: 360, minWidth: 160 }}>
      {head ? (
        <Typography.Text ellipsis style={{ maxWidth: 360, display: "block" }}>
          {head}
        </Typography.Text>
      ) : null}
      {desc ? (
        <Tooltip title={desc}>
          <Typography.Text type="secondary" ellipsis style={{ maxWidth: 360, display: "block", fontSize: 12 }}>
            {desc}
          </Typography.Text>
        </Tooltip>
      ) : null}
    </div>
  );
}

function SystemCell({ systemKey, host }: { systemKey?: string | null; host?: string | null }) {
  if (!systemKey && !host) return <Typography.Text type="secondary">—</Typography.Text>;
  return (
    <div>
      <div>{systemKey || "—"}</div>
      {host ? (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {host}
        </Typography.Text>
      ) : null}
    </div>
  );
}

export function CatalogPage() {
  const data = useLoad(() => get<CatalogData>("/console/catalog"), []);
  const [kw, setKw] = useState("");
  const [companyId, setCompanyId] = useState<string | null>(null);
  const [systemKey, setSystemKey] = useState<string | null>(null);
  const items = data.data?.items ?? [];
  const rowSystemKeys = useMemo(() => items.map((r) => r.system_key), [items]);
  const filteredTools = useMemo(
    () =>
      items.filter(
        (r) =>
          matchOrgSystem(r, companyId, systemKey) &&
          matches(r, kw, ["capability_id", "title", "description", "connector", "system_key", "system_host"]),
      ),
    [items, companyId, systemKey, kw],
  );
  return (
    <PageCard>
      <Toolbar
        placeholder={t("能力 / 套件 / 描述 / 连接器 / 系统")}
        onSearch={setKw}
        filters={
          <OrgSystemFilter
            companyId={companyId}
            systemKey={systemKey}
            onCompany={setCompanyId}
            onSystem={setSystemKey}
            rowSystemKeys={rowSystemKeys}
          />
        }
      />
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
                dataSource={filteredTools}
                locale={{ emptyText: t("无匹配资产：可放宽公司或系统筛选") }}
                columns={[
                  {
                    title: t("能力"),
                    dataIndex: "capability_id",
                    width: 260,
                    render: (v: string, r) => (
                      <span>
                        <Typography.Text strong>{v}</Typography.Text>
                        <Typography.Text type="secondary"> @{r.version}</Typography.Text>
                      </span>
                    ),
                  },
                  {
                    title: t("说明"),
                    key: "desc",
                    render: (_, r) => <DescCell title={r.title} description={r.description} />,
                  },
                  {
                    title: t("连接器"),
                    dataIndex: "connector",
                    width: 140,
                    render: (v: string | null | undefined) => v || "—",
                  },
                  {
                    title: t("系统"),
                    key: "system",
                    width: 160,
                    render: (_, r) => <SystemCell systemKey={r.system_key} host={r.system_host} />,
                  },
                  { title: t("公司"), dataIndex: "company_id", width: 130, render: (v) => <OrgName id={v} /> },
                  { title: t("归属"), dataIndex: "owner_org_id", width: 140, render: (v) => <OrgName id={v} /> },
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
