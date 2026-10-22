import { BranchesOutlined, CheckCircleOutlined, CloudUploadOutlined, DownloadOutlined } from "@ant-design/icons";
import { Alert, Button, Form, message, Modal, Select, Space, Tabs, Tag, Typography } from "antd";
import { useState } from "react";
import { describeError, downloadFile, get, post, type Page } from "../api";
import { matches, PageCard, PAGINATION, Toolbar } from "../components";
import { ListTable } from "../ListTable";
import { useAction, useLoad } from "../hooks";
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
  const consumers = useLoad(() => get<Page<{ id: string; client_id: string }>>("/consumers"), []);
  const [kw, setKw] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [checkRow, setCheckRow] = useState<SkillRow | null>(null);
  const act = useAction();

  async function onDownload(r: SkillRow) {
    const key = `${r.name}@${r.version}`;
    setBusy(`dl:${key}`);
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

  async function onPublishGit(r: SkillRow) {
    const key = `git:${r.name}@${r.version}`;
    setBusy(key);
    try {
      const res = await post<{ git_url?: string }>(`/skills/${encodeURIComponent(r.name)}/versions/${encodeURIComponent(r.version)}/publish-git`);
      message.success(t("已发布到 Git"));
      if (res?.git_url) message.info(res.git_url);
      skills.reload();
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
                <ListTable<SkillRow>
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
                      title: "Git URL",
                      dataIndex: "git_url",
                      ellipsis: true,
                      render: (v: string | null | undefined) =>
                        v ? <Typography.Text copyable={{ text: v }} code style={{ fontSize: 12 }}>{v}</Typography.Text> : "-",
                    },
                    {
                      title: t("状态"),
                      dataIndex: "retracted",
                      width: 90,
                      render: (v: boolean | undefined) =>
                        v ? <Tag color="default">{t("已撤回")}</Tag> : <Tag color="green">{t("有效")}</Tag>,
                    },
                    {
                      title: t("操作"),
                      key: "actions",
                      width: 240,
                      render: (_: unknown, r) => (
                        <Space size="small">
                          <Button
                            type="link"
                            size="small"
                            icon={<DownloadOutlined />}
                            disabled={Boolean(r.retracted)}
                            loading={busy === `dl:${r.name}@${r.version}`}
                            onClick={() => onDownload(r)}
                          >
                            {t("下载 zip")}
                          </Button>
                          <Button
                            type="link"
                            size="small"
                            icon={<CheckCircleOutlined />}
                            disabled={Boolean(r.retracted)}
                            onClick={() => setCheckRow(r)}
                          >
                            {t("安装前检查")}
                          </Button>
                          <Button
                            type="link"
                            size="small"
                            icon={<BranchesOutlined />}
                            disabled={Boolean(r.retracted)}
                            loading={busy === `git:${r.name}@${r.version}`}
                            onClick={() => onPublishGit(r)}
                          >
                            {t("发布 Git")}
                          </Button>
                        </Space>
                      ),
                    },
                  ]}
                />
                <InstallCheckModal row={checkRow} consumers={consumers.data?.items ?? []} onClose={() => setCheckRow(null)} act={act} />
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

// ── 安装前检查 Modal ──────────────────────────────────────────────────────────

interface InstallCheckResult { ok: boolean; missing_grants: string[] }

function InstallCheckModal({ row, consumers, onClose, act }: {
  row: SkillRow | null;
  consumers: { id: string; client_id: string }[];
  onClose: () => void;
  act: ReturnType<typeof useAction>;
}) {
  const [form] = Form.useForm();
  const [result, setResult] = useState<InstallCheckResult | null>(null);
  const ENVS = ["test", "staging", "prod"].map((e) => ({ value: e, label: e }));
  return (
    <Modal
      open={Boolean(row)}
      title={row ? t("安装前检查 · {0}@{1}", [row.name, row.version]) : ""}
      onCancel={() => { setResult(null); form.resetFields(); onClose(); }}
      footer={null}
      destroyOnClose
    >
      <Form form={form} layout="vertical" preserve={false}
        onFinish={async (v) => {
          if (!row) return;
          const r = await act(() => post<InstallCheckResult>("/skills/install-check", {
            consumer_id: v.consumer_id, name: row.name, version: row.version, env: v.env,
          }));
          if (r) setResult(r);
        }}>
        <Form.Item name="consumer_id" label={t("消费方")} rules={[{ required: true }]}>
          <Select showSearch optionFilterProp="label" options={consumers.map((c) => ({ value: c.id, label: c.client_id }))} />
        </Form.Item>
        <Form.Item name="env" label={t("环境")} rules={[{ required: true }]} initialValue="test">
          <Select options={ENVS} />
        </Form.Item>
        <Button htmlType="submit" type="primary" icon={<CloudUploadOutlined />}>{t("检查")}</Button>
      </Form>
      {result && (
        <div style={{ marginTop: 12 }}>
          {result.ok
            ? <Alert type="success" showIcon message={t("检查通过，可以安装")} />
            : (
              <Alert type="error" showIcon message={t("缺少授权，无法安装")}
                description={result.missing_grants.map((g) => <div key={g}>· {g}</div>)} />
            )}
        </div>
      )}
    </Modal>
  );
}
