import { Input, Tag, Typography } from "antd";
import type { Rule } from "antd/es/form";
import { createContext, useContext, useMemo, type ReactNode } from "react";
import { get, type Page } from "./api";
import { useLoad } from "./hooks";
import type { OrgUnit } from "./types";

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
  return (
    <pre style={{ background: "#fafafa", padding: 12, margin: 0, maxHeight: 360, overflow: "auto", fontSize: 12 }}>
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}

export function Clearance({ level }: { level: number | null | undefined }) {
  if (!level) return <>-</>;
  const color = ["", "default", "blue", "orange", "red"][level];
  return <Tag color={color}>L{level}</Tag>;
}

const STATE_COLOR: Record<string, string> = {
  draft: "default", testing: "processing", in_review: "gold", approved: "cyan", canary: "purple", active: "green",
  deprecated: "default", retired: "default", suspended: "red", pending_approval: "gold", disabled: "red",
  passed: "green", failed: "red", pending: "gold",
};

export function StateTag({ state }: { state: string }) {
  return <Tag color={STATE_COLOR[state] ?? "default"}>{state}</Tag>;
}

// ---------- JSON 输入：表单里填写 JSON，提交时解析 ----------

export const jsonRule: Rule = {
  validator: async (_, v: string | undefined) => {
    if (!v) return;
    try {
      JSON.parse(v);
    } catch {
      throw new Error("不是合法的 JSON");
    }
  },
};

export function JsonInput(props: { value?: string; onChange?: (v: string) => void; rows?: number }) {
  return (
    <Input.TextArea
      value={props.value}
      onChange={(e) => props.onChange?.(e.target.value)}
      rows={props.rows ?? 6}
      style={{ fontFamily: "Consolas, monospace", fontSize: 12 }}
      spellCheck={false}
    />
  );
}

export const parseJson = (v: string | undefined, fallback: unknown = {}) => (v ? JSON.parse(v) : fallback);
