"""Unit tests for files/tes-validator/tes_validator.py (issue #26).

A local HTTP server stands in for the identity provider (discovery, JWKS,
userinfo); tokens are signed with an RSA key generated here. Run with:

    python3 -m pytest tests/
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import hmac
import http.client
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                       / "files" / "tes-validator"))
import tes_validator as tv  # noqa: E402

CLIENT = "krini-client"
EXEMPT = "protes-service"
ENTITLEMENT = "urn:example:group:vo:ECP_CLN#login.example.org"
PREFIX = "urn:example:group:vo:ECP_CLN:"


class IdP:
    """A fake OpenID provider on 127.0.0.1, configurable per test."""

    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "key-1"
        self.userinfo = {"sub": "user-1", "eduperson_entitlement": [ENTITLEMENT]}
        self.userinfo_status = 200
        self.hits = {"discovery": 0, "jwks": 0, "userinfo": 0}
        self.discovery_issuer = None  # None: the real one
        self.discovery_userinfo = True
        self.extra_keys = []
        self.jwks_status = 200
        self.jwks_body = None  # None: the real key set
        # Like LS Login: userinfo answers this many seconds late when asked
        # for application/json, and at once otherwise.
        self.userinfo_json_delay = 0
        self.accept = {}  # path -> the Accept header of its last request
        idp = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                idp.accept[self.path] = self.headers.get("Accept")
                if self.path == "/.well-known/openid-configuration":
                    idp.hits["discovery"] += 1
                    body = {"issuer": idp.discovery_issuer or idp.issuer,
                            "jwks_uri": idp.base + "/jwks"}
                    if idp.discovery_userinfo:
                        body["userinfo_endpoint"] = idp.base + "/userinfo"
                    return self._json(200, body)
                if self.path == "/jwks":
                    idp.hits["jwks"] += 1
                    if idp.jwks_body is not None:
                        return self._json(idp.jwks_status, idp.jwks_body)
                    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(
                        idp.key.public_key()))
                    jwk.update(kid=idp.kid, use="sig", alg="RS256")
                    return self._json(idp.jwks_status,
                                      {"keys": [jwk, *idp.extra_keys]})
                if self.path == "/userinfo":
                    idp.hits["userinfo"] += 1
                    auth = self.headers.get("Authorization", "")
                    if not auth.startswith("Bearer "):
                        return self._json(401, {})
                    if "application/json" in (self.headers.get("Accept") or ""):
                        time.sleep(idp.userinfo_json_delay)
                    return self._json(idp.userinfo_status, idp.userinfo)
                if self.path == "/notjson":
                    return self._raw(200, b"<html>")
                if self.path == "/trickle":
                    # A byte every 0.3 s: each read is quick, the whole slow.
                    self.send_response(200)
                    self.send_header("Content-Length", "20")
                    self.end_headers()
                    for _ in range(20):
                        try:
                            self.wfile.write(b" ")
                            self.wfile.flush()
                        except OSError:
                            return
                        time.sleep(0.3)
                    return
                if self.path == "/huge":
                    return self._raw(200, b" " * (tv.MAX_RESPONSE_BYTES + 10))
                return self._json(404, {})

            def _json(self, status, body):
                self._raw(status, json.dumps(body).encode())

            def _raw(self, status, data):
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.issuer = self.base + "/"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def token(self, key=None, alg="RS256", typ="at+jwt", kid="__default__",
              **claims):
        now = int(time.time())
        body = {"iss": self.issuer, "sub": "user-1", "client_id": CLIENT,
                "iat": now, "exp": now + 300, "aud": "tes", **claims}
        body = {k: v for k, v in body.items() if v is not None}
        headers = {}
        if typ is not None:
            headers["typ"] = typ
        if kid == "__default__":
            headers["kid"] = self.kid
        elif kid is not None:
            headers["kid"] = kid
        return jwt.encode(body, key or self.key, algorithm=alg, headers=headers)

    def config(self, **overrides):
        issuer = {
            "issuer": self.issuer,
            "algorithms": ["RS256"],
            "allowed_client_ids": [CLIENT, EXEMPT],
            "exempt_client_ids": [EXEMPT],
            "entitlements": {"any_of": [ENTITLEMENT]},
            # Required with an exempt client; the test tokens' aud.
            "exempt_audiences": ["tes"],
        }
        issuer.update(overrides.pop("issuer_overrides", {}))
        d = {"issuers": [issuer], "basic_auth_enabled": True, **overrides}
        return tv.Config.from_dict(d, allow_http=True)


@pytest.fixture
def idp():
    p = IdP()
    yield p
    p.server.shutdown()


def check(validator, token):
    return validator.check(f"Bearer {token}")


def denied(validator, header, reason):
    with pytest.raises(tv.Denied, match=reason):
        validator.check(header)


# -- tokens that pass ---------------------------------------------------------

def test_valid_token_with_entitlement(idp):
    v = tv.Validator(idp.config())
    assert check(v, idp.token()).kind == "bearer"


def test_entitlement_by_prefix(idp):
    idp.userinfo["eduperson_entitlement"] = [PREFIX + "EOC-Compute#x"]
    v = tv.Validator(idp.config(issuer_overrides={
        "entitlements": {"prefix_any_of": [PREFIX]}}))
    assert check(v, idp.token()).kind == "bearer"


def test_entitlement_as_a_single_string(idp):
    idp.userinfo["eduperson_entitlement"] = ENTITLEMENT
    v = tv.Validator(idp.config())
    assert check(v, idp.token()).kind == "bearer"


def test_exempt_client_needs_no_userinfo(idp):
    v = tv.Validator(idp.config())
    assert check(v, idp.token(client_id=EXEMPT)).kind == "bearer"
    assert idp.hits["userinfo"] == 0


def test_no_entitlement_needed_when_switched_off(idp):
    v = tv.Validator(idp.config(issuer_overrides={
        "require_entitlement": False, "entitlements": {}}))
    assert "no entitlement" in check(v, idp.token()).reason
    assert idp.hits["userinfo"] == 0


def test_audience_checked_when_configured(idp):
    v = tv.Validator(idp.config(issuer_overrides={"audiences": ["tes"]}))
    assert check(v, idp.token()).kind == "bearer"
    denied(v, "Bearer " + idp.token(aud="other"), "InvalidAudience")


def test_endpoints_from_config_skip_discovery(idp):
    v = tv.Validator(idp.config(issuer_overrides={
        "jwks_uri": idp.base + "/jwks",
        "userinfo_endpoint": idp.base + "/userinfo"}))
    check(v, idp.token())
    assert idp.hits["discovery"] == 0


def test_media_type_typ_accepted(idp):
    v = tv.Validator(idp.config())
    assert check(v, idp.token(typ="application/AT+JWT")).kind == "bearer"


def test_single_key_used_when_token_has_no_kid(idp):
    v = tv.Validator(idp.config())
    assert check(v, idp.token(kid=None)).kind == "bearer"


def test_expired_within_leeway_passes(idp):
    v = tv.Validator(idp.config(leeway_seconds=60))
    assert check(v, idp.token(exp=int(time.time()) - 30)).kind == "bearer"


# -- tokens that fail ---------------------------------------------------------

def test_token_of_foreign_issuer_is_refused(idp):
    other = IdP()
    try:
        v = tv.Validator(idp.config())
        denied(v, "Bearer " + other.token(), "issuer not configured")
        assert other.hits == {"discovery": 0, "jwks": 0, "userinfo": 0}
    finally:
        other.server.shutdown()


def test_forged_iss_signed_by_another_key_is_refused(idp):
    """iss names our issuer, but the attacker signed it with their own key."""
    attacker = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    v = tv.Validator(idp.config())
    denied(v, "Bearer " + idp.token(key=attacker), "InvalidSignature")


def test_issuer_differing_by_a_slash_is_refused(idp):
    v = tv.Validator(idp.config())
    denied(v, "Bearer " + idp.token(iss=idp.issuer.rstrip("/")),
           "issuer not configured")


def test_alg_none_is_refused(idp):
    v = tv.Validator(idp.config())
    body = {"iss": idp.issuer, "sub": "user-1", "client_id": CLIENT,
            "exp": int(time.time()) + 300}
    token = jwt.encode(body, None, algorithm="none",
                       headers={"typ": "at+jwt", "kid": idp.kid})
    # PyJWT leaves the signature empty, which isn't a signed JWT at all;
    # with a fake signature it still names a forbidden algorithm.
    denied(v, "Bearer " + token, "not a signed JWT")
    denied(v, "Bearer " + token + "AAAA", "algorithm not allowed")


def test_hs256_with_public_key_as_secret_is_refused(idp):
    """The classic key confusion: HMAC keyed with the issuer's public key."""
    pem = idp.key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    v = tv.Validator(idp.config())
    header = base64.urlsafe_b64encode(json.dumps(
        {"alg": "HS256", "typ": "at+jwt", "kid": idp.kid}).encode()).rstrip(b"=")
    body = base64.urlsafe_b64encode(json.dumps(
        {"iss": idp.issuer, "sub": "user-1", "client_id": CLIENT,
         "exp": int(time.time()) + 300}).encode()).rstrip(b"=")
    sig = base64.urlsafe_b64encode(hmac.new(
        pem, header + b"." + body, hashlib.sha256).digest()).rstrip(b"=")
    token = (header + b"." + body + b"." + sig).decode()
    denied(v, "Bearer " + token, "algorithm not allowed")


