// SKILL.md 预览用的极简 Markdown 渲染：只产出 React 元素（不拼 HTML），外链只渲染 http(s)，避免引入新依赖与 XSS 面。
import { Typography } from "antd";
import type { ReactNode } from "react";

type Block =
  | { kind: "heading"; level: number; text: string }
  | { kind: "code"; lang: string; text: string }
  | { kind: "list"; ordered: boolean; items: string[] }
  | { kind: "quote"; text: string }
  | { kind: "table"; rows: string[][] }
  | { kind: "rule" }
  | { kind: "para"; text: string };

const LIST_ITEM = /^\s*([-*+]|\d+[.)])\s+(.*)$/;
const TABLE_SEP = /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$/;

export function parseBlocks(src: string): Block[] {
  const lines = src.replace(/\r\n?/g, "\n").split("\n");
  const out: Block[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i += 1; continue; }
    const fence = line.match(/^\s*(```|~~~)\s*([\w-]*)/);
    if (fence) {
      const body: string[] = [];
      i += 1;
      while (i < lines.length && !lines[i].trim().startsWith(fence[1])) { body.push(lines[i]); i += 1; }
      out.push({ kind: "code", lang: fence[2], text: body.join("\n") });
      i += 1;
      continue;
    }
    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) { out.push({ kind: "heading", level: h[1].length, text: h[2].replace(/\s+#+\s*$/, "") }); i += 1; continue; }
    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) { out.push({ kind: "rule" }); i += 1; continue; }
    if (line.trim().startsWith("|") && i + 1 < lines.length && TABLE_SEP.test(lines[i + 1])) {
      const rows: string[][] = [];
      const cells = (l: string) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
      rows.push(cells(line));
      i += 2;
      while (i < lines.length && lines[i].trim().startsWith("|")) { rows.push(cells(lines[i])); i += 1; }
      out.push({ kind: "table", rows });
      continue;
    }
    if (line.trim().startsWith(">")) {
      const body: string[] = [];
      while (i < lines.length && lines[i].trim().startsWith(">")) { body.push(lines[i].trim().replace(/^>\s?/, "")); i += 1; }
      out.push({ kind: "quote", text: body.join(" ") });
      continue;
    }
    const li = line.match(LIST_ITEM);
    if (li) {
      const ordered = /\d/.test(li[1]);
      const items: string[] = [];
      while (i < lines.length) {
        const m = lines[i].match(LIST_ITEM);
        if (m) { items.push(m[2]); i += 1; continue; }
        if (lines[i].trim() && /^\s{2,}/.test(lines[i]) && items.length) { items[items.length - 1] += ` ${lines[i].trim()}`; i += 1; continue; }
        break;
      }
      out.push({ kind: "list", ordered, items });
      continue;
    }
    const para: string[] = [];
    while (i < lines.length && lines[i].trim() && !/^(#{1,6}\s|\s*```|\s*~~~|\s*>)/.test(lines[i]) && !LIST_ITEM.test(lines[i])) {
      para.push(lines[i].trim());
      i += 1;
    }
    out.push({ kind: "para", text: para.join(" ") });
  }
  return out;
}

const INLINE = /(`[^`]+`)|(\*\*[^*]+\*\*)|(\*[^*\s][^*]*\*|_[^_\s][^_]*_)|(\[[^\]]+\]\([^)\s]+\))/g;

export function inline(text: string): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  for (const m of text.matchAll(INLINE)) {
    const at = m.index ?? 0;
    if (at > last) out.push(text.slice(last, at));
    const tok = m[0];
    const key = `${at}`;
    if (m[1]) out.push(<Typography.Text key={key} code>{tok.slice(1, -1)}</Typography.Text>);
    else if (m[2]) out.push(<strong key={key}>{inline(tok.slice(2, -2))}</strong>);
    else if (m[3]) out.push(<em key={key}>{inline(tok.slice(1, -1))}</em>);
    else {
      const [, label, href] = tok.match(/^\[([^\]]+)\]\(([^)\s]+)\)$/) ?? [];
      out.push(/^https?:\/\//i.test(href ?? "")
        ? <a key={key} href={href} target="_blank" rel="noopener noreferrer">{label}</a>
        : <span key={key} title={href}>{label}</span>);
    }
    last = at + tok.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

export function Markdown({ source }: { source: string }) {
  return (
    <div className="aip-markdown">
      {parseBlocks(source).map((b, n) => {
        switch (b.kind) {
          case "heading": {
            const level = Math.min(b.level, 5) as 1 | 2 | 3 | 4 | 5;
            return <Typography.Title key={n} level={level}>{inline(b.text)}</Typography.Title>;
          }
          case "code":
            return <pre key={n} className="aip-markdown-code"><code>{b.text}</code></pre>;
          case "list": {
            const items = b.items.map((x, k) => <li key={k}>{inline(x)}</li>);
            return b.ordered ? <ol key={n}>{items}</ol> : <ul key={n}>{items}</ul>;
          }
          case "quote":
            return <blockquote key={n}>{inline(b.text)}</blockquote>;
          case "table":
            return (
              <table key={n}>
                <thead><tr>{b.rows[0].map((c, k) => <th key={k}>{inline(c)}</th>)}</tr></thead>
                <tbody>{b.rows.slice(1).map((r, k) => <tr key={k}>{r.map((c, j) => <td key={j}>{inline(c)}</td>)}</tr>)}</tbody>
              </table>
            );
          case "rule":
            return <hr key={n} />;
          default:
            return <Typography.Paragraph key={n}>{inline(b.text)}</Typography.Paragraph>;
        }
      })}
    </div>
  );
}
