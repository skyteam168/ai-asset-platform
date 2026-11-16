/** 资产市场（H48）：已发布能力浏览；MCP/调用可见性仍以 Grant + PDP 为准。 */
import { Alert, Tag, Tooltip, Typography } from "antd";
import { useMemo, useState } from "react";
import { get } from "../api";
import {
  Clearance,
  matchOrgSystem,
  matches,
  OrgName,
  OrgSystemFilter,
  PageCard,
  PAGINATION,
  Toolbar,
} from "../components";
import { ListTable } from "../ListTable";
import { useLoad } from "../hooks";
import { t } from "../i18n";

interface CatalogTool {
  capability_id: string;
  version: string;
  title: string;
  description: string;
  risk_level: string;
  egress_class?: string;
  data_classification?: number | null;
  bundles?: string[];
  connector?: string | null;
  system_key?: string | null;
  system_host?: string | null;
  company_id?: string | null;
  owner_org_id?: string | null;
  active_grant_count?: number;
}

interface CatalogData {
  items: CatalogTool[];
}

const RISK_COLOR: Record<string, string> = { read: "green", write: "orange", danger: "red" };

/** 表格内截断文案；悬停 Tooltip 展示全文，便于运维阅读。 */
function EllipsisWithTip({
  text,
  maxWidth = 360,
  secondary = false,
}: {
  text?: string;
  maxWidth?: number;
  secondary?: boolean;
}) {
  const value = (text || "").trim();
  if (!value) return <Typography.Text type="secondary">—</Typography.Text>;
  return (
    <Tooltip title={value} overlayStyle={{ maxWidth: 480 }} overlayInnerStyle={{ whiteSpace: "pre-wrap" }}>
      <Typography.Text type={secondary ? "secondary" : undefined} ellipsis style={{ maxWidth, display: "block" }}>
        {value}
      </Typography.Text>
    </Tooltip>
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

export function MarketplacePage() {
  const [kw, setKw] = useState("");
  const [companyId, setCompanyId] = useState<string | null>(null);
  const [systemKey, setSystemKey] = useState<string | null>(null);
  // 一次拉全量后前端筛公司/系统/关键字（与资产目录一致；服务端 ?q= 仍可供 API 调用方使用）
  const { data, error, loading } = useLoad(() => get<CatalogData>("/console/catalog"), []);
  const items = useMemo(() => data?.items ?? [], [data]);
  const rowSystemKeys = useMemo(() => items.map((r) => r.system_key), [items]);
  const filtered = useMemo(
    () =>
      items.filter(
        (r) =>
          matchOrgSystem(r, companyId, systemKey) &&
          matches(r, kw, [
            "capability_id",
            "title",
            "description",
            "connector",
            "system_key",
            "system_host",
            "risk_level",
          ]),
      ),
    [items, companyId, systemKey, kw],
  );

  return (
    <PageCard>
      <Typography.Title level={4} style={{ marginTop: 0 }}>
        {t("资产市场")}
      </Typography.Title>
      <Typography.Paragraph type="secondary" style={{ marginBottom: 8 }}>
        {t("列出已发布 Active 的能力（与资产目录同源）。本页不是「已授权清单」：消费方经 MCP/REST 能看到并调用哪些 Tool，取决于对该消费方的 Grant 与调用时 Principal（用户断言、密级、数据范围），由 PDP 默认拒绝。")}
      </Typography.Paragraph>
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message={t("MCP 暴露说明")}
        description={t("审核通过并 Active 且暴露含 mcp 的 Tool，不会自动对所有接入方可见。A 部门 AI 平台用自己的消费方凭据连接 MCP 时，tools/list / tools/call 只返回该 Principal 通过授权判定的能力；未 Grant 的 Tool 既不可见也不可调。有效授权列为本资产当前未过期 Grant 条数，便于运维核对下放情况。")}
      />
      {error ? <Alert type="error" message={error} style={{ marginBottom: 16 }} /> : null}
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
      <ListTable
        rowKey={(r) => `${r.capability_id}@${r.version}`}
        loading={loading}
        dataSource={filtered}
        locale={{ emptyText: t("无匹配资产：可放宽公司或系统筛选") }}
        columns={[
          {
            title: t("能力"),
            dataIndex: "capability_id",
            width: 220,
            render: (v: string, r: CatalogTool) => (
              <span>
                <Typography.Text strong>{v}</Typography.Text>
                <Typography.Text type="secondary"> @{r.version}</Typography.Text>
              </span>
            ),
          },
          {
            title: t("说明"),
            key: "desc",
            render: (_: unknown, r: CatalogTool) => (
              <div style={{ maxWidth: 280, minWidth: 140 }}>
                {r.title ? <EllipsisWithTip text={r.title} maxWidth={280} /> : null}
                {r.description ? <EllipsisWithTip text={r.description} maxWidth={280} secondary /> : null}
              </div>
            ),
          },
          {
            title: t("公司"),
            dataIndex: "company_id",
            width: 140,
            render: (v: string | null | undefined) => <OrgName id={v} />,
          },
          {
            title: t("归属"),
            dataIndex: "owner_org_id",
            width: 150,
            render: (v: string | null | undefined) => <OrgName id={v} />,
          },
          {
            title: t("系统"),
            key: "system",
            width: 150,
            render: (_: unknown, r: CatalogTool) => (
              <SystemCell systemKey={r.system_key} host={r.system_host} />
            ),
          },
          {
            title: t("连接器"),
            dataIndex: "connector",
            width: 120,
            render: (v: string | null | undefined) => v || "—",
          },
          {
            title: t("风险"),
            dataIndex: "risk_level",
            width: 80,
            render: (v: string) => <Tag color={RISK_COLOR[v] || "default"}>{v}</Tag>,
          },
          {
            title: t("密级"),
            dataIndex: "data_classification",
            width: 70,
            render: (v: number | null | undefined) => <Clearance level={v} />,
          },
          {
            title: t("外发"),
            dataIndex: "egress_class",
            width: 80,
            render: (v: string | undefined) =>
              v === "external" ? <Tag color="red">{v}</Tag> : v || "—",
          },
          {
            title: t("有效授权"),
            dataIndex: "active_grant_count",
            width: 100,
            render: (v: number | undefined) => {
              const n = v ?? 0;
              return (
                <Tooltip title={t("当前未过期且 status=active 的 Grant 条数；不等于你此刻一定能调用。")}>
                  {n > 0 ? <Tag color="blue">{n}</Tag> : <Typography.Text type="secondary">0</Typography.Text>}
                </Tooltip>
              );
            },
          },
          {
            title: t("所属套件"),
            dataIndex: "bundles",
            width: 120,
            render: (v: string[] | undefined) =>
              v?.length ? v.map((b) => <Tag key={b}>{b}</Tag>) : "—",
          },
        ]}
        pagination={PAGINATION}
      />
    </PageCard>
  );
}
