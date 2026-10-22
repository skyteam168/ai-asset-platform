import { Alert, Descriptions, Typography } from "antd";
import { useParams } from "react-router-dom";
import { get } from "../api";
import { JsonView, PageCard, StateTag } from "../components";
import { ListTable } from "../ListTable";
import { useLoad } from "../hooks";
import type { RunView } from "../types";
import { t } from "../i18n";

export function RunsPage() {
  const { id } = useParams<{ id: string }>();
  const data = useLoad(() => (id ? get<RunView>(`/runs/${id}`) : Promise.reject(new Error("missing"))), [id]);
  const view = data.data;
  return (
    <PageCard>
      {data.error && <Alert type="error" message={data.error} style={{ marginBottom: 12 }} />}
      {!id && <Alert type="warning" message={t("请从待办中的 Run 链接进入，或在地址栏填写 /runs/:id")} />}
      {view && (
        <>
          <Descriptions bordered size="small" column={2} style={{ marginBottom: 16 }}>
            <Descriptions.Item label={t("Run")}>{view.run_id}</Descriptions.Item>
            <Descriptions.Item label={t("状态")}>
              <StateTag state={view.status} />
            </Descriptions.Item>
            <Descriptions.Item label={t("能力")}>
              <Typography.Text code>
                {view.capability_id}@{view.version}
              </Typography.Text>
            </Descriptions.Item>
            <Descriptions.Item label={t("当前节点")}>{view.current}</Descriptions.Item>
            <Descriptions.Item label={t("污点")}>{view.tainted ? t("是") : t("否")}</Descriptions.Item>
            <Descriptions.Item label="trace">{view.trace_id || "-"}</Descriptions.Item>
          </Descriptions>
          <Typography.Title level={5}>{t("步骤")}</Typography.Title>
          <ListTable
            size="small"
            pagination={false}
            rowKey="node_id"
            loading={data.loading}
            dataSource={view.steps ?? []}
            columns={[
              { title: t("节点"), dataIndex: "node_id" },
              { title: t("类型"), dataIndex: "kind", width: 100 },
              {
                title: t("状态"),
                dataIndex: "status",
                width: 100,
                render: (v: string) => <StateTag state={v} />,
              },
              { title: t("尝试"), dataIndex: "attempts", width: 70 },
            ]}
            style={{ marginBottom: 16 }}
          />
          {view.result != null && (
            <>
              <Typography.Title level={5}>{t("结果")}</Typography.Title>
              <JsonView value={view.result} />
            </>
          )}
          {view.error != null && (
            <>
              <Typography.Title level={5}>{t("错误")}</Typography.Title>
              <JsonView value={view.error} />
            </>
          )}
        </>
      )}
    </PageCard>
  );
}
