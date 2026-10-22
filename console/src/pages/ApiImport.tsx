import { Alert, Button, Checkbox, Drawer, Form, Input, Modal, Radio, Select, Space, Steps, Tag, Typography, Upload } from "antd";
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { post } from "../api";
import { JsonView, useOrgs } from "../components";
import { ListTable } from "../ListTable";
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
  input_schema?: { properties?: Record<string, unknown> };
}

interface TryResult {
  ok: boolean;
  operation: string;
  mode: string;
  connection_env?: string | null;
  item_count: number;
  truncated: boolean;
  sample: unknown;
  next_cursor?: string | null;
}

/** 猜文档格式：Postman Collection 有 info._postman_id 或 schema 指向 getpostman。 */
export function guessFormat(text: string): Format {
  return /"_postman_id"|schema\.getpostman\.com/.test(text) ? "postman" : "openapi";
}

const STEPS = [
  t("粘贴文档"),
  t("预览草稿"),
  t("真连接点测"),
  t("确认描述"),
  t("落库"),
];

/** API 导入五步向导（O1/O2）：文档 → 预览 → 真连接点测 → 确认描述 → 落库。 */
export function ApiImportDrawer(p: {
  open: boolean;
  connections: Connection[];
  onClose: () => void;
  onDone: () => void;
  /** 嵌入整页向导时用更大宽度标题 */
  embedded?: boolean;
}) {
  const [form] = Form.useForm();
  const { list } = useOrgs();
  const act = useAction();
  const [preview, setPreview] = useState<ImporterResult | null>(null);
  const [result, setResult] = useState<ImporterResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [tryBusy, setTryBusy] = useState(false);
  const [tryOp, setTryOp] = useState<string | null>(null);
  const [tryParams, setTryParams] = useState("{}");
  const [tryOut, setTryOut] = useState<TryResult | null>(null);
  const [allowWrite, setAllowWrite] = useState(false);
  const [genBusy, setGenBusy] = useState(false);
  const [genDesc, setGenDesc] = useState("");
  const [genExtras, setGenExtras] = useState<string[]>([]);
  const [genWarn, setGenWarn] = useState<string[]>([]);
  const enrich = Form.useWatch("enrich_descriptions", form) as boolean | undefined;
  const modelProfile = Form.useWatch("model_profile", form) as string | undefined;
  const connectionId = Form.useWatch("connection_id", form) as string | undefined;

  const step = useMemo(() => {
    if (result) return 4;
    if (preview && tryOut) return 3;
    if (preview && connectionId) return 2;
    if (preview) return 1;
    return 0;
  }, [preview, result, tryOut, connectionId]);

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
    if (persist) { setResult(r); p.onDone(); } else { setPreview(r); setTryOut(null); }
  };

  const runTry = async (operation: string) => {
    const connection_id = form.getFieldValue("connection_id") as string | undefined;
    if (!connection_id) {
      form.setFields([{ name: "connection_id", errors: [t("点测前须选择系统连接")] }]);
      return;
    }
    if (!preview) return;
    let params: Record<string, unknown> = {};
    try {
      params = JSON.parse(tryParams || "{}") as Record<string, unknown>;
    } catch {
      Modal.error({ title: t("参数 JSON 不合法") });
      return;
    }
    setTryBusy(true);
    setTryOp(operation);
    const out = await act(() => post<TryResult>("/importer/try-operation", {
      connection_id,
      connector_config: preview.connector_config,
      operation,
      params,
      allow_write: allowWrite,
    }));
    setTryBusy(false);
    if (out) setTryOut(out);
  };

  const close = () => {
    setPreview(null); setResult(null); setTryOut(null); setTryOp(null); setTryParams("{}"); setAllowWrite(false);
    setGenDesc(""); setGenExtras([]); setGenWarn([]);
    p.onClose();
  };

  const runGenerate = async () => {
    const v = await form.validateFields(["domain", "system", "model_profile"]).catch(() => null);
    const profile = (form.getFieldValue("model_profile") as string | undefined) || (v as { model_profile?: string } | null)?.model_profile;
    if (!profile || !/^[\w.-]+@\d+\.\d+\.\d+$/.test(profile)) {
      form.setFields([{ name: "model_profile", errors: [t("生成候选须填写 model_profile（name@x.y.z）")] }]);
      return;
    }
    if (genDesc.trim().length < 20) {
      Modal.error({ title: t("业务说明至少 20 字") });
      return;
    }
    setGenBusy(true);
    const out = await act(() => post<{
      document: string; format: string; warnings: string[]; operation_count: number; title?: string | null;
    }>("/importer/generate-openapi", {
      description: genDesc.trim(),
      model_profile: profile,
      extras: genExtras,
      domain_hint: form.getFieldValue("domain") || undefined,
      system_hint: form.getFieldValue("system") || undefined,
      owner_org_id: form.getFieldValue("owner_org_id") || undefined,
      connection_id: form.getFieldValue("connection_id") || undefined,
    }));
    setGenBusy(false);
    if (!out) return;
    form.setFieldsValue({ document: out.document, format: "openapi" });
    setPreview(null); setTryOut(null);
    setGenWarn(out.warnings ?? []);
    Modal.success({
      title: t("已生成候选 OpenAPI"),
      content: t("解析出 {0} 个 operation，已填入文档框；请继续预览与点测。", [out.operation_count]),
    });
  };

  const tools = (preview?.tools ?? []) as unknown as Draft[];
  const httpConns = p.connections.filter((c) => c.scheme === "http" || c.scheme === "https");

  const body = (
    <>
      <Steps size="small" current={step} style={{ marginBottom: 16 }}
        items={STEPS.map((title) => ({ title }))} />
      <Form form={form} layout="vertical" preserve={false}
        initialValues={{ format: "openapi", domain: "imported", system: "imported", enrich_descriptions: false }}
        onValuesChange={(changed) => {
          if ("document" in changed || "format" in changed || "connector_name" in changed) {
            setPreview(null); setResult(null); setTryOut(null);
          }
        }}>
        <Space style={{ display: "flex" }} align="start" wrap>
          <Form.Item name="format" label={t("格式")}>
            <Radio.Group optionType="button" options={[{ value: "openapi", label: t("OpenAPI / Swagger") }, { value: "postman", label: "Postman v2.x" }]} />
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
        <details style={{ marginBottom: 16 }} open={p.embedded}>
          <summary style={{ cursor: "pointer", marginBottom: 8 }}>{t("从说明生成（AI 候选 OpenAPI）")}</summary>
          <Alert type="info" showIcon style={{ marginBottom: 12 }}
            message={t("经 LLM Gateway 生成候选文档，不自动落库；可粘贴表格或截图转写文本作为附录（不解析图片）。")} />
          <Form.Item label={t("业务说明")} required>
            <Input.TextArea rows={4} value={genDesc} onChange={(e) => setGenDesc(e.target.value)}
              placeholder={t("例如：越南 MES 按工厂查询在制工单，返回工单号与计划数量…")} />
          </Form.Item>
          <Form.Item label={t("附录文本（表格 / 截图转写）")}
            extra={(
              <Upload accept=".txt,.csv,.md,.tsv,.text" showUploadList={false} multiple
                beforeUpload={async (file) => {
                  if (!/\.(txt|csv|md|tsv|text)$/i.test(file.name)) {
                    Modal.error({ title: t("仅支持文本文件（.txt / .csv / .md / .tsv）") });
                    return false;
                  }
                  const text = await file.text();
                  if (text.includes("\0")) {
                    Modal.error({ title: t("文件含二进制内容，请只上传表格或截图转写文本") });
                    return false;
                  }
                  setGenExtras((prev) => [...prev, text].slice(0, 8));
                  return false;
                }}>
                <Button size="small">{t("上传文本附录")}</Button>
              </Upload>
            )}>
            <Space direction="vertical" style={{ width: "100%" }}>
              {genExtras.map((ex, i) => (
                <Space key={i} align="start" style={{ width: "100%" }}>
                  <Typography.Text type="secondary" style={{ whiteSpace: "nowrap" }}>#{i + 1}</Typography.Text>
                  <Input.TextArea rows={2} value={ex} style={{ fontFamily: "Consolas, monospace", fontSize: 12, width: 520 }}
                    onChange={(e) => setGenExtras((prev) => prev.map((x, j) => (j === i ? e.target.value : x)))} />
                  <Button size="small" type="link" onClick={() => setGenExtras((prev) => prev.filter((_, j) => j !== i))}>{t("移除")}</Button>
                </Space>
              ))}
              {genExtras.length === 0 && (
                <Input.TextArea rows={2} placeholder={t("可粘贴接口表格、字段说明或截图 OCR 文本")}
                  onBlur={(e) => {
                    const v = e.target.value.trim();
                    if (v) { setGenExtras([v]); e.target.value = ""; }
                  }} />
              )}
            </Space>
          </Form.Item>
          <Form.Item name="model_profile" label={t("model_profile")}
            rules={[{ pattern: /^[\w.-]+@\d+\.\d+\.\d+$/, message: "name@x.y.z" }]}>
            <Input style={{ width: 240 }} placeholder="gpt-4o-mini@1.0.0" />
          </Form.Item>
          <Button loading={genBusy} onClick={() => void runGenerate()}>{t("生成 OpenAPI 候选")}</Button>
          {genWarn.length > 0 && (
            <Alert type="warning" showIcon style={{ marginTop: 12 }} message={t("生成提示")}
              description={genWarn.map((w) => <div key={w}>· {w}</div>)} />
          )}
        </details>

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
          <Input.TextArea rows={p.embedded ? 12 : 8} spellCheck={false} style={{ fontFamily: "Consolas, monospace", fontSize: 12 }}
            placeholder={t("粘贴 OpenAPI 3.x、Swagger 2.0 或 Postman Collection v2.x 文档")} />
        </Form.Item>
        <Button type="primary" loading={busy && !preview} onClick={() => run(false)}>{t("预览草稿")}</Button>

        {preview && (
          <div style={{ marginTop: 16 }}>
            {preview.warnings.length > 0 && (
              <Alert type="warning" showIcon style={{ marginBottom: 12 }} message={t("导入提示（{0}）", [preview.warnings.length])}
                description={preview.warnings.map((w) => <div key={w}>· {w}</div>)} />
            )}
            <Typography.Title level={5}>{t("预览草稿")}</Typography.Title>
            <ListTable<Draft> rowKey="capability_id" size="small" pagination={false} dataSource={tools}
              expandable={{ expandedRowRender: (d) => <JsonView value={d} /> }}
              columns={[
                { title: "capability_id", dataIndex: "capability_id", render: (v: string) => <Typography.Text code>{v}</Typography.Text> },
                { title: t("风险"), dataIndex: "risk_level", render: (v: string) => <Tag color={v === "read" ? "green" : "orange"}>{v}</Tag> },
                { title: t("描述"), dataIndex: "description", ellipsis: true },
                {
                  title: t("点测"), key: "try", width: 100,
                  render: (_, d) => (
                    <Button size="small" type="link" loading={tryBusy && tryOp === d.connector_ref.operation}
                      onClick={() => runTry(d.connector_ref.operation)}>{t("真连接试")}</Button>
                  ),
                },
              ]} />
            <Typography.Paragraph type="secondary" style={{ marginTop: 8 }}>
              {t("将生成 {0} 个 Tool 草稿（版本 0.1.0，draft）与 1 个声明式 Connector 实例", [tools.length])}
            </Typography.Paragraph>
            <details style={{ marginBottom: 12 }}>
              <summary>{t("Connector 配置")}</summary>
              <JsonView value={preview.connector_config} />
            </details>

            <Typography.Title level={5}>{t("真连接点测")}</Typography.Title>
            <Alert type="info" showIcon style={{ marginBottom: 12 }}
              message={t("选择已登记的系统连接，对单个 operation 发起真实 HTTP 调用；只读用 live_read，写操作须勾选并限 sandbox/test。")} />
            <Space align="start" wrap style={{ marginBottom: 12 }}>
              <Form.Item name="connection_id" label={t("系统连接")} style={{ width: 360 }}>
                <Select options={httpConns.map((c) => ({ value: c.id, label: `${c.system_key}（${c.env}） · ${c.base_url}` }))} />
              </Form.Item>
              <Form.Item label={t("试调参数（JSON）")} style={{ width: 360 }}>
                <Input.TextArea rows={3} value={tryParams} onChange={(e) => setTryParams(e.target.value)}
                  spellCheck={false} style={{ fontFamily: "Consolas, monospace", fontSize: 12 }} />
              </Form.Item>
              <Form.Item label=" ">
                <Checkbox checked={allowWrite} onChange={(e) => setAllowWrite(e.target.checked)}>
                  {t("允许写操作试调（sandbox/test）")}
                </Checkbox>
              </Form.Item>
            </Space>
            {tryOut && (
              <Alert
                type={tryOut.ok ? "success" : "error"}
                showIcon
                style={{ marginBottom: 12 }}
                message={t("试调结果 · {0}（{1} 条{2}）", [
                  tryOut.operation,
                  tryOut.item_count,
                  tryOut.truncated ? t("，已截断") : "",
                ])}
                description={<JsonView value={tryOut.sample} />}
              />
            )}

            <Typography.Title level={5}>{t("确认描述")}</Typography.Title>
            <Space align="start" wrap>
              <Form.Item name="enrich_descriptions" valuePropName="checked">
                <Checkbox>{t("经 LLM Gateway 生成候选描述（需人工确认；使用上方 model_profile）")}</Checkbox>
              </Form.Item>
              {enrich && !modelProfile && (
                <Typography.Text type="danger">{t("请先填写 model_profile")}</Typography.Text>
              )}
              <Form.Item name="owner_org_id" label={t("归属组织（缺省为连接所属公司）")} style={{ width: 320 }}>
                <Select allowClear options={list.filter((o) => o.type !== "group").map((o) => ({ value: o.id, label: `${o.code} · ${o.name}` }))} />
              </Form.Item>
            </Space>
            <Space>
              <Button type="primary" loading={busy} disabled={Boolean(result)} onClick={() => run(true)}>{t("落库为草稿")}</Button>
              <Typography.Text type="secondary">{t("下一步：到「资产与门禁」补齐用例后提交 G0")}</Typography.Text>
            </Space>
          </div>
        )}
      </Form>
      {result && (
        <Alert type="success" showIcon style={{ marginTop: 16 }}
          message={t("已创建 Connector 实例与 {0} 个 Tool 草稿，到「资产与门禁」补齐用例后提交 G0", [result.drafts?.length ?? 0])}
          description={(
            <>
              {(result.drafts ?? []).map((d) => (
                <div key={d.version_id}><Typography.Text code>{d.capability_id}</Typography.Text> · {d.state}</div>
              ))}
              <div style={{ marginTop: 8 }}>
                <Link to="/assets">{t("前往资产与门禁")}</Link>
                {" · "}
                <Link to="/prism-mocks">{t("一键 Lab（Prism Mock）")}</Link>
              </div>
            </>
          )} />
      )}
    </>
  );

  if (p.embedded) return body;
  return (
    <Drawer open={p.open} width={980} title={t("API 导入：OpenAPI / Swagger / Postman → 声明式 Connector 与 Tool 草稿")} onClose={close} destroyOnClose>
      {body}
    </Drawer>
  );
}