def test_allowed_alg_with_key_of_other_type_is_refused(idp):
    """ES256 allowed by config, but the JWKS key for that kid is RSA."""
    ec_key = ec.generate_private_key(ec.SECP256R1())
    v = tv.Validator(idp.config(issuer_overrides={
        "algorithms": ["RS256", "ES256"]}))
    denied(v, "Bearer " + idp.token(key=ec_key, alg="ES256"),
           "key type does not match")


def test_id_token_is_refused(idp):
    v = tv.Validator(idp.config())
    denied(v, "Bearer " + idp.token(typ="JWT"), "not an access token")
    denied(v, "Bearer " + idp.token(typ=None), "not an access token")


def test_client_not_allowed_is_refused(idp):
    v = tv.Validator(idp.config())
    denied(v, "Bearer " + idp.token(client_id="someone-else"), "client not allowed")
    denied(v, "Bearer " + idp.token(client_id=[CLIENT]), "client not allowed")


def test_prefix_needs_a_boundary(idp):
    with pytest.raises(ValueError, match="must end with ':'"):
        idp.config(issuer_overrides={
            "entitlements": {"prefix_any_of": ["urn:example:group:vo:ECP"]}})
    # With the boundary, a longer group name doesn't match.
    idp.userinfo["eduperson_entitlement"] = ["urn:example:group:vo:ECP_CLN_EVIL#x"]
    v = tv.Validator(idp.config(issuer_overrides={
        "entitlements": {"prefix_any_of": [PREFIX]}}))
    denied(v, "Bearer " + idp.token(), "no matching entitlement")


def test_missing_client_id_is_refused(idp):
    v = tv.Validator(idp.config())
    denied(v, "Bearer " + idp.token(client_id=None), "MissingRequiredClaim")


def test_expired_token_is_refused(idp):
    v = tv.Validator(idp.config(leeway_seconds=60))
    denied(v, "Bearer " + idp.token(exp=int(time.time()) - 120),
           "ExpiredSignature")


def test_token_not_yet_valid_is_refused(idp):
    v = tv.Validator(idp.config(leeway_seconds=60))
    denied(v, "Bearer " + idp.token(nbf=int(time.time()) + 600),
           "ImmatureSignature")


def test_user_without_entitlement_is_forbidden_not_unauthenticated(idp):
    idp.userinfo["eduperson_entitlement"] = ["urn:example:group:vo:other"]
    v = tv.Validator(idp.config())
    with pytest.raises(tv.Forbidden, match="no matching entitlement"):
        check(v, idp.token())


def test_userinfo_without_entitlement_claim_is_logged(idp, caplog):
    del idp.userinfo["eduperson_entitlement"]
    v = tv.Validator(idp.config())
    with caplog.at_level("WARNING", logger="tes-validator"):
        with pytest.raises(tv.Forbidden):
            check(v, idp.token())
    assert "userinfo has no eduperson_entitlement" in caplog.text
    assert CLIENT in caplog.text
    # Once per client and hour, not for every token.
    caplog.clear()
    with caplog.at_level("WARNING", logger="tes-validator"):
        with pytest.raises(tv.Forbidden):
            check(v, idp.token(sub="user-1", iat=1))
    assert "userinfo has no eduperson_entitlement" not in caplog.text


