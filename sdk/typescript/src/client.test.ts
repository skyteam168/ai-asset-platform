import { describe, expect, it, vi } from "vitest";
import { Client, JWT_BEARER } from "./client.js";
import { SdkError } from "./errors.js";

function jsonResponse(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...headers },
  });
}

describe("Client", () => {
  it("catalog and invoke with static token", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const auth = (init?.headers as Record<string, string>)?.Authorization;
      expect(auth).toBe("Bearer aipk_test");
      if (url.endsWith("/api/v1/catalog")) {
        return jsonResponse(200, {
          items: [{ capability_id: "production.mes.query_work_orders", input_schema: {} }],
        });
      }
      if (url.includes("/api/v1/invoke/")) {
        expect(init?.method).toBe("POST");
        expect((init?.headers as Record<string, string>)["Idempotency-Key"]).toBe("k1");
        const payload = JSON.parse(String(init?.body)) as { args: Record<string, string> };
        expect(payload.args.factory_id).toBe("F001");
        return jsonResponse(200, { status: "succeeded", execution_id: "e1" });
      }
      if (url.includes("/api/v1/executions/")) {
        return jsonResponse(200, { status: "succeeded", execution_id: "e1" });
      }
      return jsonResponse(404, { code: "NOT_FOUND", detail: "missing" });
    });

    const c = new Client("http://dp", { staticToken: "aipk_test", fetch: fetchMock as typeof fetch });
    const cat = await c.catalog();
    expect((cat.items as { capability_id: string }[])[0].capability_id).toBe(
      "production.mes.query_work_orders",
    );
    const out = await c.invoke(
      "production.mes.query_work_orders",
      { factory_id: "F001" },
      { idempotencyKey: "k1" },
    );
    expect(out.status).toBe("succeeded");
    const got = await c.getExecution("e1");
    expect(got.execution_id).toBe("e1");
  });

  it("exchangeToken stores accessToken", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      expect(init?.method).toBe("POST");
      expect((init?.headers as Record<string, string>).Authorization).toBe("Bearer aipk_static");
      expect(String(init?.body)).toContain(`grant_type=${encodeURIComponent(JWT_BEARER)}`);
      expect(String(init?.body)).toContain("assertion=user-jwt");
      return jsonResponse(200, {
        access_token: "plat_tok",
        token_type: "Bearer",
        expires_in: 3600,
      });
    });

    const c = new Client("http://dp", { staticToken: "aipk_static", fetch: fetchMock as typeof fetch });
    const tok = await c.exchangeToken("user-jwt");
    expect(tok.token_type).toBe("Bearer");
    expect(c.accessToken).toBe("plat_tok");
  });

  it("raises SdkError from Problem Details", async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse(404, { code: "NOT_FOUND", detail: "执行记录不存在" }),
    );
    const c = new Client("http://dp", { accessToken: "tok", fetch: fetchMock as typeof fetch });
    await expect(c.getExecution("missing")).rejects.toMatchObject({
      name: "SdkError",
      code: "NOT_FOUND",
      status: 404,
      detail: "执行记录不存在",
    } satisfies Partial<SdkError>);
  });

  it("confirm and reject", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/confirm")) {
        const body = JSON.parse(String(init?.body)) as { assertion: string };
        expect(body.assertion).toBe("assert-1");
        return jsonResponse(200, { status: "succeeded" });
      }
      if (url.endsWith("/reject")) {
        return jsonResponse(200, { status: "rejected" });
      }
      return jsonResponse(500, { code: "INTERNAL", detail: "x" });
    });
    const c = new Client("http://dp", { accessToken: "tok", fetch: fetchMock as typeof fetch });
    expect((await c.confirm("c1", "assert-1")).status).toBe("succeeded");
    expect((await c.reject("c1", { comment: "no" })).status).toBe("rejected");
  });
});
