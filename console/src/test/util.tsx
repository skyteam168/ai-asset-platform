
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";
import { AuthProvider } from "../auth";
import { ThemeProvider } from "../theme";

type Route = (method: string, path: string, body: unknown) => { status?: number; body?: unknown } | undefined;

/** 用路由函数模拟控制面接口；未匹配的请求返回 404，记录全部调用。 */
export function mockApi(route: Route) {
  const calls: { method: string; path: string; body: unknown }[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = String(input).replace(/^\/api\/v1/, "");
    const method = init?.method ?? "GET";
    const body = init?.body instanceof FormData ? init.body : init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ method, path: url, body });
    const r = route(method, url, body) ?? { status: 404, body: { code: "NOT_FOUND", detail: "未模拟" } };
    return new Response(r.body === undefined ? null : JSON.stringify(r.body), { status: r.status ?? 200 });
  });
  return calls;
}

export function wrap(ui: ReactNode, path = "/") {
  return (
    <ThemeProvider>
      <AuthProvider>
        <MemoryRouter initialEntries={[path]}>{ui}</MemoryRouter>
      </AuthProvider>
    </ThemeProvider>
  );
}

export const page = <T,>(items: T[]) => ({ body: { items, next_cursor: null, has_more: false } });
