"""PoC: HTTP / self-signed HTTPS pinned / insecure client factory on httpx + httpcore.

Pin and hostname checks run right after the TLS handshake inside start_tls(),
i.e. before httpcore writes a single byte of the HTTP request (Authorization included).
"""
import asyncio, datetime, hashlib, ipaddress, ssl, tempfile, threading, os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpcore, httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


class TlsPolicyError(httpcore.ConnectError):
    def __init__(self, code, detail=""):
        super().__init__(f"{code}: {detail}")
        self.code = code


def fp_cert(der):
    return hashlib.sha256(der).hexdigest()


def fp_spki(der):
    pub = x509.load_der_x509_certificate(der).public_key()
    return hashlib.sha256(pub.public_bytes(serialization.Encoding.DER,
                                            serialization.PublicFormat.SubjectPublicKeyInfo)).hexdigest()


def hostname_matches(der, host):
    cert = x509.load_der_x509_certificate(der)
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return False
    try:
        ip = ipaddress.ip_address(host)
        return ip in san.get_values_for_type(x509.IPAddress)
    except ValueError:
        names = san.get_values_for_type(x509.DNSName)
        return any(n.lower() == host.lower() or (n.startswith("*.") and host.lower().endswith(n[1:].lower())
                                                   and host.count(".") == n.count(".")) for n in names)


class _CheckedStream(httpcore.AsyncNetworkStream):
    def __init__(self, inner, policy, host):
        self._s, self._p, self._host = inner, policy, host

    async def read(self, n, timeout=None): return await self._s.read(n, timeout)
    async def write(self, b, timeout=None): return await self._s.write(b, timeout)
    async def aclose(self): return await self._s.aclose()
    def get_extra_info(self, k): return self._s.get_extra_info(k)

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        s = await self._s.start_tls(ssl_context, server_hostname, timeout)
        der = s.get_extra_info("ssl_object").getpeercert(binary_form=True)
        p = self._p
        try:
            if p["tls_mode"] == "pinned":
                got = fp_spki(der) if p.get("pin_kind") == "spki" else fp_cert(der)
                if got not in p["pins"]:
                    raise TlsPolicyError("TLS_PIN_MISMATCH", got)
            if p["tls_mode"] == "insecure":
                if p.get("check_hostname") and not hostname_matches(der, self._host):
                    raise TlsPolicyError("TLS_HOSTNAME_MISMATCH", self._host)
                seen = fp_cert(der)
                last = p.get("last_seen_fingerprint")
                if last and last != seen:
                    p.setdefault("events", []).append(("TLS_FINGERPRINT_CHANGED", last, seen))
                    if p.get("block_on_change"):
                        raise TlsPolicyError("TLS_FINGERPRINT_CHANGED", seen)
                p["last_seen_fingerprint"] = seen
        except Exception:
            await s.aclose()
            raise
        return _CheckedStream(s, p, self._host)


class _Backend(httpcore.AsyncNetworkBackend):
    def __init__(self, policy):
        self._inner, self._p = httpcore.AnyIOBackend(), policy

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        s = await self._inner.connect_tcp(host, port, timeout, local_address, socket_options)
        return _CheckedStream(s, self._p, host)

    async def sleep(self, seconds): await self._inner.sleep(seconds)


class _Transport(httpx.AsyncBaseTransport):
    def __init__(self, policy, ctx):
        self._pool = httpcore.AsyncConnectionPool(ssl_context=ctx, network_backend=_Backend(policy))
        self._t = httpx.AsyncHTTPTransport()   # reuse httpx's request/response mapping
        self._t._pool = self._pool

    async def handle_async_request(self, request):
        return await self._t.handle_async_request(request)

    async def aclose(self): await self._pool.aclose()


def build_client(conn: dict) -> httpx.AsyncClient:
    """conn: base_url, scheme, tls_mode, pins, pin_kind, check_hostname, ca_pem ..."""
    url = httpx.URL(conn["base_url"])
    if url.scheme != conn["scheme"]:
        raise ValueError("SCHEME_MISMATCH")
    if conn["scheme"] == "http" or conn["tls_mode"] in ("pinned", "insecure"):
        host = url.host
        if not ipaddress.ip_address(host).is_private if _is_ip(host) else False:
            raise ValueError("TRANSPORT_EXCEPTION_PUBLIC_ADDRESS")
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    if conn.get("tls_mode") == "custom_ca":
        ctx.load_verify_locations(cadata=conn["ca_pem"])
    elif conn.get("tls_mode") in ("pinned", "insecure"):
        ctx.check_hostname = False          # must be set before CERT_NONE (ssl limitation)
        ctx.verify_mode = ssl.CERT_NONE     # chain not validated; pin / manual hostname check instead
    return httpx.AsyncClient(base_url=conn["base_url"], transport=_Transport(conn, ctx),
                             follow_redirects=False, timeout=httpx.Timeout(5.0, connect=2.0))


