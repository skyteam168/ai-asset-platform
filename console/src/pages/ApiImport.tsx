import { Alert, Button, Checkbox, Drawer, Form, Input, Radio, Select, Space, Steps, Table, Tag, Typography, Upload } from "antd";
import { useState } from "react";
import { post } from "../api";
import { JsonView, useOrgs } from "../components";
import { useAction } from "../hooks";
import type { Connection, ImporterResult } from "../types";
import { t } from "../i18n";

type Format = "openapi" | "postman";

interface Draft {
  capability_id: string;
  name: string;
  description: string;
  risk_level: string;
  connector_ref: { connector: string; operation: string };
}

/** 猜文档格式：Postman Collection 有 info._postman_id 或 schema 指向 getpostman。 */
export function guessFormat(text: string): Format {
  return /"_postman_id"|schema\.getpostman\.com/.test(text) ? "postman" : "openapi";
}

/** API 导入：先 persist=false 预览 Tool 草稿，再选系统连接与归属组织 persist=true 落库（H2 / H3）。 */
export function ApiImportDrawer(p: { open: boolean; connections: Connection[]; onClose: () => void; onDone: () => void }) {
  const [form] = Form.useForm();
  const { list } = useOrgs();
  const act = useAction();
  const [preview, setPreview] = useState<ImporterResult | null>(null);
  const [result, setResult] = useState<ImporterResult | null>(null);
  const [busy, setBusy] = useState(false);
  const enrich = Form.useWatch("enrich_descriptions", form) as boolean | undefined;
  const step = result ? 2 : preview ? 1 : 0;

  const run = async (persist: boolean) => {
    const v = await form.validateFields();
    if (persist && !v.connection_id) {
      form.setFields([{ name: "connection_id", errors: [t("落库前须选择系统连接")] }]);
      return;
    }
    const body = {
      document: v.document, connector_name: v.connector_name, domain: v.domain, system: v.system, persist,
      connection_id: persist ? v.connection_id : null, owner_org_id: persist ? v.owner_org_id ?? null : null,
      enrich_descriptions: Boolean(v.enrich_descriptions), model_profile: v.enrich_descriptions ? v.model_profile : null,
    };
    setBusy(true);
    const r = await act(() => post<ImporterResult>(`/importer/${v.format}`, body), persist ? t("已落库为草稿") : undefined);
    setBusy(false);
    if (!r) return;
    if (persist) { setResult(r); p.onDone(); } else setPreview(r);
  };

  const close = () => { setPreview(null); setResult(null); p.onClose(); };
  const tools = (preview?.tools ?? []) as unknown as Draft[];
  return (
    <Drawer open={p.open} width={980} title={t("API 导入：OpenAPI / Postman → 声明式 Connector 与 Tool 草稿")} onClose={close} destroyOnClose>
      <Steps size="small" current={step} style={{ marginBottom: 16 }}
        items={[{ title: t("粘贴或上传文档") }, { title: t("预览草稿") }, { title: t("落库") }]} />
      <Form form={form} layout="vertical" preserve={false}
        initialValues={{ format: "openapi", domain: "imported", system: "imported", enrich_descriptions: false }}
        onValuesChange={(changed) => { if ("document" in changed || "format" in changed || "connector_name" in changed) { setPreview(null); setResult(null); } }}>
        <Space style={{ display: "flex" }} align="start" wrap>
          <Form.Item name="format" label={t("格式")}>
            <Radio.Group optionType="button" options={[{ value: "openapi", label: "OpenAPI 3.x" }, { value: "postman", label: "Postman v2.x" }]} />
          </Form.Item>
          <Form.Item name="connector_name" label={t("Connector 名称")} style={{ width: 220 }}
            rules={[{ required: true, pattern: /^[a-z][a-z0-9_]{2,63}$/, message: t("小写字母开头，仅含 a-z / 0-9 / _") }]}>
            <Input placeholder="mes_vn01" />
          </Form.Item>
          <Form.Item name="domain" label={t("业务域")} rules={[{ required: true, pattern: /^[a-z][a-z0-9_]*$/, message: t("小写标识") }]}>
            <Input style={{ width: 140 }} placeholder="production" />
          </Form.Item>
          <Form.Item name="system" label={t("系统")} rules={[{ required: true, pattern: /^[a-z][a-z0-9_]*$/, message: t("小写标识") }]}>
            <Input style={{ width: 140 }} placeholder="mes" />
          </Form.Item>
        </Space>
        <Form.Item name="document" label={t("文档（JSON 或 YAML）")} rules={[{ required: true }]}
          extra={(
            <Upload accept=".json,.yaml,.yml" showUploadList={false}
              beforeUpload={async (file) => {
                const text = await file.text();
                form.setFieldsValue({ document: text, format: guessFormat(text) });
                setPreview(null);
                return false;
              }}>
              <Button size="small" style={{ marginTop: 8 }}>{t("从文件读取")}</Button>
            </Upload>
          )}>
          <Input.TextArea rows={8} spellCheck={false} style={{ fontFamily: "Consolas, monospace", fontSize: 12 }}
            placeholder={t("粘贴 OpenAPI 3.x 或 Postman Collection v2.x 文档")} />
        </Form.Item>
        <Space align="start" wrap>
          <Form.Item name="enrich_descriptions" valuePropName="checked">
            <Checkbox>{t("经 LLM Gateway 生成候选描述（需人工确认）")}</Checkbox>
          </Form.Item>
          {enrich && (
            <Form.Item name="model_profile" rules={[{ required: true, pattern: /^[\w.-]+@\d+\.\d+\.\d+$/, message: "name@x.y.z" }]}>
              <Input style={{ width: 240 }} placeholder="gpt-4o-mini@1.0.0" />
            </Form.Item>
          )}
        </Space>
        <Button type="primary" loading={busy && !preview} onClick={() => run(false)}>{t("预览草稿")}</Button>

        {preview && (
          <div style={{ marginTop: 16 }}>
            {preview.warnings.length > 0 && (
              <Alert type="warning" showIcon style={{ marginBottom: 12 }} message={t("导入提示（{0}）", [preview.warnings.length])}
                description={preview.warnings.map((w) => <div key={w}>· {w}</div>)} />
            )}
            <Table<Draft> rowKey="capability_id" size="small" pagination={false} dataSource={tools}
              expandable={{ expandedRowRender: (d) => <JsonView value={d} /> }}
              columns={[
                { title: "capability_id", dataIndex: "capability_id", render: (v: string) => <Typography.Text code>{v}</Typography.Text> },
                { title: t("风险"), dataIndex: "risk_level", render: (v: string) => <Tag color={v === "read" ? "green" : "orange"}>{v}</Tag> },
                { title: t("描述"), dataIndex: "description", ellipsis: true },
              ]} />
            <Typography.Paragraph type="secondary" style={{ marginTop: 8 }}>{t("将生成 {0} 个 Tool 草稿（版本 0.1.0，draft）与 1 个声明式 Connector 实例", [tools.length])}</Typography.Paragraph>
            <details style={{ marginBottom: 12 }}>
              <summary>{t("Connector 配置")}</summary>
              <JsonView value={preview.connector_config} />
            </details>
            <Space align="start" wrap>
              <Form.Item name="connection_id" label={t("系统连接")} style={{ width: 360 }}>
                <Select options={p.connections.map((c) => ({ value: c.id, label: `${c.system_key}（${c.env}） · ${c.base_url}` }))} />
              </Form.Item>
              <Form.Item name="owner_org_id" label={t("归属组织（缺省为连接所属公司）")} style={{ width: 320 }}>
                <Select allowClear options={list.filter((o) => o.type !== "group").map((o) => ({ value: o.id, label: `${o.code} · ${o.name}` }))} />
              </Form.Item>
            </Space>
            <div><Button type="primary" loading={busy} disabled={Boolean(result)} onClick={() => run(true)}>{t("落库为草稿")}</Button></div>
          </div>
        )}
      </Form>
      {result && (
        <Alert type="success" showIcon style={{ marginTop: 16 }}
          message={t("已创建 Connector 实例与 {0} 个 Tool 草稿，到「资产与门禁」补齐用例后提交 G0", [result.drafts?.length ?? 0])}
          description={(result.drafts ?? []).map((d) => <div key={d.version_id}><Typography.Text code>{d.capability_id}</Typography.Text> · {d.state}</div>)} />
      )}
    </Drawer>
  );
}
