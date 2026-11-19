"""Creates the e2e test identity provider's keys (issue #26), in <dir>:

- ca.pem, tls.pem, tls.key: a throwaway CA and a TLS certificate for
  idp.e2e.test, so the validator talks https to the mock, as to LS Login;
- signing.key, jwks.json: the RSA key the tokens are signed with, and its
  public half as a JWKS; other.key: a key the issuer doesn't publish.

Test material only, made fresh for each run.

    python3 make_keys.py <dir>
"""

import datetime
import json
import sys
from pathlib import Path

import jwt
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

HOST = "idp.e2e.test"


def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def pem(k):
    return k.private_bytes(serialization.Encoding.PEM,
                           serialization.PrivateFormat.PKCS8,
                           serialization.NoEncryption())


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key, tls_key = key(), key()
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "e2e test CA")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
          .public_key(ca_key.public_key()).serial_number(1)
          .not_valid_before(now - datetime.timedelta(minutes=5))
          .not_valid_after(now + datetime.timedelta(days=1))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), True)
          .add_extension(x509.KeyUsage(False, False, False, False, False,
                                       True, True, False, False), True)
          # Python 3.13's default context verifies strictly (RFC 5280) and
          # needs the key identifiers.
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(
              ca_key.public_key()), False)
          .sign(ca_key, hashes.SHA256()))
    tls = (x509.CertificateBuilder()
           .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, HOST)]))
           .issuer_name(ca_name).public_key(tls_key.public_key()).serial_number(2)
           .not_valid_before(now - datetime.timedelta(minutes=5))
           .not_valid_after(now + datetime.timedelta(days=1))
           .add_extension(x509.SubjectAlternativeName([x509.DNSName(HOST)]), False)
           .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
           .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(
               ca_key.public_key()), False)
           .add_extension(x509.SubjectKeyIdentifier.from_public_key(
               tls_key.public_key()), False)
           .sign(ca_key, hashes.SHA256()))
    (out / "ca.pem").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    (out / "tls.pem").write_bytes(tls.public_bytes(serialization.Encoding.PEM))
    (out / "tls.key").write_bytes(pem(tls_key))
    signing = key()
    (out / "signing.key").write_bytes(pem(signing))
    (out / "other.key").write_bytes(pem(key()))
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(signing.public_key()))
    jwk.update(kid="e2e-1", use="sig", alg="RS256")
    (out / "jwks.json").write_text(json.dumps({"keys": [jwk]}))
    # The mock runs as nobody in its container.
    for f in out.iterdir():
        f.chmod(0o644)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
