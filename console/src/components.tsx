import { Button, Input, Tag, Typography } from "antd";
import type { Rule } from "antd/es/form";
import type { TablePaginationConfig } from "antd/es/table";
import React, { createContext, useContext, useMemo, useState, type ReactNode } from "react";
import { get, type Page } from "./api";
import { useLoad } from "./hooks";
import type { OrgUnit } from "./types";
import { t } from "./i18n";

// ---------- 组织字典：把 id 显示成编码与名称 ----------

interface Orgs {
  list: OrgUnit[];
  byId: Map<string, OrgUnit>;
  companies: OrgUnit[];
  reload: () => void;
}

const OrgContext = createContext<Orgs>({ list: [], byId: new Map(), companies: [], reload: () => undefined });

export function OrgProvider({ children }: { children: ReactNode }) {
  const { data, reload } = useLoad(() => get<Page<OrgUnit>>("/orgs"), []);
  const value = useMemo<Orgs>(() => {
    const list = data?.items ?? [];
    return { list, byId: new Map(list.map((o) => [o.id, o])), companies: list.filter((o) => o.type === "company"),
      reload };
  }, [data, reload]);
  return <OrgContext.Provider value={value}>{children}</OrgContext.Provider>;
}

export const useOrgs = () => useContext(OrgContext);

export function OrgName({ id }: { id: string | null | undefined }) {
  const { byId } = useOrgs();
  if (!id) return <>-</>;
  const o = byId.get(id);
  return o ? <span title={o.id}>{o.code} · {o.name}</span> : <Typography.Text type="secondary">{id.slice(0, 8)}…</Typography.Text>;
}

// ---------- 展示 ----------

export function JsonView({ value }: { value: unknown }) {
  return <pre className="aip-json-view">{JSON.stringify(value, null, 2)}</pre>;
}

export function Clearance({ level }: { level: number | null | undefined }) {
  if (!level) return <>-</>;
  const color = ["", "default", "blue", "orange", "red"][level];
  return <Tag color={color}>L{level}</Tag>;
}

const STATE_COLOR: Record<string, string> = {
  draft: "default", testing: "processing", in_review: "gold", approved: "cyan", canary: "purple", active: "green",
  deprecated: "default", retired: "default", suspended: "red", pending_approval: "gold", disabled: "red",
  passed: "green", failed: "red", pending: "gold", running: "processing", waiting: "gold",
  succeeded: "green", compensated: "orange", rejected: "red", cancelled: "default", started: "processing",
  done: "green",
};

const STATE_LABEL: Record<string, string> = {
  draft: t("草稿"),
  testing: t("测试中"),
  in_review: t("评审中"),
  approved: t("已批准"),
  canary: t("灰度中"),
  active: t("已生效"),
  deprecated: t("已弃用"),
  retired: t("已退役"),
  suspended: t("已熔断"),
  pending_approval: t("待审批"),
  disabled: t("已停用"),
  passed: t("通过"),
  failed: t("失败"),
  pending: t("待处理"),
  running: t("运行中"),
  waiting: t("等待中"),
  succeeded: t("成功"),
  compensated: t("已补偿"),
  rejected: t("已驳回"),
  cancelled: t("已取消"),
  started: t("执行中"),
  done: t("完成"),
};

/** 状态标签：显示译名，原始状态码放在提示里便于排查 */
export function StateTag({ state }: { state: string }) {
  return (
    <Tag color={STATE_COLOR[state] ?? "default"} title={state}>
      {STATE_LABEL[state] ?? state}
    </Tag>
  );
}

