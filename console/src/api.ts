// 控制面 API 客户端：Bearer 会话令牌、RFC 9457 错误解析（docs/api/conventions.md）
const BASE = "/api/v1";
const TOKEN_KEY = "aip.session";

export interface FieldError {
  field: string;
  code: string;
  message: string;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    public detail: string,
    public errors: FieldError[] = [],
  ) {
    super(`${code}: ${detail}`);
  }
}

export interface Page<T> {
  items: T[];
  next_cursor: string | null;
  has_more: boolean;
}

export const session = {
  get: (): string | null => sessionStorage.getItem(TOKEN_KEY),
  set: (token: string) => sessionStorage.setItem(TOKEN_KEY, token),
  clear: () => sessionStorage.removeItem(TOKEN_KEY),
};

let onUnauthorized: () => void = () => undefined;
export function setUnauthorizedHandler(fn: () => void) {
  onUnauthorized = fn;
}

export async function api<T>(method: string, path: string, body?: unknown): Promise<T> {
  return request<T>(method, path, body === undefined ? undefined : JSON.stringify(body), "application/json");
}

/** multipart 上传（如 Skill zip）；Content-Type 由浏览器带上 boundary。 */
export function upload<T>(path: string, file: Blob, filename: string, field = "file"): Promise<T> {
  const form = new FormData();
  form.append(field, file, filename);
  return request<T>("POST", path, form);
}

async function request<T>(method: string, path: string, body?: BodyInit, contentType?: string): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  const token = session.get();
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined && contentType) headers["Content-Type"] = contentType;
  const res = await fetch(`${BASE}${path}`, { method, headers, body });
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  const data = text ? JSON.parse(text) : undefined;
  if (!res.ok) {
    const err = new ApiError(res.status, data?.code ?? "INTERNAL", data?.detail ?? res.statusText, data?.errors ?? []);
    if (res.status === 401 && path !== "/auth/login") onUnauthorized();
    throw err;
  }
  return data as T;
}

export const get = <T,>(path: string) => api<T>("GET", path);
export const post = <T,>(path: string, body?: unknown) => api<T>("POST", path, body ?? {});
export const put = <T,>(path: string, body: unknown) => api<T>("PUT", path, body);
export const del = <T = void>(path: string) => api<T>("DELETE", path);

export function describeError(e: unknown): string {
  if (e instanceof ApiError) {
    const fields = e.errors.map((f) => `${f.field}：${f.message}`).join("；");
    return fields ? `${e.detail}（${fields}）` : `${e.detail}（${e.code}）`;
  }
  return e instanceof Error ? e.message : String(e);
}

export function query(params: Record<string, string | number | undefined | null>): string {
  const q = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== "") q.set(k, String(v));
  });
  const s = q.toString();
  return s ? `?${s}` : "";
}
