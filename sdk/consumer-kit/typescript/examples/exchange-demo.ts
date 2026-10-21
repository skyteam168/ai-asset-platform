/**
 * 用用户断言换 access_token 的最小示例（jwt-bearer）。
 *
 * 环境变量同 python/examples/exchange_demo.py。
 * 安全：禁止把完整 access_token / 断言写入日志；本脚本仅打印前缀。
 */

import { readFileSync } from "node:fs";
import { importPKCS8 } from "jose";
import { signClientAssertion, signUserAssertion } from "../src/assertion.js";

async function main(): Promise<void> {
  const base = required("AIP_BASE_URL").replace(/\/$/, "");
  const clientId = required("AIP_CLIENT_ID");
  const subject = required("AIP_SUBJECT");
  const audience = required("AIP_AUDIENCE");
  const kid = required("AIP_KID");
  const pem = readFileSync(required("AIP_PRIVATE_PEM_PATH"), "utf8");
  const privateKey = await importPKCS8(pem, "ES256");

  const assertion = await signUserAssertion(privateKey, {
    clientId,
    subject,
    audience,
    kid,
  });

  const body = new URLSearchParams({
    grant_type: "urn:ietf:params:oauth:grant-type:jwt-bearer",
    assertion,
    client_id: clientId,
  });

  const staticToken = process.env.AIP_STATIC_TOKEN;
  if (staticToken) {
    body.set("client_assertion", staticToken);
    body.set(
      "client_assertion_type",
      "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
    );
  } else {
    const tokenAud =
      process.env.AIP_TOKEN_ENDPOINT_AUD ?? `${base}/oauth/token`;
    body.set(
      "client_assertion",
      await signClientAssertion(privateKey, {
        clientId,
        audience: tokenAud,
        kid,
      }),
    );
    body.set(
      "client_assertion_type",
      "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
    );
  }

  const r = await fetch(`${base}/oauth/token`, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body,
  });
  if (!r.ok) {
    throw new Error(`token exchange failed: ${r.status}`);
  }
  const json = (await r.json()) as { access_token: string };
  const token = json.access_token;
  console.log(`access_token prefix: ${token.slice(0, 12)}… (len=${token.length})`);
  console.log("勿将完整令牌写入日志或提交到版本库。");
}

function required(name: string): string {
  const v = process.env[name];
  if (!v) throw new Error(`missing env ${name}`);
  return v;
}

main().catch((e) => {
  console.error(e instanceof Error ? e.message : e);
  process.exit(1);
});
