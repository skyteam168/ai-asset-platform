import { Alert, Button, Col, Descriptions, Empty, Form, Input, List, Modal, Row, Select, Space, Tabs, Typography, Upload } from "antd";
import { useEffect, useMemo, useState } from "react";
import { ApiError, get, put, upload, type Page } from "../api";
import { useAction, useLoad } from "../hooks";
import { Markdown } from "../markdown";
import type { SkillSource, SkillSourceSaved, VersionSummary } from "../types";
import { t } from "../i18n";

// 与后端 package.py 保持一致：首期只发指令型 Skill（architecture.md 6.2、10.4）
const BLOCKED = [".py", ".sh", ".bash", ".ps1", ".js", ".mjs", ".cjs", ".exe", ".bat", ".cmd"];
const REFERENCE_SUFFIX = [".md", ".markdown", ".txt", ".json", ".yaml", ".yml", ".csv"];
const MAX_ASSET = 1024 * 1024;
const SEGMENT = /^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$/;
const TOOL_REF = /^[a-z0-9_.]+@\d+\.\d+\.\d+$/;

export const isBlocked = (path: string) => BLOCKED.some((s) => path.toLowerCase().endsWith(s));

/** 校验 references/ 或 assets/ 下的文件名；返回错误文案，合法时返回 null。 */
export function checkFileName(name: string, kind: "references" | "assets"): string | null {
  if (!name.split("/").every((seg) => SEGMENT.test(seg))) return t("文件名只能包含字母、数字、点、下划线和连字符");
  if (isBlocked(name)) return t("首期不接受脚本文件（.py / .sh / .js 等）");
  if (kind === "references" && !REFERENCE_SUFFIX.some((s) => name.toLowerCase().endsWith(s))) {
    return t("references/ 只接受文本文件（md、txt、json、yaml、csv）");
  }
  return null;
}

const bodyTemplate = (name: string) => [
  `# ${name}`,
  "",
  `## ${t("何时使用")}`,
  "",
  t("说明在什么场景下应当启用本 Skill。"),
  "",
  `## ${t("步骤")}`,
  "",
  `1. ${t("确认调用者所属工厂与查询范围。")}`,
  `2. ${t("调用引用的 Tool，并按返回结果汇总。")}`,
  "",
  `## ${t("注意事项")}`,
  "",
  `- ${t("只读取授权范围内的数据，不要尝试绕过权限。")}`,
  "",
].join("\n");

function toBase64(file: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).replace(/^data:[^,]*,/, ""));
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

interface Props {
  versionId: string;
  assetId: string;
  spec: Record<string, unknown>;
  editable: boolean;
  onSaved: () => void;
}

/** Skill 版本面板：在线编辑 SKILL.md、Tool 引用与附带文件，服务端组装并签名；上传 zip 为次要入口。 */
export function SkillPackagePanel(p: Props) {
  const [reload, setReload] = useState(0);
  const source = useLoad(async () => {
    try {
      return await get<SkillSource>(`/versions/${p.versionId}/skill-source`);
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) return null;
      throw e;
    }
  }, [p.versionId, reload]);
  if (source.loading && !source.data) return null;
  if (source.error) return <Alert type="error" message={source.error} />;
  // 上传 zip 会整体替换包：重新读取并重建编辑器；在线保存只通知上层刷新版本信息
  const replaced = () => { setReload((n) => n + 1); p.onSaved(); };
  if (!p.editable) return <SkillReadOnly source={source.data} />;
  return <SkillEditor key={`${reload}`} {...p} initial={source.data} onReplaced={replaced} />;
}