def test_http_missing_group_is_403(idp, service):
    idp.userinfo["eduperson_entitlement"] = []
    status, headers = call(service + "/auth", "Bearer " + idp.token())
    assert status == 403 and "WWW-Authenticate" not in headers


def test_exact_entitlement_is_not_a_prefix(idp):
    idp.userinfo["eduperson_entitlement"] = [ENTITLEMENT + "-evil"]
    v = tv.Validator(idp.config())
    denied(v, "Bearer " + idp.token(), "no matching entitlement")


def test_entitlement_of_wrong_type_is_ignored(idp):
    idp.userinfo["eduperson_entitlement"] = {"x": ENTITLEMENT}
    v = tv.Validator(idp.config())
    denied(v, "Bearer " + idp.token(), "no matching entitlement")


def test_userinfo_down_refuses_and_is_not_cached(idp):
    idp.userinfo_status = 503
    v = tv.Validator(idp.config())
    with pytest.raises(tv.Unavailable, match="userinfo failed"):
        check(v, idp.token())
    # Not cached: works once userinfo is back.
    idp.userinfo_status = 200
    assert check(v, idp.token()).kind == "bearer"


def test_userinfo_refusing_the_token_is_a_denial_not_an_outage(idp):
    idp.userinfo_status = 401
    v = tv.Validator(idp.config())
    with pytest.raises(tv.Denied, match="userinfo failed") as e:
        check(v, idp.token())
    assert not isinstance(e.value, tv.Unavailable)


def test_issuer_outages_are_unavailable(idp):
    idp.jwks_status = 503
    v = tv.Validator(idp.config())
    with pytest.raises(tv.Unavailable, match="JWKS fetch failed"):
        check(v, idp.token())
    v = tv.Validator(idp.config(issuer_overrides={
        "discovery_url": idp.base + "/missing"}))
    with pytest.raises(tv.Unavailable, match="discovery failed"):
        check(v, idp.token())


def test_exempt_client_needs_an_audience(idp):
    with pytest.raises(ValueError, match="need exempt_audiences"):
        idp.config(issuer_overrides={"exempt_audiences": []})
    # Without exempt clients, no audience is required.
    idp.config(issuer_overrides={"exempt_audiences": [], "exempt_client_ids": []})


def test_exempt_audience_must_not_be_a_client_id(idp):
    with pytest.raises(ValueError, match="not a client"):
        idp.config(issuer_overrides={"exempt_audiences": [EXEMPT]})


def test_exempt_token_for_another_service_is_refused(idp):
    v = tv.Validator(idp.config())
    denied(v, "Bearer " + idp.token(client_id=EXEMPT, aud="other-backend"),
           "not for this service")
    denied(v, "Bearer " + idp.token(client_id=EXEMPT, aud=None),
           "not for this service")
    assert check(v, idp.token(client_id=EXEMPT, aud=["x", "tes"])).exempt
    # User tokens needn't carry the exempt audience.
    assert check(v, idp.token(aud="something-else")).kind == "bearer"


def test_userinfo_rate_limit_is_unavailable(idp):
    idp.userinfo_status = 429
    v = tv.Validator(idp.config())
    with pytest.raises(tv.Unavailable):
        check(v, idp.token())


def test_userinfo_for_another_subject_is_refused(idp):
    idp.userinfo["sub"] = "user-2"
    v = tv.Validator(idp.config())
    denied(v, "Bearer " + idp.token(), "another subject")


def test_discovery_for_another_issuer_is_refused(idp):
    idp.discovery_issuer = "https://evil.example.org/"
    v = tv.Validator(idp.config())
    denied(v, "Bearer " + idp.token(), "another issuer")


def test_discovery_down_refuses(idp):
    v = tv.Validator(idp.config(issuer_overrides={
        "discovery_url": idp.base + "/missing"}))
    denied(v, "Bearer " + idp.token(), "discovery failed")


def test_discovered_endpoints_must_be_https(idp):
    # The configured URLs passed (allow_http); the discovered ones don't.
    config = dataclasses.replace(idp.config(), allow_http=False)
    v = tv.Validator(config)
    denied(v, "Bearer " + idp.token(), "jwks_uri must be an https")


def test_discovery_without_userinfo_endpoint(idp):
    idp.discovery_userinfo = False
    v = tv.Validator(idp.config())
    denied(v, "Bearer " + idp.token(), "no userinfo endpoint")
    # Tokens of exempt clients need no userinfo.
    assert check(v, idp.token(client_id=EXEMPT)).kind == "bearer"


def test_jwks_down_refuses(idp):
    v = tv.Validator(idp.config(issuer_overrides={
        "jwks_uri": idp.base + "/notjson"}))
    denied(v, "Bearer " + idp.token(), "JWKS fetch failed")


def test_unknown_kid_is_refused(idp):
    v = tv.Validator(idp.config())
    denied(v, "Bearer " + idp.token(kid="nope"), "unknown signing key")


def test_a_kidless_token_needs_a_single_key(idp):
    v = tv.Validator(idp.config())
    assert check(v, idp.token(kid=None)).kind == "bearer"
    state = v._state[idp.issuer]
    state.keys = {**state.keys, "key-2": object()}  # the issuer has two
    denied(v, "Bearer " + idp.token(kid=None), "unknown signing key")


def test_bad_jwks_entries_are_skipped(idp):
    idp.extra_keys = ["x", {"kty": "RSA", "kid": "broken"},
                      {"kty": "oct", "k": "c2VjcmV0", "kid": "enc", "use": "enc"}]
    v = tv.Validator(idp.config())
    assert check(v, idp.token()).kind == "bearer"
    denied(v, "Bearer " + idp.token(kid="broken"), "unknown signing key")


@pytest.mark.parametrize("jwks", [["not", "a", "dict"], {"keys": "no"},
                                  {"keys": []}])
def test_jwks_that_is_not_a_key_set_is_a_failed_fetch(idp, jwks):
    idp.jwks_body = jwks
    v = tv.Validator(idp.config())
    denied(v, "Bearer " + idp.token(), "no usable signing keys")


def test_empty_jwks_does_not_replace_known_keys(idp):
    now = [time.time()]
    v = tv.Validator(idp.config(jwks_cache_seconds=100), timer=lambda: now[0])
    check(v, idp.token(client_id=EXEMPT))
    idp.jwks_body = {"keys": []}
    now[0] += 101
    assert check(v, idp.token(client_id=EXEMPT)).kind == "bearer"


