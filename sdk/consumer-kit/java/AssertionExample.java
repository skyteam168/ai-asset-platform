/**
 * AIP 消费方断言签发示例（ES256）— 对等 claims 模板，无 Maven CI。
 *
 * 依赖（自行引入）：
 *   io.jsonwebtoken:jjwt-api / jjwt-impl / jjwt-jackson
 *   或等价 JWT 库支持 ES256 + kid header。
 *
 * Claims（与 architecture.md 3.1 / 现网一致）：
 *   用户断言：iss=client_id, sub=外部主体, aud=mcp_resource（或 token_endpoint_aud）,
 *             iat/exp(≤5min), jti=每次新随机；header.kid
 *   客户端断言：sub=iss=client_id, aud=token endpoint audience
 *
 * 安全：勿将断言或 access_token 写入日志。
 */
import io.jsonwebtoken.Jwts;
import io.jsonwebtoken.security.EcPrivateJwk;
import io.jsonwebtoken.security.Jwks;

import java.security.KeyPair;
import java.security.KeyPairGenerator;
import java.security.PrivateKey;
import java.security.interfaces.ECPrivateKey;
import java.time.Instant;
import java.util.Date;
import java.util.UUID;

public final class AssertionExample {

    private AssertionExample() {}

    /** 签发用户断言。 */
    public static String signUserAssertion(
            PrivateKey privateKey,
            String kid,
            String clientId,
            String subject,
            String audience,
            long ttlSeconds
    ) {
        if (ttlSeconds > 300) {
            throw new IllegalArgumentException("断言有效期不得超过 300 秒");
        }
        Instant now = Instant.now();
        return Jwts.builder()
                .header().keyId(kid).and()
                .issuer(clientId)
                .subject(subject)
                .audience().add(audience).and()
                .issuedAt(Date.from(now))
                .expiration(Date.from(now.plusSeconds(ttlSeconds)))
                .id(UUID.randomUUID().toString().replace("-", ""))
                .signWith(privateKey)
                .compact();
    }

    /** 签发客户端断言：sub = iss = clientId。 */
    public static String signClientAssertion(
            PrivateKey privateKey,
            String kid,
            String clientId,
            String tokenEndpointAudience,
            long ttlSeconds
    ) {
        return signUserAssertion(
                privateKey, kid, clientId, clientId, tokenEndpointAudience, ttlSeconds);
    }

    /** 本地生成 P-256 密钥对并打印公钥 JWK（登记到平台）；演示签发。 */
    public static void main(String[] args) throws Exception {
        KeyPairGenerator g = KeyPairGenerator.getInstance("EC");
        g.initialize(256);
        KeyPair kp = g.generateKeyPair();
        String kid = UUID.randomUUID().toString().substring(0, 16);

        EcPrivateJwk jwk = Jwks.builder()
                .key((ECPrivateKey) kp.getPrivate())
                .id(kid)
                .build();
        System.out.println("登记到平台的公钥 JWK（勿提交私钥）:");
        System.out.println(jwk.toPublicJwk());

        String assertion = signUserAssertion(
                kp.getPrivate(), kid, "vn-ai", "emp-42",
                "https://aip.example/mcp", 120);
        System.out.println("assertion prefix: " + assertion.substring(0, Math.min(24, assertion.length())) + "…");
        System.out.println("换令牌: POST /oauth/token grant_type=jwt-bearer + assertion + client_assertion");
    }
}
