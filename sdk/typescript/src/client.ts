import { SdkError } from "./errors.js";

export const JWT_BEARER = "urn:ietf:params:oauth:grant-type:jwt-bearer";
export const CLIENT_ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer";

export type JsonObject = Record<string, unknown>;
export type FetchLike = typeof fetch;

export interface ClientOptions {
  accessToken?: string;
  staticToken?: string;
  clientId?: string;
  fetch?: FetchLike;
  timeoutMs?: number;
}

export interface AuthOpts {
  userAssertion?: string;
  idempotencyKey?: string;
}

export interface ExchangeOpts {
  clientAssertion?: string;
  resource?: string;
  grantType?: string;
}

export interface InvokeOpts extends AuthOpts {
  version?: string;
  channel?: string;
}

export interface ConfirmOpts {
  comment?: string;
  args?: JsonObject;
}

export interface RejectOpts {
  assertion?: string;
  comment?: string;
}

/** Async consumer client for data-plane REST (architecture.md 10.5). */
export class Client {
  readonly baseUrl: string;
  accessToken?: string;
  staticToken?: string;
  clientId?: string;
  private readonly fetchImpl: FetchLike;
  private readonly timeoutMs: number;

  constructor(baseUrl: string, opts: ClientOptions = {}) {
    this.baseUrl = baseUrl.replace(/\/+$/, "");
    this.accessToken = opts.accessToken;
    this.staticToken = opts.staticToken;
    this.clientId = opts.clientId;
    this.fetchImpl = opts.fetch ?? globalThis.fetch.bind(globalThis);
    this.timeoutMs = opts.timeoutMs ?? 30_000;
  }

  private bearer(): string {
    const tok = this.accessToken || this.staticToken;
    if (!tok) {
      throw new SdkError("UNAUTHENTICATED", "未设置 accessToken 或 staticToken");
    }
    return tok;
  }

  private authHeaders(opts: AuthOpts = {}): Record<string, string> {
    const h: Record<string, string> = { Authorization: `Bearer ${this.bearer()}` };
    if (opts.userAssertion) h["X-User-Assertion"] = opts.userAssertion;
    if (opts.idempotencyKey) h["Idempotency-Key"] = opts.idempotencyKey;
    return h;
  }

  private async request(
    method: string,
    path: string,
    init: {
      headers?: Record<string, string>;
      json?: JsonObject;
      form?: Record<string, string>;
    } = {},
  ): Promise<unknown> {
    const headers: Record<string, string> = { ...(init.headers ?? {}) };
    let body: string | undefined;
    if (init.json !== undefined) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(init.json);
    } else if (init.form !== undefined) {
      headers["Content-Type"] = "application/x-www-form-urlencoded";
      body = new URLSearchParams(init.form).toString();
    }

    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), this.timeoutMs);
    let res: Response;
    try {
      res = await this.fetchImpl(`${this.baseUrl}${path}`, {
        method,
        headers,
        body,
        signal: ctrl.signal,
      });
    } finally {
      clearTimeout(timer);
    }

    if (res.status >= 400) {
      throw await toSdkError(res);
    }
    if (res.status === 204) return null;
    const text = await res.text();
    if (!text) return null;
    const ctype = res.headers.get("content-type") ?? "";
    if (ctype.includes("application/json") || text.startsWith("{") || text.startsWith("[")) {
      return JSON.parse(text) as unknown;
    }
    return text;
  }

  async exchangeToken(assertion: string, opts: ExchangeOpts = {}): Promise<JsonObject> {
    const form: Record<string, string> = {
      grant_type: opts.grantType ?? JWT_BEARER,
      assertion,
    };
    if (opts.resource) form.resource = opts.resource;

    const headers: Record<string, string> = {};
    if (opts.clientAssertion) {
      if (!this.clientId) {
        throw new SdkError("INVALID_ARGUMENT", "使用 clientAssertion 时需要 clientId");
      }
      form.client_id = this.clientId;
      form.client_assertion_type = CLIENT_ASSERTION_TYPE;
      form.client_assertion = opts.clientAssertion;
    } else if (this.staticToken) {
      headers.Authorization = `Bearer ${this.staticToken}`;
    } else {
      throw new SdkError("UNAUTHENTICATED", "需要 staticToken 或 clientAssertion");
    }

    const body = (await this.request("POST", "/oauth/token", { headers, form })) as JsonObject;
    const tok = body.access_token;
    if (typeof tok === "string") this.accessToken = tok;
    return body;
  }

  async catalog(opts: AuthOpts = {}): Promise<JsonObject> {
    return (await this.request("GET", "/api/v1/catalog", {
      headers: this.authHeaders(opts),
    })) as JsonObject;
  }

  async invoke(
    capabilityId: string,
    args: JsonObject = {},
    opts: InvokeOpts = {},
  ): Promise<JsonObject> {
    const payload: JsonObject = { args, channel: opts.channel ?? "stable" };
    if (opts.version !== undefined) payload.version = opts.version;
    return (await this.request("POST", `/api/v1/invoke/${encodeURIComponent(capabilityId)}`, {
      headers: this.authHeaders(opts),
      json: payload,
    })) as JsonObject;
  }

  async getExecution(executionId: string, opts: AuthOpts = {}): Promise<JsonObject> {
    return (await this.request("GET", `/api/v1/executions/${encodeURIComponent(executionId)}`, {
      headers: this.authHeaders(opts),
    })) as JsonObject;
  }

  async getRun(runId: string, opts: AuthOpts = {}): Promise<JsonObject> {
    return (await this.request("GET", `/api/v1/runs/${encodeURIComponent(runId)}`, {
      headers: this.authHeaders(opts),
    })) as JsonObject;
  }

  async confirm(
    confirmationId: string,
    assertion: string,
    opts: ConfirmOpts = {},
  ): Promise<JsonObject> {
    const payload: JsonObject = { assertion };
    if (opts.comment !== undefined) payload.comment = opts.comment;
    if (opts.args !== undefined) payload.args = opts.args;
    return (await this.request(
      "POST",
      `/api/v1/confirmations/${encodeURIComponent(confirmationId)}/confirm`,
      { headers: this.authHeaders(), json: payload },
    )) as JsonObject;
  }

  async reject(confirmationId: string, opts: RejectOpts = {}): Promise<JsonObject> {
    const payload: JsonObject = {};
    if (opts.assertion !== undefined) payload.assertion = opts.assertion;
    if (opts.comment !== undefined) payload.comment = opts.comment;
    return (await this.request(
      "POST",
      `/api/v1/confirmations/${encodeURIComponent(confirmationId)}/reject`,
      { headers: this.authHeaders(), json: payload },
    )) as JsonObject;
  }
}

async function toSdkError(res: Response): Promise<SdkError> {
  let code = `HTTP_${res.status}`;
  let detail = "";
  let body: Record<string, unknown> = {};
  const text = await res.text();
  detail = text.slice(0, 500);
  try {
    const parsed = JSON.parse(text) as unknown;
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      body = parsed as Record<string, unknown>;
      if (body.code != null) code = String(body.code);
      if (body.detail != null) detail = String(body.detail);
    }
  } catch {
    /* keep text detail */
  }
  return new SdkError(code, detail, { status: res.status, body });
}
