import { DownloadOutlined } from "@ant-design/icons";
import { Alert, Button, message, Table, Tabs, Tag, Typography } from "antd";
import { useState } from "react";
import { describeError, downloadFile, get } from "../api";
import { matches, PageCard, PAGINATION, Toolbar } from "../components";
import { useLoad } from "../hooks";
import { t } from "../i18n";

interface SkillRow {
  name: string;
  version: string;
  description?: string;
  retracted?: boolean;
  git_url?: string | null;
}

/** 套件 / Skill / webhook 管理入口；Skill 支持目录浏览与 zip 下载。 */
export function DistributionPage() {
  const skills = useLoad(() => get<{ items: SkillRow[] }>("/skills"), []);
  const [kw, setKw] = useState("");
  const [busy, setBusy] = useState<string | null>(null);

  async function onDownload(r: SkillRow) {
    const key = `${r.name}@${r.version}`;
    setBusy(key);
    try {
      await downloadFile(
        `/skills/${encodeURIComponent(r.name)}/versions/${encodeURIComponent(r.version)}/download`,
        `${r.name}@${r.version}.zip`,
      );
      message.success(t("已开始下载 {0}", [key]));
    } catch (e) {
      message.error(describeError(e));
    } finally {
      setBusy(null);
    }
  }

  return (
    <PageCard>
      <Toolbar placeholder={t("Skill / 能力")} onSearch={setKw} />
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message={t("下载 Skill zip 后可用 corp-skill install，或解压到 Cursor / Claude 的 skills 目录。")}
      />
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
                    {
                      title: t("操作"),
                      key: "actions",
                      width: 120,
                      render: (_: unknown, r) => (
                        <Button
                          type="link"
                          size="small"
                          icon={<DownloadOutlined />}
                          disabled={Boolean(r.retracted)}
                          loading={busy === `${r.name}@${r.version}`}
                          onClick={() => onDownload(r)}
                        >
                          {t("下载 zip")}
                        </Button>
                      ),
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
