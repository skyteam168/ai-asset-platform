/** 签发消费方用户断言 / 客户端断言（ES256，architecture.md 3.1）。 */

import { exportJWK, generateKeyPair, SignJWT, type JWK, type KeyLike } from "jose";
import { randomBytes } from "node:crypto";

export type GeneratedKey = {
  privateKey: KeyLike;
  kid: string;
  publicJwk: JWK;
};

export async function generateEs256Keypair(): Promise<GeneratedKey> {
  const { privateKey, publicKey } = await generateKeyPair("ES256");
  const kid = randomBytes(8).toString("hex");
  const publicJwk = await exportJWK(publicKey);
  publicJwk.kid = kid;
  publicJwk.alg = "ES256";
  publicJwk.use = "sig";
  return { privateKey, kid, publicJwk };
}

export type SignUserAssertionOpts = {
  clientId: string;
  subject: string;
  audience: string;
  kid: string;
  /** 秒；默认 120，上限 300 */
  ttlS?: number;
  extra?: Record<string, unknown>;
};

export async function signUserAssertion(
  privateKey: KeyLike,
  opts: SignUserAssertionOpts,
): Promise<string> {
  const ttl = opts.ttlS ?? 120;
  if (ttl > 300) {
    throw new Error("断言有效期不得超过 300 秒（architecture.md 3.1）");
  }
  const now = Math.floor(Date.now() / 1000);
  return new SignJWT({
    ...(opts.extra ?? {}),
    jti: randomBytes(8).toString("hex"),
  })
    .setProtectedHeader({ alg: "ES256", kid: opts.kid })
    .setIssuer(opts.clientId)
    .setSubject(opts.subject)
    .setAudience(opts.audience)
    .setIssuedAt(now)
    .setExpirationTime(now + ttl)
    .sign(privateKey);
}

export async function signClientAssertion(
  privateKey: KeyLike,
  opts: Omit<SignUserAssertionOpts, "subject">,
): Promise<string> {
  return signUserAssertion(privateKey, {
    ...opts,
    subject: opts.clientId,
  });
}