def test_stale_key_used_while_another_request_refreshes(idp):
    now = [time.time()]
    v = tv.Validator(idp.config(jwks_cache_seconds=100), timer=lambda: now[0])
    check(v, idp.token(client_id=EXEMPT))
    now[0] += 101
    issuer_state = next(iter(v._state.values()))
    issuer_state.refreshing = threading.Event()  # a refresh in progress
    assert check(v, idp.token(client_id=EXEMPT)).kind == "bearer"
    assert idp.hits["jwks"] == 1


def test_keys_dropped_after_max_stale_age(idp):
    now = [time.time()]
    v = tv.Validator(idp.config(jwks_cache_seconds=100,
                                jwks_max_stale_seconds=1000),
                     timer=lambda: now[0])
    check(v, idp.token(client_id=EXEMPT))
    idp.jwks_status = 503
    now[0] += 500
    assert check(v, idp.token(client_id=EXEMPT)).kind == "bearer"
    now[0] += 600
    denied(v, "Bearer " + idp.token(client_id=EXEMPT), "too old")


def test_known_keys_survive_a_failed_refresh(idp):
    now = [time.time()]
    v = tv.Validator(idp.config(jwks_cache_seconds=100), timer=lambda: now[0])
    check(v, idp.token(client_id=EXEMPT))
    idp.jwks_status = 503
    now[0] += 101
    # The issuer is down when the keys are due: they still work.
    assert check(v, idp.token(client_id=EXEMPT)).kind == "bearer"
    assert idp.hits["jwks"] == 2


def test_failed_jwks_fetches_are_not_retried_on_every_request(idp):
    now = [time.time()]
    idp.jwks_status = 503
    v = tv.Validator(idp.config(jwks_min_refresh_seconds=60),
                     timer=lambda: now[0])
    for _ in range(5):
        denied(v, "Bearer " + idp.token(), "JWKS fetch failed")
    assert "retrying after 60s" in str(pytest.raises(
        tv.Denied, v.check, "Bearer " + idp.token()).value)
    assert idp.hits["jwks"] == 1
    idp.jwks_status = 200
    now[0] += 61
    assert check(v, idp.token(client_id=EXEMPT)).kind == "bearer"


def test_garbage_tokens_are_refused(idp):
    v = tv.Validator(idp.config())
    denied(v, "Bearer not-a-jwt", "not a signed JWT")
    denied(v, "Bearer " + "a" * 20000 + ".b.c", "not a signed JWT")
    denied(v, "Bearer abc.def.ghi", "unreadable token")


# -- caching and abuse --------------------------------------------------------

def test_userinfo_is_not_asked_for_json(idp):
    """LS Login answered userinfo about 6 s late to Accept: application/json
    (0.3 s to */*), past the timeout: every token got 503."""
    idp.userinfo_json_delay = 3
    v = tv.Validator(idp.config(http_timeout_seconds=1))
    start = time.monotonic()
    assert check(v, idp.token()).kind == "bearer"
    assert time.monotonic() - start < 1
    assert idp.accept["/userinfo"] == "*/*"


def test_discovery_and_jwks_still_ask_for_json(idp):
    v = tv.Validator(idp.config())
    check(v, idp.token())
    assert idp.accept["/.well-known/openid-configuration"] == "application/json"
    assert idp.accept["/jwks"] == "application/json"


def test_a_slow_userinfo_is_paid_once_per_token(idp):
    idp.userinfo_json_delay = 0.5
    v = tv.Validator(idp.config())
    token = idp.token()
    check(v, token)
    start = time.monotonic()
    for _ in range(5):
        check(v, token)
    assert time.monotonic() - start < 0.5
    assert idp.hits["userinfo"] == 1


def test_userinfo_answer_is_cached(idp):
    v = tv.Validator(idp.config())
    token = idp.token()
    check(v, token)
    check(v, token)
    assert idp.hits["userinfo"] == 1


def test_userinfo_cache_expires_with_the_token(idp):
    now = [time.time()]
    v = tv.Validator(idp.config(), clock=lambda: now[0])
    token = idp.token(exp=int(now[0]) + 30)
    check(v, token)
    # The cache's clock passes exp; PyJWT, on the real clock, still accepts
    # the token, so this shows the cache entry expired with it.
    now[0] += 31
    check(v, token)
    assert idp.hits["userinfo"] == 2


def test_negative_entitlement_is_cached(idp):
    idp.userinfo["eduperson_entitlement"] = []
    v = tv.Validator(idp.config())
    token = idp.token()
    for _ in range(3):
        denied(v, "Bearer " + token, "no matching entitlement")
    assert idp.hits["userinfo"] == 1


def test_cache_is_bounded():
    clock = [0.0]
    cache = tv.TTLCache(2, lambda: clock[0])
    for k in "abc":
        cache.put(k, True, 10)
    assert cache.get("a") is None and cache.get("c") is True
    clock[0] = 11
    assert cache.get("c") is None
    off = tv.TTLCache(0, lambda: 0)
    off.put("a", True, 10)
    assert off.get("a") is None


def test_random_kids_do_not_hammer_jwks(idp):
    now = [time.time()]
    v = tv.Validator(idp.config(jwks_min_refresh_seconds=60),
                     timer=lambda: now[0])
    check(v, idp.token())
    for i in range(20):
        denied(v, "Bearer " + idp.token(kid=f"random-{i}"), "unknown signing key")
    assert idp.hits["jwks"] == 1
    now[0] += 61
    denied(v, "Bearer " + idp.token(kid="random-x"), "unknown signing key")
    assert idp.hits["jwks"] == 2


def test_rotated_key_is_picked_up(idp):
    now = [time.time()]
    v = tv.Validator(idp.config(), timer=lambda: now[0])
    check(v, idp.token())
    idp.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    idp.kid = "key-2"
    now[0] += 61
    assert check(v, idp.token()).kind == "bearer"


def test_jwks_refetched_after_cache_time(idp):
    now = [time.time()]
    v = tv.Validator(idp.config(jwks_cache_seconds=100), timer=lambda: now[0])
    check(v, idp.token(client_id=EXEMPT))
    now[0] += 101
    check(v, idp.token(client_id=EXEMPT))
    assert idp.hits["jwks"] == 2


# -- Basic and other schemes --------------------------------------------------

