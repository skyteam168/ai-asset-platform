/** Prism Mock 会话列表（H49 薄层）。 */
import { Alert, Button, Space, Typography } from "antd";
import { del, get } from "../api";
import { PageCard, PAGINATION } from "../components";
import { ListTable } from "../ListTable";
import { useAction, useLoad } from "../hooks";
import { t } from "../i18n";

interface MockItem {
  id: string;
  base_url: string;
  container_name: string;
  expires_at: string;
}

export function PrismMocksPage() {
  const { data, error, reload } = useLoad(() => get<{ items: MockItem[] }>("/prism-mocks"), []);
  const run = useAction();
  return (
    <PageCard>
      <Typography.Title level={4} style={{ marginTop: 0 }}>
        {t("Prism Mock")}
      </Typography.Title>
      <Typography.Paragraph type="secondary">
        {t("本控制面进程内的 Mock 会话；重启后失效。创建请用 API 或导入流程。")}
      </Typography.Paragraph>
      {error ? <Alert type="error" message={error} style={{ marginBottom: 16 }} /> : null}
      <ListTable
        rowKey="id"
        dataSource={data?.items ?? []}
        columns={[
          {
            title: t("基址"),
            dataIndex: "base_url",
            render: (u: string) => (
              <Typography.Link href={u} target="_blank" rel="noreferrer">
                {u}
              </Typography.Link>
            ),
          },
          { title: t("容器"), dataIndex: "container_name", width: 200 },
          { title: t("到期"), dataIndex: "expires_at", width: 200 },
          {
            title: t("操作"),
            width: 120,
            render: (_: unknown, row: MockItem) => (
              <Space>
                <Button
                  size="small"
                  danger
                  onClick={() => run(async () => { await del(`/prism-mocks/${row.id}`); reload(); }, t("已停止"))}
                >
                  {t("停止")}
                </Button>
              </Space>
            ),
          },
        ]}
        pagination={PAGINATION}
      />
    </PageCard>
  );
}