def _is_ip(h):
    try:
        ipaddress.ip_address(h); return True
    except ValueError:
        return False


# ---------------- test fixtures ----------------
def make_self_signed(cn="legacy.local", sans=("legacy.local",), ips=("127.0.0.1",)):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = datetime.datetime.now(datetime.timezone.utc)
    alt = [x509.DNSName(s) for s in sans] + [x509.IPAddress(ipaddress.ip_address(i)) for i in ips]
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=365))
            .add_extension(x509.SubjectAlternativeName(alt), critical=False).sign(key, hashes.SHA256()))
    d = tempfile.mkdtemp()
    cp, kp = os.path.join(d, "c.pem"), os.path.join(d, "k.pem")
    open(cp, "wb").write(cert.public_bytes(serialization.Encoding.PEM))
    open(kp, "wb").write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return cp, kp, cert.public_bytes(serialization.Encoding.DER)


class Srv:
    def __init__(self, tls=None):
        self.hits = 0
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.hits += 1
                self.send_response(200); self.end_headers(); self.wfile.write(b'{"ok":true}')
            def log_message(self, *a): pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        if tls:
            c = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); c.load_cert_chain(*tls)
            self.httpd.socket = c.wrap_socket(self.httpd.socket, server_side=True)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


async def call(conn, path="/ping"):
    async with build_client(conn) as c:
        try:
            r = await c.get(path, headers={"Authorization": "Bearer SECRET"})
            return r.status_code
        except TlsPolicyError as e:
            return e.code
        except httpx.ConnectError as e:
            return "CONNECT_ERROR:" + type(e.__cause__ or e).__name__ + ":" + str(e)[:60]


async def main():
    cp, kp, der = make_self_signed()
    https = Srv((cp, kp)); http = Srv()
    base = f"https://127.0.0.1:{https.port}"
    res = {}
    res["http_ok"] = await call({"base_url": f"http://127.0.0.1:{http.port}", "scheme": "http", "tls_mode": None})
    res["scheme_mismatch"] = None
    try:
        build_client({"base_url": f"http://127.0.0.1:{http.port}", "scheme": "https", "tls_mode": "pinned"})
    except ValueError as e:
        res["scheme_mismatch"] = str(e)
    res["system_ca_rejects_selfsigned"] = await call({"base_url": base, "scheme": "https", "tls_mode": "system_ca"})
    res["custom_ca_selfsigned_as_anchor"] = await call({"base_url": base, "scheme": "https", "tls_mode": "custom_ca",
                                                        "ca_pem": open(cp).read()})
    res["pinned_cert_ok"] = await call({"base_url": base, "scheme": "https", "tls_mode": "pinned", "pins": [fp_cert(der)]})
    res["pinned_spki_ok"] = await call({"base_url": base, "scheme": "https", "tls_mode": "pinned", "pin_kind": "spki",
                                        "pins": [fp_spki(der)]})
    before = https.hits
    res["pinned_wrong"] = await call({"base_url": base, "scheme": "https", "tls_mode": "pinned", "pins": ["00" * 32]})
    res["pinned_wrong_server_hits_delta"] = https.hits - before
    p = {"base_url": base, "scheme": "https", "tls_mode": "insecure", "check_hostname": False}
    res["insecure_ok"] = await call(p)
    res["insecure_recorded_fp"] = p.get("last_seen_fingerprint") == fp_cert(der)
    res["insecure_hostname_ip_ok"] = await call({**p, "check_hostname": True})
    res["insecure_hostname_mismatch"] = await call({"base_url": f"https://localhost:{https.port}", "scheme": "https",
                                                    "tls_mode": "insecure", "check_hostname": True})
    p2 = {**p, "last_seen_fingerprint": "ab" * 32, "block_on_change": False}
    res["insecure_fp_change_alert_only"] = (await call(p2), p2.get("events", [[None]])[0][0])
    for k, v in res.items():
        print(f"{k:36s} {v}")


asyncio.run(main())