def test_basic_passes_through_when_enabled(idp):
    v = tv.Validator(idp.config())
    creds = base64.b64encode(b"tes:secret").decode()
    assert v.check(f"Basic {creds}").kind == "basic"
    assert v.check(f"basic {creds}").kind == "basic"


def test_basic_refused_when_disabled(idp):
    v = tv.Validator(idp.config(basic_auth_enabled=False))
    creds = base64.b64encode(b"tes:secret").decode()
    denied(v, f"Basic {creds}", "Basic auth is disabled")


def test_malformed_basic(idp):
    v = tv.Validator(idp.config())
    denied(v, "Basic !!!", "malformed")
    # Not what tesp-api reads as credentials.
    creds = base64.b64encode(b"tes:secret").decode()
    denied(v, f"Basic  {creds}", "malformed")
    denied(v, f"Basic\t{creds}", "unsupported")
    denied(v, "Basic ", "malformed")
    denied(v, "Basic " + base64.b64encode(b"nocolon").decode(), "malformed")


def test_missing_and_unknown_schemes(idp):
    v = tv.Validator(idp.config())
    denied(v, None, "no Authorization")
    denied(v, "", "no Authorization")
    denied(v, "Digest abc", "unsupported")


def test_bearer_without_issuers():
    v = tv.Validator(tv.Config.from_dict({"basic_auth_enabled": True}))
    denied(v, "Bearer a.b.c", "no token issuer")


# -- configuration ------------------------------------------------------------

@pytest.mark.parametrize("issuer, message", [
    ({"issuer": "http://idp.example.org/"}, "https"),
    ({"algorithms": ["HS256"]}, "algorithms"),
    ({"algorithms": ["none"]}, "algorithms"),
    ({"algorithms": []}, "algorithms"),
    ({"allowed_client_ids": []}, "allowed_client_ids must not be empty"),
    ({"allowed_client_ids": "c"}, "list of non-empty strings"),
    ({"exempt_client_ids": ["other"]}, "exempt_client_ids must also"),
    ({"entitlements": {}}, "set entitlements"),
    ({"require_entitlement": "no"}, "true or false"),
    ({"jwks_uri": "http://idp.example.org/jwks"}, "jwks_uri"),
    ({"discovery_url": "ftp://x"}, "discovery_url"),
])
def test_invalid_issuer_config(issuer, message):
    base = {"issuer": "https://idp.example.org/", "allowed_client_ids": ["c"],
            "entitlements": {"any_of": ["e"]}}
    with pytest.raises(ValueError, match=message):
        tv.Config.from_dict({"issuers": [{**base, **issuer}]})


@pytest.mark.parametrize("config, message", [
    ({"basic_auth_enabled": False}, "neither"),
    ({"audit_tag_keys": "user_sub"}, "audit_tag_keys"),
    ({"basic_auth_enabled": "yes"}, "true or false"),
    ({"leeway_seconds": -1}, "leeway_seconds"),
    ({"http_timeout_seconds": True}, "http_timeout_seconds"),
    ({"basic_auth_enabled": True, "http_timeout_seconds": 0},
     "http_timeout_seconds must be above 0"),
    ({"jwks_max_stale_seconds": float("nan")},
     "jwks_max_stale_seconds must be a number from"),
    ({"leeway_seconds": float("inf")}, "leeway_seconds must be a number from"),
    ({"http_timeout_seconds": 1e300},
     "http_timeout_seconds must be a number from"),
    ({"cache_max_entries": 10 ** 400}, "cache_max_entries must be a number from"),
    ({"leeway_seconds": tv.MAX_CONFIG_NUMBER + 1},
     "leeway_seconds must be a number from"),
    ({"basic_auth_enabled": True, "jwks_cache_seconds": 100,
      "jwks_max_stale_seconds": 99}, "at least jwks_cache_seconds"),
    ({"basic_auth_enabled": True, "jwks_cache_seconds": 0,
      "jwks_max_stale_seconds": 0}, "above 0"),
])
def test_invalid_config(config, message):
    with pytest.raises(ValueError, match=message):
        tv.Config.from_dict(config)


def test_config_numbers_may_reach_the_bound():
    config = tv.Config.from_dict({"leeway_seconds": tv.MAX_CONFIG_NUMBER})
    assert config.leeway_seconds == tv.MAX_CONFIG_NUMBER


def test_duplicate_issuer():
    i = {"issuer": "https://idp.example.org/", "allowed_client_ids": ["c"],
         "entitlements": {"any_of": ["e"]}}
    with pytest.raises(ValueError, match="twice"):
        tv.Config.from_dict({"issuers": [i, i]})


def test_valid_config_defaults():
    c = tv.Config.from_dict({"issuers": [{
        "issuer": "https://login.aai.lifescience-ri.eu/oidc/",
        "allowed_client_ids": ["c"], "entitlements": {"prefix_any_of": ["p:"]},
        "userinfo_endpoint": "https://login.aai.lifescience-ri.eu/oidc/userinfo",
    }], "http_timeout_seconds": 3})
    i = c.issuers["https://login.aai.lifescience-ri.eu/oidc/"]
    assert i.discovery_url == (
        "https://login.aai.lifescience-ri.eu/oidc/.well-known/openid-configuration")
    assert i.algorithms == ("RS256",)
    assert c.http_timeout_seconds == 3 and c.leeway_seconds == 60


# -- fetch_json ---------------------------------------------------------------

def test_fetch_json_errors(idp):
    with pytest.raises(tv.FetchError, match="not JSON"):
        tv.fetch_json(idp.base + "/notjson", 2)
    with pytest.raises(tv.FetchError, match="too large"):
        tv.fetch_json(idp.base + "/huge", 2)
    with pytest.raises(tv.FetchError, match="HTTPError 404"):
        tv.fetch_json(idp.base + "/missing", 2)
    with pytest.raises(tv.FetchError):
        tv.fetch_json("http://127.0.0.1:1/", 2)


def test_fetch_json_sends_the_accept_header_asked_for(idp):
    tv.fetch_json(idp.base + "/jwks", 2)
    assert idp.accept["/jwks"] == "application/json"
    tv.fetch_json(idp.base + "/jwks", 2, accept="*/*")
    assert idp.accept["/jwks"] == "*/*"


def test_default_issuer_timeout_is_ten_seconds():
    c = tv.Config.from_dict({"basic_auth_enabled": True})
    assert c.http_timeout_seconds == 10


