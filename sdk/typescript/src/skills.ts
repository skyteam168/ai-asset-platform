import { SdkError } from "./errors.js";
import type { FetchLike, JsonObject } from "./client.js";

export interface SkillRegistryClientOptions {
  accessToken: string;
  fetch?: FetchLike;
  timeoutMs?: number;
}

/** Control-plane Skill Registry client (list / manifest / download / install-check). */
export class SkillRegistryClient {
  readonly baseUrl: string;
  accessToken: string;
  private readonly fetchImpl: FetchLike;
  private readonly timeoutMs: number;

  constructor(baseUrl: string, opts: SkillRegistryClientOptions) {
    this.baseUrl = baseUrl.replace(/\/+$/, "");
    this.accessToken = opts.accessToken;
    this.fetchImpl = opts.fetch ?? globalThis.fetch.bind(globalThis);
    this.timeoutMs = opts.timeoutMs ?? 60_000;
  }

  private headers(): Record<string, string> {
    return { Authorization: `Bearer ${this.accessToken}` };
  }

  private async request(
    method: string,
    path: string,
    init: { json?: JsonObject; params?: Record<string, string | boolean> } = {},
  ): Promise<unknown> {
    const url = new URL(this.baseUrl + path);
    if (init.params) {
      for (const [k, v] of Object.entries(init.params)) url.searchParams.set(k, String(v));
    }
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), this.timeoutMs);
    try {
      const r = await this.fetchImpl(url, {
        method,
        headers: {
          ...this.headers(),
          ...(init.json ? { "Content-Type": "application/json" } : {}),
        },
        body: init.json ? JSON.stringify(init.json) : undefined,
        signal: ctrl.signal,
      });
      if (!r.ok) {
        let body: JsonObject = {};
        const text = await r.text();
        try {
          body = JSON.parse(text) as JsonObject;
        } catch {
          /* keep empty */
        }
        throw new SdkError(
          String(body.code ?? `HTTP_${r.status}`),
          String(body.detail ?? text.slice(0, 500)),
          { status: r.status, body },
        );
      }
      if (r.status === 204) return null;
      const ctype = r.headers.get("content-type") ?? "";
      if (ctype.includes("application/json")) return r.json();
      return new Uint8Array(await r.arrayBuffer());
    } finally {
      clearTimeout(t);
    }
  }

  listSkills(opts: { q?: string; channel?: string; includeRetracted?: boolean } = {}): Promise<JsonObject> {
    return this.request("GET", "/api/v1/skills", {
      params: {
        channel: opts.channel ?? "stable",
        include_retracted: opts.includeRetracted ?? true,
        ...(opts.q ? { q: opts.q } : {}),
      },
    }) as Promise<JsonObject>;
  }

  getManifest(name: string, version: string): Promise<JsonObject> {
    return this.request(
      "GET",
      `/api/v1/skills/${encodeURIComponent(name)}/versions/${encodeURIComponent(version)}/manifest`,
    ) as Promise<JsonObject>;
  }

  async download(name: string, version: string): Promise<Uint8Array> {
    const body = await this.request(
      "GET",
      `/api/v1/skills/${encodeURIComponent(name)}/versions/${encodeURIComponent(version)}/download`,
    );
    if (!(body instanceof Uint8Array)) {
      throw new SdkError("INVALID_ARGUMENT", "下载响应不是 zip 字节");
    }
    return body;
  }

  installCheck(opts: {
    consumerId: string;
    name: string;
    version: string;
    env?: string;
  }): Promise<JsonObject> {
    return this.request("POST", "/api/v1/skills/install-check", {
      json: {
        consumer_id: opts.consumerId,
        name: opts.name,
        version: opts.version,
        env: opts.env ?? "test",
      },
    }) as Promise<JsonObject>;
  }
}