function SkillReadOnly({ source }: { source: SkillSource | null }) {
  if (!source) return <Empty description={t("尚未上传或保存 Skill 包")} />;
  return (
    <Space direction="vertical" style={{ width: "100%" }}>
      <Descriptions size="small" column={2} bordered>
        <Descriptions.Item label="name">{source.name}</Descriptions.Item>
        <Descriptions.Item label="license">{source.license ?? "-"}</Descriptions.Item>
        <Descriptions.Item label="description" span={2}>{source.description}</Descriptions.Item>
        <Descriptions.Item label="compatibility" span={2}>{source.compatibility ?? "-"}</Descriptions.Item>
        <Descriptions.Item label="allowed-tools" span={2}>{source.allowed_tools.join(" ") || "-"}</Descriptions.Item>
        <Descriptions.Item label={t("引用的 Tool")} span={2}>{source.tools.join("，") || "-"}</Descriptions.Item>
        <Descriptions.Item label={t("内容哈希")} span={2}><Typography.Text code>{source.content_sha256 ?? "-"}</Typography.Text></Descriptions.Item>
      </Descriptions>
      <Markdown source={source.body} />
      <List size="small" header={t("附带文件")} dataSource={[...source.references.map((f) => f.path), ...source.assets.map((f) => f.path)]}
        renderItem={(x) => <List.Item><Typography.Text code>{x}</Typography.Text></List.Item>} locale={{ emptyText: t("无") }} />
    </Space>
  );
}

interface FileText { path: string; content: string }
interface FileBin { path: string; content_base64: string; size?: number | null }

