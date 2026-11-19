"""A minimal OpenID provider for the e2e test (issue #26): discovery, JWKS
and userinfo over https, like LS Login. Standard library only.

userinfo answers for any Bearer token without checking it (the validator
checks tokens; this only stands in for LS Login's answer): subjects starting
with "entitled" get the e2e entitlement, others get none.

    python3 mock_idp.py <dir from make_keys.py>
"""

import base64
import json
import ssl
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ISSUER = "https://idp.e2e.test:8443/"
ENTITLEMENT = "urn:e2e:group:ECP_CLN"


def make_handler(jwks: bytes):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path == "/.well-known/openid-configuration":
                self.send(200, {"issuer": ISSUER, "jwks_uri": ISSUER + "jwk",
                                "userinfo_endpoint": ISSUER + "userinfo"})
            elif self.path == "/jwk":
                self.send(200, json.loads(jwks))
            elif self.path == "/userinfo":
                auth = self.headers.get("Authorization", "")
                if not auth.startswith("Bearer "):
                    self.send(401, {"error": "invalid_token"})
                    return
                payload = auth[7:].split(".")[1]
                claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
                sub = claims.get("sub", "")
                ents = [ENTITLEMENT] if sub.startswith("entitled") else []
                self.send(200, {"sub": sub, "eduperson_entitlement": ents})
            else:
                self.send(404, {})

        def send(self, status, body):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler


def main(keys: Path) -> None:
    server = ThreadingHTTPServer(("0.0.0.0", 8443),
                                 make_handler((keys / "jwks.json").read_bytes()))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(keys / "tls.pem", keys / "tls.key")
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()


if __name__ == "__main__":
    main(Path(sys.argv[1]))