/** 门禁证据：有 cases 时用表格（避免把「期望失败」误解成门失败）；其余仍走 JSON。 */
export function GateEvidence({ value }: { value: unknown }) {
  const evidence = value && typeof value === "object" ? (value as Record<string, unknown>) : null;
  const cases = Array.isArray(evidence?.cases) ? (evidence.cases as Record<string, unknown>[]) : null;
  if (!cases?.length) return <JsonView value={value} />;
  return (
    <div className="aip-gate-evidence">
      <table className="aip-gate-cases">
        <thead>
          <tr>
            <th>{t("用例")}</th>
            <th>{t("类型")}</th>
            <th>{t("用例结果")}</th>
            <th>{t("执行状态")}</th>
            <th>{t("说明")}</th>
          </tr>
        </thead>
        <tbody>
          {cases.map((c, i) => {
            const passed = Boolean(c.passed);
            const status = String(c.status ?? "-");
            const kind = String(c.kind ?? "-");
            const note =
              kind === "permission" && passed && status === "failed"
                ? t("期望被拒绝（权限用例通过）")
                : String(c.detail || c.error_code || "");
            return (
              <tr key={`${String(c.name)}-${i}`}>
                <td>{String(c.name ?? "-")}</td>
                <td>{kind}</td>
                <td><StateTag state={passed ? "passed" : "failed"} /></td>
                <td><StateTag state={status} /></td>
                <td className="aip-gate-cases-note">{note || "—"}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <details className="aip-gate-evidence-raw">
        <summary>{t("原始证据 JSON")}</summary>
        <JsonView value={value} />
      </details>
    </div>
  );
}

/** 页面级表格的统一分页：显示总数、可切换每页条数 */
export const PAGINATION: TablePaginationConfig = {
  showTotal: (n: number) => t("共 {0} 条", [n]),
  showSizeChanger: true,
  defaultPageSize: 20,
  pageSizeOptions: [10, 20, 50, 100],
};

// ---------- JSON 输入：表单里填写 JSON，提交时解析 ----------

export const jsonRule: Rule = {
  validator: async (_, v: string | undefined) => {
    if (!v) return;
    try {
      JSON.parse(v);
    } catch {
      throw new Error(t("不是合法的 JSON"));
    }
  },
};

// id 由 Form.Item 注入：让标签与输入框关联（读屏与按标签定位都依赖它）
export function JsonInput(props: { id?: string; value?: string; onChange?: (v: string) => void; rows?: number }) {
  return (
    <Input.TextArea
      id={props.id}
      value={props.value}
      onChange={(e) => props.onChange?.(e.target.value)}
      rows={props.rows ?? 6}
      style={{ fontFamily: "Consolas, monospace", fontSize: 12 }}
      spellCheck={false}
    />
  );
}

export const parseJson = (v: string | undefined, fallback: unknown = {}) => (v ? JSON.parse(v) : fallback);

/** 页面卡片：与集团其他中台一致的白色（深色模式下深灰）圆角容器 */
export function PageCard({ children, style }: { children: React.ReactNode; style?: React.CSSProperties }) {
  return (
    <div className="aip-card" style={style}>
      {children}
    </div>
  );
}

/** 列表页工具栏：关键字筛选 + 查询 + 右侧动作（新建等） */
export function Toolbar(p: {
  placeholder?: string;
  onSearch?: (keyword: string) => void;
  actions?: React.ReactNode;
  extra?: React.ReactNode;
}) {
  const [kw, setKw] = useState("");
  return (
    <div className="aip-toolbar">
      {p.onSearch && (
        <>
          <Input className="aip-toolbar-search" allowClear placeholder={p.placeholder} value={kw}
            onChange={(e) => {
              setKw(e.target.value);
              if (!e.target.value) p.onSearch?.("");
            }}
            onPressEnter={() => p.onSearch?.(kw.trim())} />
          <Button type="primary" onClick={() => p.onSearch?.(kw.trim())}>{t("查询")}</Button>
        </>
      )}
      {p.actions}
      <span className="aip-toolbar-spacer" />
      {p.extra}
    </div>
  );
}

/** 按关键字过滤（任一字段包含即命中，不区分大小写） */
export function matches<T>(row: T, keyword: string, fields: (keyof T)[]): boolean {
  if (!keyword) return true;
  const k = keyword.toLowerCase();
  return fields.some((f) => String(row[f] ?? "").toLowerCase().includes(k));
}
