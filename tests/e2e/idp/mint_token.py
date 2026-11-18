"""Prints an access token from the e2e identity provider (issue #26).

    python3 mint_token.py <keys dir> [--sub S] [--client C] [--typ T]
                          [--exp-in SECONDS] [--other-key]
"""

import argparse
import time
from pathlib import Path

import jwt

ISSUER = "https://idp.e2e.test:8443/"

parser = argparse.ArgumentParser()
parser.add_argument("keys", type=Path)
parser.add_argument("--sub", default="entitled-user")
parser.add_argument("--client", default="e2e-portal")
parser.add_argument("--typ", default="at+jwt")
parser.add_argument("--exp-in", type=int, default=300)
parser.add_argument("--other-key", action="store_true",
                    help="sign with a key the issuer doesn't publish")
args = parser.parse_args()
now = int(time.time())
key = (args.keys / ("other.key" if args.other_key else "signing.key")).read_bytes()
print(jwt.encode(
    {"iss": ISSUER, "sub": args.sub, "client_id": args.client, "aud": "tes",
     "iat": now, "exp": now + args.exp_in, "scope": "openid"},
    key, algorithm="RS256", headers={"typ": args.typ, "kid": "e2e-1"}))
