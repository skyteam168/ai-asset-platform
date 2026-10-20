import { describe, expect, it, vi } from "vitest";
import { ApiError, describeError, get, query, session, setUnauthorizedHandler } from "./api";
import { mockApi } from "./test/util";

describe("api 客户端", () => {
  it("带上会话令牌，并把 RFC 9457 错误解析为 ApiError", async () => {
    session.set("tok-1");
    const calls = mockApi(() => ({
      status: 422,
      body: { code: "INVALID_ARGUMENT", detail: "参数不合法", errors: [{ field: "semver", code: "pattern", message: "格式错误" }] },
    }));
    const err = await get("/assets").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(422);
    expect(describeError(err)).toBe("参数不合法（semver：格式错误）");
    expect(calls[0].path).toBe("/assets");
    const init = vi.mocked(fetch).mock.calls[0][1]!;
    expect((init.headers as Record<string, string>).Authorization).toBe("Bearer tok-1");
  });

  it("401 触发登出回调，登录接口自身的 401 除外", async () => {
    const handler = vi.fn();
    setUnauthorizedHandler(handler);
    mockApi(() => ({ status: 401, body: { code: "UNAUTHENTICATED", detail: "会话过期" } }));
    await get("/me").catch(() => undefined);
    expect(handler).toHaveBeenCalledTimes(1);
    await get("/auth/login").catch(() => undefined);
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it("query 丢弃空值并编码", () => {
    expect(query({ kind: "tool.invoke", trace_id: "", limit: 50, since: undefined })).toBe("?kind=tool.invoke&limit=50");
    expect(query({})).toBe("");
  });
});
