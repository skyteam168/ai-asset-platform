// AIP 消费方断言签发示例（ES256）— 对等 claims 模板，无 dotnet test CI。
//
// 包：System.IdentityModel.Tokens.Jwt、Microsoft.IdentityModel.Tokens
//
// Claims（与 architecture.md 3.1 / 现网一致）：
//   用户断言：iss=client_id, sub=外部主体, aud=mcp_resource（或 token_endpoint_aud）,
//             iat/exp(≤5min), jti=每次新随机；header.kid
//   客户端断言：sub=iss=client_id, aud=token endpoint audience
//
// 安全：勿将断言或 access_token 写入日志。

using System.IdentityModel.Tokens.Jwt;
using System.Security.Cryptography;
using Microsoft.IdentityModel.Tokens;

public static class AssertionExample
{
    /// <summary>签发用户断言。</summary>
    public static string SignUserAssertion(
        ECDsa privateKey,
        string kid,
        string clientId,
        string subject,
        string audience,
        int ttlSeconds = 120)
    {
        if (ttlSeconds > 300)
            throw new ArgumentOutOfRangeException(nameof(ttlSeconds), "断言有效期不得超过 300 秒");

        var now = DateTimeOffset.UtcNow;
        var credentials = new SigningCredentials(
            new ECDsaSecurityKey(privateKey) { KeyId = kid },
            SecurityAlgorithms.EcdsaSha256);

        var token = new JwtSecurityToken(
            issuer: clientId,
            audience: audience,
            claims: new[]
            {
                new System.Security.Claims.Claim(JwtRegisteredClaimNames.Sub, subject),
                new System.Security.Claims.Claim(JwtRegisteredClaimNames.Jti, Guid.NewGuid().ToString("N")),
            },
            notBefore: now.UtcDateTime,
            expires: now.AddSeconds(ttlSeconds).UtcDateTime,
            signingCredentials: credentials);

        return new JwtSecurityTokenHandler().WriteToken(token);
    }

    /// <summary>签发客户端断言：sub = iss = clientId。</summary>
    public static string SignClientAssertion(
        ECDsa privateKey,
        string kid,
        string clientId,
        string tokenEndpointAudience,
        int ttlSeconds = 120) =>
        SignUserAssertion(privateKey, kid, clientId, clientId, tokenEndpointAudience, ttlSeconds);

    public static void Main()
    {
        using var ecdsa = ECDsa.Create(ECCurve.NamedCurves.nistP256);
        var kid = Guid.NewGuid().ToString("N")[..16];
        var parameters = ecdsa.ExportParameters(includePrivateParameters: false);
        Console.WriteLine($"kid={kid}; 将公钥 JWK 登记到平台（勿提交私钥）");
        Console.WriteLine($"x={Convert.ToBase64String(parameters.Q.X!)}, y={Convert.ToBase64String(parameters.Q.Y!)}");

        var assertion = SignUserAssertion(
            ecdsa, kid, "vn-ai", "emp-42", "https://aip.example/mcp", 120);
        Console.WriteLine($"assertion prefix: {assertion[..Math.Min(24, assertion.Length)]}…");
        Console.WriteLine("换令牌: POST /oauth/token grant_type=jwt-bearer + assertion + client_assertion");
    }
}
