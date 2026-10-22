/** 运营概览：漂移 / 风险（H47）+ 接入管道（O4）。 */
import { Alert, Card, Col, Row, Spin, Statistic, Typography } from "antd";
import { Link } from "react-router-dom";
import { get } from "../api";
import { PageCard } from "../components";
import { ListTable } from "../ListTable";
import { useLoad } from "../hooks";
import { t } from "../i18n";

interface PipelineCounts {
  draft: number;
  gating: number;
  published: number;
  granted: number;
}

interface PipelineSystemRow extends PipelineCounts {
  system_key: string;
}

interface PipelineCompanyRow extends PipelineCounts {
  company_id: string;
  company_code: string;
  company_name: string;
  by_system: PipelineSystemRow[];
}

interface OpsSummary {
  active_drifts: number;
  open_risk_events: number;
  recent_risk_events: { id: string; rule_code: string; severity: string; status: string; created_at: string | null }[];
  drift_items: { version_id: string; capability_id: string; reason: string; marked_at: string | null }[];
  pipeline?: {
    totals: PipelineCounts;
    active_connections: number;
    by_company: PipelineCompanyRow[];
  };
}

const STAGE_COLS = [
  { title: t("草稿"), dataIndex: "draft" as const, width: 80 },
  { title: t("门禁中"), dataIndex: "gating" as const, width: 80 },
  { title: t("已发布"), dataIndex: "published" as const, width: 80 },
  { title: t("已授权"), dataIndex: "granted" as const, width: 80 },
];

export function OpsDashboardPage() {
  const { data, loading, error } = useLoad(() => get<OpsSummary>("/console/ops-summary"), []);
  const pipe = data?.pipeline;
  if (loading && !data) {
    return (
      <PageCard>
        <Spin />
      </PageCard>
    );
  }
  return (
    <PageCard>
      {error ? <Alert type="error" message={error} style={{ marginBottom: 16 }} /> : null}
      <Typography.Title level={4} style={{ marginTop: 0 }}>
        {t("运营概览")}
      </Typography.Title>

      <Typography.Title level={5}>{t("接入管道")}</Typography.Title>
      <Typography.Paragraph type="secondary" style={{ marginTop: 0 }}>
        {t("按公司 / 系统统计 Tool：草稿 → 门禁（测试/评审/已批准待发）→ 已发布 → 已授权。每个资产只计最远阶段。")}
        {" "}
        <Link to="/wizard">{t("创建 Tool 向导")}</Link>
      </Typography.Paragraph>
      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col span={4}>
          <Card size="small">
            <Statistic title={t("活跃连接")} value={pipe?.active_connections ?? 0} />
          </Card>
        </Col>
        <Col span={5}>
          <Card size="small">
            <Statistic title={t("草稿")} value={pipe?.totals.draft ?? 0} />
          </Card>
        </Col>
        <Col span={5}>
          <Card size="small">
            <Statistic title={t("门禁中")} value={pipe?.totals.gating ?? 0} />
          </Card>
        </Col>
        <Col span={5}>
          <Card size="small">
            <Statistic title={t("已发布")} value={pipe?.totals.published ?? 0} />
          </Card>
        </Col>
        <Col span={5}>
          <Card size="small">
            <Statistic title={t("已授权")} value={pipe?.totals.granted ?? 0} />
          </Card>
        </Col>
      </Row>
      <ListTable
        rowKey="company_id"
        size="small"
        pagination={false}
        dataSource={pipe?.by_company ?? []}
        expandable={{
          expandedRowRender: (row: PipelineCompanyRow) => (
            <ListTable
              rowKey="system_key"
              size="small"
              pagination={false}
              dataSource={row.by_system}
              columns={[
                { title: t("系统"), dataIndex: "system_key" },
                ...STAGE_COLS,
              ]}
            />
          ),
        }}
        columns={[
          {
            title: t("公司"),
            key: "company",
            render: (_, r: PipelineCompanyRow) => `${r.company_code}${r.company_name ? ` · ${r.company_name}` : ""}`,
          },
          ...STAGE_COLS,
        ]}
      />

      <Row gutter={16} style={{ marginTop: 24, marginBottom: 16 }}>
        <Col span={8}>
          <Card>
            <Statistic title={t("契约漂移")} value={data?.active_drifts ?? 0} />
          </Card>
        </Col>
        <Col span={8}>
          <Card>
            <Statistic title={t("开放风险事件")} value={data?.open_risk_events ?? 0} />
          </Card>
        </Col>
      </Row>
      <Typography.Title level={5}>{t("活跃漂移")}</Typography.Title>
      <ListTable
        rowKey="version_id"
        dataSource={data?.drift_items ?? []}
        columns={[
          { title: t("能力"), dataIndex: "capability_id" },
          { title: t("原因"), dataIndex: "reason", ellipsis: true },
          { title: t("标记时间"), dataIndex: "marked_at", width: 200 },
        ]}
        pagination={false}
        size="small"
      />
      <Typography.Title level={5} style={{ marginTop: 24 }}>
        {t("近期风险")}
      </Typography.Title>
      <ListTable
        rowKey="id"
        dataSource={data?.recent_risk_events ?? []}
        columns={[
          { title: t("规则"), dataIndex: "rule_code" },
          { title: t("严重度"), dataIndex: "severity", width: 100 },
          { title: t("状态"), dataIndex: "status", width: 120 },
          { title: t("时间"), dataIndex: "created_at", width: 200 },
        ]}
        pagination={false}
        size="small"
      />
    </PageCard>
  );
}