def test_fetch_json_error_does_not_contain_the_token(idp):
    idp.userinfo_status = 500
    token = idp.token()
    with pytest.raises(tv.FetchError) as e:
        tv.fetch_json(idp.base + "/userinfo", 2, token=token)
    assert token not in str(e.value)


# -- HTTP interface -----------------------------------------------------------

@pytest.fixture
def service(idp):
    server = tv.make_server(tv.Validator(idp.config()), "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def call(url, authorization=None):
    req = urllib.request.Request(url)
    if authorization is not None:
        req.add_header("Authorization", authorization)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers)


def test_http_allowed_bearer(idp, service):
    status, headers = call(service + "/auth", "Bearer " + idp.token())
    assert status == 200 and headers["X-Tes-Auth-Kind"] == "bearer"
    assert headers["X-Tes-Auth-Client"] == CLIENT
    assert headers["X-Tes-Auth-Exempt"] == "0"


def test_http_allowed_exempt_client(idp, service):
    status, headers = call(service + "/auth", "Bearer " + idp.token(client_id=EXEMPT))
    assert status == 200 and headers["X-Tes-Auth-Exempt"] == "1"
    assert headers["X-Tes-Auth-Client"] == EXEMPT


def test_http_allowed_basic(service):
    status, headers = call(service + "/auth",
                           "Basic " + base64.b64encode(b"a:b").decode())
    assert status == 200 and headers["X-Tes-Auth-Kind"] == "basic"
    assert "X-Tes-Auth-Client" not in headers


# -- audit of tasks from exempt clients ---------------------------------------

def post(url, body, headers):
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


@pytest.fixture
def audit_service(idp):
    server = tv.make_server(
        tv.Validator(idp.config(audit_tag_keys=["user_sub"])), "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_audit_logs_the_identity_tag_only(audit_service, caplog):
    task = {"name": "t", "tags": {"user_sub": "abc@lifescience-ri.eu",
                                  "secret": "do-not-log"},
            "executors": [{"image": "x", "env": {"PASSWORD": "do-not-log"}}]}
    with caplog.at_level("INFO", logger="tes-validator"):
        status = post(audit_service + "/audit", json.dumps(task).encode(),
                      {"X-Tes-Auth-Exempt": "1", "X-Tes-Auth-Client": EXEMPT})
    assert status == 204
    assert (f"audit: [-] task from exempt client {EXEMPT} for "
            "user_sub=abc@lifescience-ri.eu") in caplog.text
    assert "do-not-log" not in caplog.text


def test_audit_ignores_non_exempt_requests(audit_service, caplog):
    with caplog.at_level("INFO", logger="tes-validator"):
        assert post(audit_service + "/audit", b'{"tags": {"user_sub": "x"}}',
                    {"X-Tes-Auth-Exempt": "0"}) == 204
        assert post(audit_service + "/audit", b"{}", {}) == 204
    assert "audit" not in caplog.text


def test_audit_rejects_bad_requests(audit_service):
    assert post(audit_service + "/other", b"{}", {}) == 404


def test_audit_logs_a_task_too_large_to_read(audit_service, caplog):
    # Only the header: the validator answers before reading the body, so
    # sending a real 1 MiB body could end in a broken pipe.
    host, port = audit_service.removeprefix("http://").split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=5)
    with caplog.at_level("INFO", logger="tes-validator"):
        conn.putrequest("POST", "/audit")
        for name, value in {"Content-Length": str(tv.MAX_AUDIT_BODY_BYTES + 1),
                            "X-Tes-Auth-Exempt": "1", "X-Tes-Auth-Client": EXEMPT,
                            "X-Request-Id": "r1"}.items():
            conn.putheader(name, value)
        conn.endheaders()
        status = conn.getresponse().status
        conn.close()
    assert status == 413
    assert f"audit: [r1] task from exempt client {EXEMPT}; body too large" in caplog.text


def test_audit_when_too_busy(idp, caplog, monkeypatch):
    server = tv.make_server(tv.Validator(idp.config()), "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(server.RequestHandlerClass.slots, "acquire",
                        lambda timeout: False)
    try:
        with caplog.at_level("INFO", logger="tes-validator"):
            status = post(f"http://127.0.0.1:{server.server_address[1]}/audit",
                          b"{}", {"X-Tes-Auth-Exempt": "1",
                                  "X-Tes-Auth-Client": EXEMPT})
    finally:
        server.shutdown()
    assert status == 503 and "too busy" in caplog.text


@pytest.mark.parametrize("body, keys, expected", [
    (b'{"tags": {"user_sub": "u1"}}', ("user_sub",), "for user_sub=u1"),
    (b'{"tags": {"other": "u1"}}', ("user_sub",), "for nobody (no audit tags)"),
    (b"not json", ("user_sub",), "for nobody"),
    (b'{"tags": ["a"]}', ("user_sub",), "for nobody"),
    (b'["a"]', ("user_sub",), "for nobody"),
    (json.dumps({"tags": {"user_sub": "a\nFAKE LINE\u00e9"}}).encode(),
     ("user_sub",), "user_sub=a?FAKE LINE?"),
    (b'{"tags": {"b": "1", "a": "2"}}', (), "no audit_tag_keys set; tags present: a, b"),
    (b"{}", (), "tags present: none"),
])
def test_audit_line(body, keys, expected):
    line = tv.audit_line("svc\n", body, keys)
    assert line.startswith("task from exempt client svc? ")
    assert expected in line
    assert "\n" not in line


def test_http_issuer_outage_is_503(idp, service, caplog):
    idp.jwks_status = 503
    with caplog.at_level("INFO", logger="tes-validator"):
        status, headers = call(service + "/auth", "Bearer " + idp.token())
    assert status == 503 and "WWW-Authenticate" not in headers
    assert "unavailable: JWKS fetch failed" in caplog.text


def test_http_denied(idp, service, caplog):
    token = idp.token(client_id="someone-else")
    with caplog.at_level("INFO", logger="tes-validator"):
        status, headers = call(service + "/auth", "Bearer " + token)
    assert status == 401
    assert headers["WWW-Authenticate"] == 'Bearer realm="tes", error="invalid_token"'
    assert "X-Tes-Auth-Kind" not in headers
    assert "client not allowed" in caplog.text
    assert token not in caplog.text
    status, headers = call(service + "/auth")
    assert status == 401 and headers["WWW-Authenticate"] == 'Bearer realm="tes"'