function SkillEditor(p: Props & { initial: SkillSource | null; onReplaced: () => void }) {
  const [form] = Form.useForm();
  const act = useAction();
  const specName = typeof p.spec.name === "string" ? p.spec.name : "";
  const [refs, setRefs] = useState<FileText[]>(p.initial?.references ?? []);
  const [assets, setAssets] = useState<FileBin[]>(p.initial?.assets ?? []);
  const [active, setActive] = useState<string | null>(p.initial?.references[0]?.path ?? null);
  const [newRef, setNewRef] = useState("");
  const [saved, setSaved] = useState<SkillSourceSaved | null>(null);
  const [notice, setNotice] = useState<string[]>(p.initial?.warnings ?? []);
  const body = Form.useWatch("body", form) as string | undefined;
  const catalog = useLoad(() => get<{ items: { capability_id: string | null; version: string | null; title?: string | null }[] }>("/console/catalog")
    .catch(() => ({ items: [] })), []);
  const versions = useLoad(() => get<Page<VersionSummary>>(`/assets/${p.assetId}/versions`), [p.assetId]);
  const toolOptions = useMemo(() => (catalog.data?.items ?? [])
    .filter((x) => x.capability_id && x.version)
    .map((x) => ({ value: `${x.capability_id}@${x.version}`, label: `${x.capability_id}@${x.version}${x.title ? ` · ${x.title}` : ""}` })), [catalog.data]);

  const fill = (s: SkillSource | null) => {
    form.setFieldsValue(s ? {
      name: s.name, description: s.description, license: s.license ?? "", compatibility: s.compatibility ?? "",
      allowed_tools: s.allowed_tools, tools: s.tools, body: s.body,
    } : {
      name: specName, description: typeof p.spec.description === "string" ? p.spec.description : "", license: "", compatibility: "",
      allowed_tools: [], tools: Array.isArray(p.spec.tools) ? p.spec.tools : [], body: bodyTemplate(specName || "my-skill"),
    });
    setRefs(s?.references ?? []);
    setAssets(s?.assets ?? []);
    setActive(s?.references[0]?.path ?? null);
  };
  useEffect(() => { fill(p.initial); }, []); // 只在首次挂载时用已入库内容填表

  const loadFrom = async (versionId: string) => {
    const s = await act(() => get<SkillSource>(`/versions/${versionId}/skill-source`));
    if (s) { fill(s); setNotice(s.warnings); }
  };

  const addRef = () => {
    const name = newRef.trim();
    const err = checkFileName(name, "references");
    if (err) { Modal.error({ title: err }); return; }
    const path = `references/${name}`;
    if (refs.some((r) => r.path === path)) { Modal.error({ title: t("文件已存在：{0}", [path]) }); return; }
    setRefs([...refs, { path, content: "" }]);
    setActive(path);
    setNewRef("");
  };

  const addAsset = async (file: File) => {
    const err = checkFileName(file.name, "assets");
    if (err) { Modal.error({ title: err }); return false; }
    if (file.size > MAX_ASSET) { Modal.error({ title: t("附件不能超过 1MB") }); return false; }
    const path = `assets/${file.name}`;
    const content_base64 = await toBase64(file);
    setAssets((xs) => [...xs.filter((x) => x.path !== path), { path, content_base64, size: file.size }]);
    return false;
  };

  const loadable = (versions.data?.items ?? []).filter((v) => v.id !== p.versionId);
  const current = refs.find((r) => r.path === active);
  return (
    <Form form={form} layout="vertical"
      onFinish={async (v) => {
        const payload = {
          name: v.name, description: v.description, license: v.license || null, compatibility: v.compatibility || null,
          allowed_tools: v.allowed_tools ?? [], tools: v.tools ?? [], body: v.body ?? "", references: refs,
          assets: assets.map(({ path, content_base64 }) => ({ path, content_base64 })),
        };
        const r = await act(() => put<SkillSourceSaved>(`/versions/${p.versionId}/skill-source`, payload), t("已保存并签名"));
        if (r) { setSaved(r); setNotice(r.warnings); p.onSaved(); }
      }}>
      <Alert type="info" showIcon style={{ marginBottom: 12 }}
        message={t("在线编辑标准 Agent Skills 目录：SKILL.md + references/ + assets/；保存时由服务端组装 zip、计算哈希并签名。首期只支持指令型 Skill，不接受脚本。")} />
      <Space wrap style={{ marginBottom: 12 }}>
        <Select style={{ width: 260 }} placeholder={t("从已有版本载入（作为新版本起点）")}
          options={loadable.map((v) => ({ value: v.id, label: `${v.semver} · ${v.state}` }))}
          onChange={(id: string) => loadFrom(id)} />
        <Upload accept=".zip" showUploadList={false}
          beforeUpload={async (file) => {
            const r = await act(() => upload<SkillSourceSaved>(`/versions/${p.versionId}/skill-package`, file, file.name), t("已上传并签名"));
            if (r) p.onReplaced();
            return false;
          }}>
          <Button>{t("上传 zip（本地编写的 Skill）")}</Button>
        </Upload>
      </Space>
      {notice.length > 0 && (
        <Alert type="warning" showIcon style={{ marginBottom: 12 }} message={t("安全扫描预检（G0 会正式判定）")}
          description={notice.map((x) => <div key={x}>· {x}</div>)} />
      )}
      {saved && (
        <Alert type="success" showIcon style={{ marginBottom: 12 }} closable onClose={() => setSaved(null)}
          message={t("已签名入库：{0} 个文件", [saved.files.length])}
          description={<Typography.Text code style={{ fontSize: 11 }}>sha256 {saved.content_sha256}</Typography.Text>} />
      )}
      <Row gutter={12}>
        <Col span={8}>
          <Form.Item name="name" label="name" tooltip={t("小写字母开头，可含 a-z、0-9、-、_；与目录名一致")}
            rules={[{ required: true, pattern: /^[a-z][a-z0-9_-]{1,63}$/, message: t("小写字母开头的 2–64 位标识") }]}>
            <Input placeholder="query-work-orders" />
          </Form.Item>
        </Col>
        <Col span={8}><Form.Item name="license" label="license"><Input placeholder="Proprietary" /></Form.Item></Col>
        <Col span={8}><Form.Item name="compatibility" label="compatibility"><Input placeholder={t("运行环境要求，可选")} /></Form.Item></Col>
      </Row>
      <Form.Item name="description" label="description" tooltip={t("模型据此判断何时启用本 Skill")}
        rules={[{ required: true, min: 10, max: 1024 }, { pattern: /^[^\n\r]*$/, message: t("须为单行文本") }]}>
        <Input.TextArea autoSize={{ minRows: 2, maxRows: 4 }} />
      </Form.Item>
      <Row gutter={12}>
        <Col span={12}>
          <Form.Item name="allowed_tools" label="allowed-tools" tooltip={t("可选；客户端预先放行的工具名，每项不含空格")}
            rules={[{ validator: async (_, v: string[] | undefined) => { if ((v ?? []).some((x) => /\s/.test(x))) throw new Error(t("每一项不能包含空白")); } }]}>
            <Select mode="tags" tokenSeparators={[" ", ","]} placeholder="Read Grep" />
          </Form.Item>
        </Col>
        <Col span={12}>
          <Form.Item name="tools" label={t("引用的 Tool@版本（写入 skill.lock.json）")}
            rules={[{ validator: async (_, v: string[] | undefined) => { const bad = (v ?? []).filter((x) => !TOOL_REF.test(x)); if (bad.length) throw new Error(t("格式须为 capability_id@x.y.z：{0}", [bad.join("，")])); } }]}>
            <Select mode="tags" options={toolOptions} placeholder="production.mes.query_work_orders@1.0.0" />
          </Form.Item>
        </Col>
      </Row>
      <Typography.Text strong>SKILL.md</Typography.Text>
      <Row gutter={12} style={{ marginTop: 8 }}>
        <Col span={12}>
          <Form.Item name="body" noStyle>
            <Input.TextArea aria-label={t("SKILL.md 正文（Markdown）")} rows={20} spellCheck={false}
              style={{ fontFamily: "Consolas, monospace", fontSize: 12 }} />
          </Form.Item>
        </Col>
        <Col span={12}><Markdown source={body ?? ""} /></Col>
      </Row>
      <Tabs style={{ marginTop: 12 }} items={[
        { key: "references", label: t("references/（{0}）", [refs.length]), children: (
          <Row gutter={12}>
            <Col span={8}>
              <Space.Compact style={{ width: "100%", marginBottom: 8 }}>
                <Input value={newRef} onChange={(e) => setNewRef(e.target.value)} placeholder="guide.md" onPressEnter={(e) => { e.preventDefault(); addRef(); }} />
                <Button onClick={addRef}>{t("新增")}</Button>
              </Space.Compact>
              <List size="small" bordered dataSource={refs} locale={{ emptyText: t("无") }}
                renderItem={(r) => (
                  <List.Item style={{ cursor: "pointer", background: r.path === active ? "var(--aip-selected)" : undefined }}
                    onClick={() => setActive(r.path)}
                    actions={[<Typography.Link key="del" type="danger" onClick={(e) => {
                      e.stopPropagation();
                      setRefs(refs.filter((x) => x.path !== r.path));
                      if (active === r.path) setActive(null);
                    }}>{t("删除")}</Typography.Link>]}>
                    <Typography.Text code>{r.path.replace(/^references\//, "")}</Typography.Text>
                  </List.Item>
                )} />
            </Col>
            <Col span={16}>
              {current ? (
                <Input.TextArea aria-label={t("文件内容：{0}", [current.path])} rows={12} spellCheck={false} value={current.content}
                  style={{ fontFamily: "Consolas, monospace", fontSize: 12 }}
                  onChange={(e) => setRefs(refs.map((x) => (x.path === current.path ? { ...x, content: e.target.value } : x)))} />
              ) : <Empty description={t("选择或新增一个参考文件")} />}
            </Col>
          </Row>
        ) },
        { key: "assets", label: t("assets/（{0}）", [assets.length]), children: (
          <>
            <Upload multiple showUploadList={false} beforeUpload={addAsset}>
              <Button>{t("添加附件（≤ 1MB，不接受脚本）")}</Button>
            </Upload>
            <List size="small" style={{ marginTop: 8 }} bordered dataSource={assets} locale={{ emptyText: t("无") }}
              renderItem={(a) => (
                <List.Item actions={[<Typography.Link key="del" type="danger" onClick={() => setAssets(assets.filter((x) => x.path !== a.path))}>{t("删除")}</Typography.Link>]}>
                  <Typography.Text code>{a.path}</Typography.Text>
                  {a.size != null && <Typography.Text type="secondary"> {(a.size / 1024).toFixed(1)} KB</Typography.Text>}
                </List.Item>
              )} />
          </>
        ) },
      ]} />
      <Button type="primary" htmlType="submit" style={{ marginTop: 12 }}>{t("保存并签名")}</Button>
    </Form>
  );
}
