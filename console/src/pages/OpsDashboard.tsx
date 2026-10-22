/** 运营概览（H47）：漂移与开放风险摘要。 */
import { Alert, Card, Col, Row, Spin, Statistic, Typography } from "antd";
import { get } from "../api";
import { PageCard } from "../components";
import { ListTable } from "../ListTable";
import { useLoad } from "../hooks";
import { t } from "../i18n";

interface OpsSummary {
  active_drifts: number;
  open_risk_events: number;
  recent_risk_events: { id: string; rule_code: string; severity: string; status: string; created_at: string | null }[];
  drift_items: { version_id: string; capability_id: string; reason: string; marked_at: string | null }[];
}

export function OpsDashboardPage() {
  const { data, loading, error } = useLoad(() => get<OpsSummary>("/console/ops-summary"), []);
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
      <Row gutter={16} style={{ marginBottom: 16 }}>
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