def test_http_unexpected_error_is_a_401(idp, caplog):
    v = tv.Validator(idp.config())
    token = idp.token()

    def broken(authorization):
        raise RuntimeError(f"bug while reading {authorization}")

    v.check = broken
    server = tv.make_server(v, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with caplog.at_level("INFO", logger="tes-validator"):
            status, headers = call(
                f"http://127.0.0.1:{server.server_address[1]}/auth",
                "Bearer " + token)
    finally:
        server.shutdown()
    assert status == 401 and "X-Tes-Auth-Kind" not in headers
    assert "validator error: RuntimeError" in caplog.text
    assert token not in caplog.text


def test_http_too_many_concurrent_checks(idp, monkeypatch):
    server = tv.make_server(tv.Validator(idp.config()), "127.0.0.1", 0,
                            max_concurrent=1)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    # A full pool: acquiring a slot times out.
    monkeypatch.setattr(server.RequestHandlerClass.slots, "acquire",
                        lambda timeout: False)
    try:
        status, _ = call(f"http://127.0.0.1:{server.server_address[1]}/auth",
                         "Bearer " + idp.token())
    finally:
        server.shutdown()
    assert status == 503


def test_http_other_paths(service):
    assert call(service + "/healthz")[0] == 200
    assert call(service + "/other")[0] == 404


def test_main_loads_config(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"basic_auth_enabled": True}))
    started = {}

    class FakeServer:
        def serve_forever(self):
            started["yes"] = True

    monkeypatch.setattr(tv, "make_server",
                        lambda validator, host, port: FakeServer())
    assert tv.main(["--config", str(cfg), "--port", "0"]) == 0
    assert started == {"yes": True}


# -- hardening (issue #36) ---------------------------------------------------

def test_server_has_a_listen_backlog():
    server = tv.make_server(tv.Validator(tv.Config.from_dict({})), "127.0.0.1", 0)
    try:
        assert server.request_queue_size >= 128
    finally:
        server.server_close()


def test_jwks_fetch_doesnt_block_known_keys(idp):
    """A slow refresh (e.g. for an unknown kid) holds no lock that a token
    with a known, fresh key needs; concurrent callers share one fetch."""
    gate = threading.Event()
    calls = []

    def slow_fetch(url, timeout, token=None, **kwargs):
        if url.endswith("/jwks") and calls:
            gate.wait(5)
        calls.append(url)
        return tv.fetch_json(url, timeout, token, **kwargs)

    v = tv.Validator(idp.config(), fetch=slow_fetch)
    check(v, idp.token())  # keys and endpoints fetched, fresh
    jwks_calls = sum(u.endswith("/jwks") for u in calls)
    results = []
    # min refresh interval passed: an unknown kid triggers a (slow) refresh
    v._state[idp.issuer].attempted_at = v._timer() - 3600
    slow = threading.Thread(target=lambda: results.append(
        pytest.raises(tv.Denied, check, v, idp.token(kid="rotated"))))
    slow.start()
    time.sleep(0.2)
    start = time.time()
    # A user token with a known key; its userinfo lookup needs no lock either.
    assert check(v, idp.token(sub="user-1", iat=2)).kind == "bearer"
    assert time.time() - start < 1, "a known key waited for the refresh"
    gate.set()
    slow.join(5)
    assert results
    assert sum(u.endswith("/jwks") for u in calls) == jwks_calls + 1


def test_userinfo_refusal_is_cached_briefly(idp):
    idp.userinfo_status = 401
    now = [time.time()]
    v = tv.Validator(idp.config(), clock=lambda: now[0])
    token = idp.token()
    for _ in range(3):
        denied(v, "Bearer " + token, "userinfo failed")
    assert idp.hits["userinfo"] == 1
    now[0] += 61
    denied(v, "Bearer " + token, "userinfo failed")
    assert idp.hits["userinfo"] == 2


def _slow_jwks(gate):
    calls = []

    def fetch(url, timeout, token=None, **kwargs):
        if url.endswith("/jwks"):
            calls.append(url)
            gate.wait(5)
        return tv.fetch_json(url, timeout, token, **kwargs)
    return fetch, calls


def test_concurrent_requests_share_one_jwks_fetch(idp):
    gate = threading.Event()
    fetch, calls = _slow_jwks(gate)
    v = tv.Validator(idp.config(), fetch=fetch)
    results = []
    workers = [threading.Thread(target=lambda: results.append(
        check(v, idp.token(client_id=EXEMPT)).kind)) for _ in range(3)]
    for w in workers:
        w.start()
    time.sleep(0.3)
    gate.set()
    for w in workers:
        w.join(10)
    assert results == ["bearer"] * 3
    assert len(calls) == 1


def test_waiting_for_a_failed_first_fetch_is_unavailable(idp):
    gate = threading.Event()
    fetch, calls = _slow_jwks(gate)
    idp.jwks_status = 503
    v = tv.Validator(idp.config(), fetch=fetch)
    errors = []

    def run():
        try:
            check(v, idp.token(client_id=EXEMPT))
        except tv.Denied as e:
            errors.append(type(e))
    workers = [threading.Thread(target=run) for _ in range(2)]
    for w in workers:
        w.start()
    time.sleep(0.3)
    gate.set()
    for w in workers:
        w.join(10)
    assert errors == [tv.Unavailable, tv.Unavailable]
    assert len(calls) == 1


def test_a_hung_refresh_is_given_up_on_after_the_backoff(idp):
    now = [time.time()]
    v = tv.Validator(idp.config(http_timeout_seconds=1), timer=lambda: now[0])
    state = v._state[idp.issuer]
    state.refreshing = threading.Event()  # a fetch that never ends
    # Abandoned (older than 2 x timeout + 1) but within the back-off: no new
    # fetch yet, so hung fetches can't pile up.
    state.attempted_at = now[0] - 10
    with pytest.raises(tv.Unavailable):
        check(v, idp.token(client_id=EXEMPT))
    assert idp.hits["jwks"] == 0
    # Past the back-off: a new fetch.
    now[0] += 60
    assert check(v, idp.token(client_id=EXEMPT)).kind == "bearer"
    assert state.refreshing is None and idp.hits["jwks"] == 1


