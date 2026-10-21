import { decodeJwt, decodeProtectedHeader, importJWK, jwtVerify } from "jose";
import { describe, expect, it } from "vitest";
import {
  generateEs256Keypair,
  signClientAssertion,
  signUserAssertion,
} from "./assertion.js";

describe("assertion", () => {
  it("signUserAssertion roundtrip", async () => {
    const { privateKey, kid, publicJwk } = await generateEs256Keypair();
    const token = await signUserAssertion(privateKey, {
      clientId: "vn-ai",
      subject: "emp-42",
      audience: "https://aip.example/mcp",
      kid,
      ttlS: 120,
    });
    const key = await importJWK(publicJwk, "ES256");
    const { payload, protectedHeader } = await jwtVerify(token, key, {
      audience: "https://aip.example/mcp",
    });
    expect(payload.iss).toBe("vn-ai");
    expect(payload.sub).toBe("emp-42");
    expect(payload.jti).toBeTruthy();
    expect(protectedHeader.kid).toBe(kid);
    expect(protectedHeader.alg).toBe("ES256");
  });

  it("signClientAssertion sub equals iss", async () => {
    const { privateKey, kid, publicJwk } = await generateEs256Keypair();
    const token = await signClientAssertion(privateKey, {
      clientId: "vn-ai",
      audience: "https://aip.example/oauth/token",
      kid,
    });
    const key = await importJWK(publicJwk, "ES256");
    const { payload } = await jwtVerify(token, key, {
      audience: "https://aip.example/oauth/token",
    });
    expect(payload.iss).toBe("vn-ai");
    expect(payload.sub).toBe("vn-ai");
  });

  it("rejects ttl > 300", async () => {
    const { privateKey, kid } = await generateEs256Keypair();
    await expect(
      signUserAssertion(privateKey, {
        clientId: "vn-ai",
        subject: "e",
        audience: "aud",
        kid,
        ttlS: 301,
      }),
    ).rejects.toThrow(/300/);
  });

  it("jti unique per call", async () => {
    const { privateKey, kid } = await generateEs256Keypair();
    const a = await signUserAssertion(privateKey, {
      clientId: "c",
      subject: "s",
      audience: "a",
      kid,
    });
    const b = await signUserAssertion(privateKey, {
      clientId: "c",
      subject: "s",
      audience: "a",
      kid,
    });
    expect(decodeJwt(a).jti).not.toBe(decodeJwt(b).jti);
    expect(decodeProtectedHeader(a).kid).toBe(kid);
  });
});
