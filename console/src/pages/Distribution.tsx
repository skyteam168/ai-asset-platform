import { Alert, Table, Tabs, Tag, Typography } from "antd";
import { useState } from "react";
import { get } from "../api";
import { matches, PageCard, PAGINATION, Toolbar } from "../components";
import { useLoad } from "../hooks";
import { t } from "../i18n";

interface SkillRow {
  name: string;
  version: string;
  description?: string;
  retracted?: boolean;
}

/** 套件 / Skill / webhook 管理入口（列表只读；写操作走对应 API）。 */
export function DistributionPage() {
  const skills = useLoad(() => get<{ items: SkillRow[] }>("/skills"), []);
  const [kw, setKw] = useState("");
  return (
    <PageCard>
      <Toolbar placeholder={t("Skill / 能力")} onSearch={setKw} />
      <Tabs
        items={[
          {
            key: "skills",
            label: t("Skill"),
            children: (
              <>
                {skills.error && <Alert type="error" message={skills.error} style={{ marginBottom: 12 }} />}
                <Table<SkillRow>
                  pagination={PAGINATION}
                  rowKey={(r) => `${r.name}@${r.version}`}
                  loading={skills.loading}
                  dataSource={(skills.data?.items ?? []).filter((r) =>
                    matches(r, kw, ["name", "description", "version"]),
                  )}
                  columns={[
                    {
                      title: t("能力"),
                      dataIndex: "name",
                      render: (v: string, r) => (
                        <span>
                          <Typography.Text strong>{v}</Typography.Text>
                          <Typography.Text type="secondary"> @{r.version}</Typography.Text>
                        </span>
                      ),
                    },
                    { title: t("描述"), dataIndex: "description", render: (v: string | undefined) => v || "-" },
                    {
                      title: t("状态"),
                      dataIndex: "retracted",
                      width: 100,
                      render: (v: boolean | undefined) =>
                        v ? <Tag color="default">{t("已撤回")}</Tag> : <Tag color="green">{t("有效")}</Tag>,
                    },
                  ]}
                />
              </>
            ),
          },
          {
            key: "webhooks",
            label: t("Webhook"),
            children: (
              <Alert
                type="info"
                showIcon
                message={t("Webhook 按消费方管理：在「消费方与绑定」中打开对应消费方后配置回调 URL 与事件订阅。")}
                description={
                  <Typography.Paragraph type="secondary" style={{ marginBottom: 0 }}>
                    {t("投递签名为 HMAC-SHA256；密钥轮换后旧密钥立即失效。")}
                  </Typography.Paragraph>
                }
              />
            ),
          },
          {
            key: "bundles",
            label: t("套件"),
            children: (
              <Alert
                type="info"
                showIcon
                message={t("套件列表见「资产目录」页；授权与发布在「资产与门禁」。")}
              />
            ),
          },
        ]}
      />
    </PageCard>
  );
}