def test_an_abandoned_fetch_publishes_while_a_newer_one_runs(idp):
    """Fetch 1, given up on, finishes while fetch 2 runs. No newer keys are
    published, so fetch 1 publishes. Fetch 2 still owns `refreshing` and
    replaces them if it succeeds."""
    v = tv.Validator(idp.config())
    state = v._state[idp.issuer]
    newer = threading.Event()
    state.refreshing, state.generation = newer, 2
    old = threading.Event()
    v._fetch_jwks(v.config.issuers[idp.issuer], state, v._timer(), old, 1)
    assert old.is_set() and state.refreshing is newer
    assert state.keys and state.published == 1


def test_fetch_json_has_a_total_deadline(idp):
    start = time.monotonic()
    with pytest.raises(tv.FetchError, match="too slow"):
        tv.fetch_json(idp.base + "/trickle", 1)
    assert time.monotonic() - start < 3


def test_waiting_in_vain_for_an_unknown_kid_is_unavailable(idp):
    now = [time.time()]
    v = tv.Validator(idp.config(http_timeout_seconds=0.1), timer=lambda: now[0])
    check(v, idp.token(client_id=EXEMPT))
    state = v._state[idp.issuer]
    state.refreshing = threading.Event()  # someone else fetches, slowly
    state.attempted_at = now[0]
    with pytest.raises(tv.Unavailable, match="still running"):
        check(v, idp.token(kid="rotated"))


def test_discovery_with_a_bad_userinfo_url_sets_no_endpoint(idp):
    # jwks_uri from the configuration (https) passes; the discovered
    # userinfo endpoint (plain http here) doesn't: then neither is set.
    config = dataclasses.replace(idp.config(issuer_overrides={
        "jwks_uri": "https://jwks.example.org/jwks"}), allow_http=False)
    v = tv.Validator(config)
    denied(v, "Bearer " + idp.token(), "userinfo_endpoint must be an https")
    state = v._state[idp.issuer]
    assert state.jwks_uri is None and state.userinfo_endpoint is None


def test_server_closes_connections_beyond_the_thread_cap(idp, monkeypatch, caplog):
    server = tv.make_server(tv.Validator(idp.config()), "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(tv, "MAX_THREADS", 0)
    try:
        with caplog.at_level("WARNING", logger="tes-validator"):
            with pytest.raises((urllib.error.URLError, ConnectionError, http.client.HTTPException)):
                urllib.request.urlopen(
                    f"http://127.0.0.1:{server.server_address[1]}/healthz", timeout=5)
    finally:
        monkeypatch.setattr(tv, "MAX_THREADS", 100)
        server.shutdown()
    assert "too many connections" in caplog.text


def test_unknown_kid_during_backoff_after_a_failed_fetch_is_unavailable(idp):
    now = [time.time()]
    v = tv.Validator(idp.config(), timer=lambda: now[0])
    check(v, idp.token(client_id=EXEMPT))  # keys known
    idp.jwks_status = 503
    now[0] += 61
    # The refresh for the rotated kid fails; the next try is a minute away.
    with pytest.raises(tv.Denied):
        check(v, idp.token(kid="rotated"))
    now[0] += 10
    with pytest.raises(tv.Unavailable, match="retrying after"):
        check(v, idp.token(kid="rotated"))
    # Known keys still work meanwhile, and a token without a kid (not a
    # rotated key) is checked against the one key known.
    assert check(v, idp.token(client_id=EXEMPT)).kind == "bearer"
    assert check(v, idp.token(kid=None)).kind == "bearer"


def test_a_late_fetch_publishes_if_nothing_newer_was_published(idp):
    v = tv.Validator(idp.config())
    state = v._state[idp.issuer]
    old = threading.Event()  # given up on: refreshing was cleared
    v._fetch_jwks(v.config.issuers[idp.issuer], state, v._timer(), old, 1)
    assert state.keys and state.fetched_at is not None and state.published == 1


def test_a_late_fetch_doesnt_bring_back_older_keys(idp):
    """A fetch given up on, finishing after a newer refresh completed, must
    not publish its (older) keys: a revoked kid would be accepted again."""
    v = tv.Validator(idp.config())
    state = v._state[idp.issuer]
    issuer = v.config.issuers[idp.issuer]
    started = v._timer()
    newer = {"new-kid": object()}
    # Fetch 2 started later and completed first.
    state.keys, state.fetched_at, state.published = newer, started + 5, 2
    v._fetch_jwks(issuer, state, started, threading.Event(), 1)
    assert state.keys is newer and state.fetched_at == started + 5


def test_jwks_timing_ignores_wall_clock_steps(idp):
    """JWKS ages and the back-off follow the timer (monotonic), not the
    wall clock: after the wall clock steps back an hour, a rotated key is
    still fetched after the back-off, and old keys still age out."""
    wall, mono = [time.time()], [1000.0]
    v = tv.Validator(idp.config(jwks_cache_seconds=100),
                     clock=lambda: wall[0], timer=lambda: mono[0])
    check(v, idp.token(client_id=EXEMPT))
    state = v._state[idp.issuer]
    wall[0] -= 3600
    # The issuer rotated its key; the back-off has passed.
    state.keys = {"revoked": object()}
    mono[0] += 61
    check(v, idp.token(client_id=EXEMPT))  # its kid is unknown now
    assert state.published == 2 and "revoked" not in state.keys
    # Known keys age out after jwks_cache_seconds.
    mono[0] += 100
    check(v, idp.token(client_id=EXEMPT))
    assert idp.hits["jwks"] == 3


def test_a_kidless_token_doesnt_wait_for_a_refresh(idp):
    """Like a known kid, a kid-less token with the one key known is checked
    at once while another request refreshes, not after abandoned_after."""
    now = [time.time()]
    v = tv.Validator(idp.config(http_timeout_seconds=5), timer=lambda: now[0])
    check(v, idp.token(client_id=EXEMPT))
    state = v._state[idp.issuer]
    state.refreshing = threading.Event()  # someone else fetches, slowly
    state.attempted_at = now[0]
    start = time.monotonic()
    assert check(v, idp.token(kid=None)).kind == "bearer"
    assert time.monotonic() - start < 2


def test_unknown_kid_right_after_a_failed_fetch_is_unavailable(idp):
    now = [time.time()]
    v = tv.Validator(idp.config(), timer=lambda: now[0])
    check(v, idp.token(client_id=EXEMPT))
    idp.jwks_status = 503
    now[0] += 61
    # The fetch for the rotated kid fails: 503 at once, not 401.
    with pytest.raises(tv.Unavailable, match="JWKS fetch failed"):
        check(v, idp.token(kid="rotated"))
