"""Token check for the TES gateway (TESP-src issue #26).

nginx in front of tesp-api sends every protected request here first
(auth_request) with the client's Authorization header, and no body:

- ``Authorization: Bearer <JWT>``: an OAuth2 access token (RFC 9068) of a
  configured issuer, checked against that issuer's configuration only. On
  success the answer carries ``X-Tes-Auth-Kind: bearer``, and nginx replaces
  the header with tesp-api's own Basic credentials.
- ``Authorization: Basic ...``: let through unchecked when Basic is enabled
  (``X-Tes-Auth-Kind: basic``); nginx then forwards the header unchanged and
  tesp-api checks the credentials.
- Anything else, and every failure, is 401.

For tokens of exempt clients (a service such as proTES, which submits tasks
on behalf of users and names them in task tags), nginx also mirrors task
submissions to ``POST /audit``; the validator logs the configured identity
tags of the task (``audit_tag_keys``), and nothing else of it.

Nothing in a token decides where keys or user info come from: the issuer must
match the configuration exactly, and the discovery document, JWKS and
userinfo endpoint are those of the configuration. Tokens are never logged or
stored; the userinfo cache is keyed by a SHA-256 of the token.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import logging
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Literal

import jwt
from jwt.algorithms import get_default_algorithms

log = logging.getLogger("tes-validator")

# Signatures with a public key only: no "none", no HS* (a shared secret would
# let anyone who knows it, e.g. any client of the issuer, mint tokens).
ASYMMETRIC_ALGORITHMS = frozenset(
    {"RS256", "RS384", "RS512", "PS256", "PS384", "PS512",
     "ES256", "ES384", "ES512"}
)
# RFC 9068 section 2.1; media types are case-insensitive.
ACCESS_TOKEN_TYPES = frozenset({"at+jwt", "application/at+jwt"})
# Three base64url parts. Bounded, so a huge header costs nothing to reject.
JWS_RE = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
MAX_TOKEN_LENGTH = 16384
MAX_RESPONSE_BYTES = 1 << 20    # from the issuer
MAX_AUDIT_BODY_BYTES = 1 << 20  # a task sent to /audit
REFUSAL_CACHE_SECONDS = 60      # userinfo refused a token (4xx)
REFUSED = "refused"             # userinfo cache value for such a token
LISTEN_BACKLOG = 128            # connections waiting for accept()


class Denied(Exception):
    """The request is not authorised. The message is logged, never sent."""


class Unavailable(Denied):
    """The issuer can't be reached, so the token can't be checked (503)."""


class Forbidden(Denied):
    """A valid token of a user without the required group (403): logging in
    again wouldn't help, so not 401."""


class FetchError(Exception):
    """An identity provider endpoint could not be read.

    status is the HTTP status the endpoint answered with, None if it didn't.
    """

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status

    @property
    def unavailable(self) -> bool:
        """The endpoint is down or broken, rather than refusing the token."""
        # 429: the issuer rate-limits us; not the token's fault.
        return self.status is None or self.status >= 500 or self.status == 429


def _https_only(url: str, what: str, allow_http: bool) -> str:
    """The URL if it is https (or http when allowed), else ValueError."""
    if not isinstance(url, str) or not (
        url.startswith("https://") or (allow_http and url.startswith("http://"))
    ):
        raise ValueError(f"{what} must be an https:// URL")
    return url


def _string_list(value: Any, what: str) -> tuple[str, ...]:
    """A list of non-empty strings as a tuple (None: empty), else ValueError."""
    if value is None:
        return ()
    if not isinstance(value, list) or not all(
        isinstance(v, str) and v for v in value
    ):
        raise ValueError(f"{what} must be a list of non-empty strings")
    return tuple(value)


@dataclass(frozen=True)
class IssuerConfig:
    """One trusted token issuer, and which of its tokens are let in."""

    issuer: str
    discovery_url: str
    algorithms: tuple[str, ...]
    allowed_client_ids: frozenset[str]
    exempt_client_ids: frozenset[str]
    entitlements_any_of: frozenset[str]
    entitlements_prefix_any_of: tuple[str, ...]
    require_entitlement: bool
    audiences: tuple[str, ...] = ()
    exempt_audiences: frozenset[str] = frozenset()
    jwks_uri: str | None = None
    userinfo_endpoint: str | None = None

    @classmethod
    def from_dict(cls, d: dict, allow_http: bool = False) -> IssuerConfig:
        issuer = _https_only(d.get("issuer"), "issuer", allow_http)
        discovery = d.get("discovery_url") or (
            issuer.rstrip("/") + "/.well-known/openid-configuration"
        )
        algorithms = _string_list(d.get("algorithms", ["RS256"]), "algorithms")
        if not algorithms or not set(algorithms) <= ASYMMETRIC_ALGORITHMS:
            raise ValueError(
                f"{issuer}: algorithms must be a non-empty subset of "
                f"{sorted(ASYMMETRIC_ALGORITHMS)}"
            )
        allowed = frozenset(
            _string_list(d.get("allowed_client_ids"), "allowed_client_ids")
        )
        if not allowed:
            raise ValueError(f"{issuer}: allowed_client_ids must not be empty")
        exempt = frozenset(
            _string_list(d.get("exempt_client_ids"), "exempt_client_ids")
        )
        if not exempt <= allowed:
            raise ValueError(
                f"{issuer}: exempt_client_ids must also be in allowed_client_ids"
            )
        audiences = _string_list(d.get("audiences"), "audiences")
        # Exempt (service) clients skip the user checks, so their tokens must
        # be bound to this service: one the same client got for another
        # backend must not work here. Checked on exempt tokens only, so user
        # tokens needn't carry it; and never a client id, which every token
        # of that client would carry.
        exempt_audiences = frozenset(
            _string_list(d.get("exempt_audiences"), "exempt_audiences"))
        if exempt and not exempt_audiences:
            raise ValueError(f"{issuer}: exempt_client_ids need exempt_audiences")
        if exempt_audiences & allowed:
            raise ValueError(
                f"{issuer}: exempt_audiences must name this service, not a client")
        ents = d.get("entitlements") or {}
        any_of = frozenset(_string_list(ents.get("any_of"), "entitlements.any_of"))
        prefixes = _string_list(
            ents.get("prefix_any_of"), "entitlements.prefix_any_of"
        )
        # A boundary, so "...:ECP_CLN:" can't match "...:ECP_CLN_OTHER".
        if not all(p.endswith(":") for p in prefixes):
            raise ValueError(
                f"{issuer}: every entitlements.prefix_any_of entry must end "
                "with ':' (a group and its subgroups)"
            )
        require = d.get("require_entitlement", True)
        if not isinstance(require, bool):
            raise ValueError(f"{issuer}: require_entitlement must be true or false")
        if require and not (any_of or prefixes):
            raise ValueError(
                f"{issuer}: set entitlements.any_of or prefix_any_of, or "
                "require_entitlement: false"
            )
        jwks_uri = d.get("jwks_uri")
        userinfo = d.get("userinfo_endpoint")
        return cls(
            issuer=issuer,
            discovery_url=_https_only(discovery, "discovery_url", allow_http),
            algorithms=algorithms,
            allowed_client_ids=allowed,
            exempt_client_ids=exempt,
            entitlements_any_of=any_of,
            entitlements_prefix_any_of=prefixes,
            require_entitlement=require,
            audiences=audiences,
            exempt_audiences=exempt_audiences,
            jwks_uri=_https_only(jwks_uri, "jwks_uri", allow_http)
            if jwks_uri else None,
            userinfo_endpoint=_https_only(userinfo, "userinfo_endpoint", allow_http)
            if userinfo else None,
        )

    def entitled(self, values: list[str]) -> bool:
        return any(
            v in self.entitlements_any_of
            or any(v.startswith(p) for p in self.entitlements_prefix_any_of)
            for v in values
        )


@dataclass(frozen=True)
class Config:
    """The validator's configuration file (rendered by Ansible)."""

    issuers: dict[str, IssuerConfig]
    basic_auth_enabled: bool = True
    leeway_seconds: int = 60
    http_timeout_seconds: float = 5.0
    jwks_cache_seconds: int = 3600
    jwks_min_refresh_seconds: int = 60
    # Known keys stay in use while the issuer can't be reached, this long.
    jwks_max_stale_seconds: int = 86400
    userinfo_cache_seconds: int = 300
    cache_max_entries: int = 10000
    # Task tags that name the user for whom an exempt client submits a task;
    # logged for audit.
    audit_tag_keys: tuple[str, ...] = ()
    # Only for the unit tests' plain-HTTP identity provider; never read from
    # the configuration file.
    allow_http: bool = False

    @classmethod
    def from_dict(cls, d: dict, allow_http: bool = False) -> Config:
        issuers = {}
        for item in d.get("issuers") or []:
            ic = IssuerConfig.from_dict(item, allow_http)
            if ic.issuer in issuers:
                raise ValueError(f"issuer {ic.issuer} is configured twice")
            issuers[ic.issuer] = ic
        basic = d.get("basic_auth_enabled", True)
        if not isinstance(basic, bool):
            raise ValueError("basic_auth_enabled must be true or false")
        if not issuers and not basic:
            raise ValueError("neither an issuer nor Basic auth is enabled")
        numbers = {}
        for name in ("leeway_seconds", "http_timeout_seconds",
                     "jwks_cache_seconds", "jwks_min_refresh_seconds",
                     "jwks_max_stale_seconds",
                     "userinfo_cache_seconds", "cache_max_entries"):
            if name in d:
                value = d[name]
                if isinstance(value, bool) or not isinstance(value, (int, float)) \
                        or value < 0:
                    raise ValueError(f"{name} must be a number >= 0")
                numbers[name] = value
        audit = _string_list(d.get("audit_tag_keys"), "audit_tag_keys")
        return cls(issuers=issuers, basic_auth_enabled=basic,
                   audit_tag_keys=audit, allow_http=allow_http, **numbers)


def fetch_json(url: str, timeout: float, token: str | None = None) -> Any:
    """GET a JSON document, with a timeout and a size limit."""
    headers = {"Accept": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except (urllib.error.URLError, OSError, ValueError) as e:
        # HTTPError is a URLError; its message has the status, not the token.
        status = getattr(e, "code", None)
        raise FetchError(f"{url}: {type(e).__name__} {status or ''}",
                         status if isinstance(status, int) else None) from None
    if len(body) > MAX_RESPONSE_BYTES:
        raise FetchError(f"{url}: response too large")
    try:
        return json.loads(body)
    except ValueError:
        raise FetchError(f"{url}: not JSON") from None


class TTLCache:
    """A small thread-safe LRU cache whose entries expire."""

    def __init__(self, max_entries: int, clock: Callable[[], float]):
        self._data: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._max = max_entries
        self._clock = clock
        self._lock = threading.Lock()

    def get(self, key: str) -> Any:
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            expires, value = item
            if expires <= self._clock():
                del self._data[key]
                return None
            self._data.move_to_end(key)
            return value

    def put(self, key: str, value: Any, expires: float) -> None:
        if self._max <= 0:
            return
        with self._lock:
            self._data[key] = (expires, value)
            self._data.move_to_end(key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)


@dataclass
class _IssuerState:
    lock: threading.Lock = field(default_factory=threading.Lock)
    jwks_uri: str | None = None
    userinfo_endpoint: str | None = None
    keys: dict[str | None, Any] = field(default_factory=dict)
    fetched_at: float | None = None    # last successful JWKS fetch
    attempted_at: float | None = None  # last attempt, successful or not
    # Set while one request fetches the JWKS; others wait on it, without
    # holding the lock (which guards only these fields, never a fetch).
    refreshing: threading.Event | None = None


@dataclass(frozen=True)
class Decision:
    """Why a request is let in: Basic credentials or a valid token."""

    kind: Literal["basic", "bearer"]
    reason: str = ""
    client_id: str = ""
    exempt: bool = False


class Validator:
    """Checks Authorization headers against the configuration."""

    def __init__(self, config: Config,
                 fetch: Callable[..., Any] = fetch_json,
                 clock: Callable[[], float] = time.time):
        self.config = config
        self._fetch = fetch
        self._clock = clock
        self._state = {iss: _IssuerState() for iss in config.issuers}
        self._userinfo = TTLCache(config.cache_max_entries, clock)
        self._warned = TTLCache(1000, clock)
        self._algorithms = get_default_algorithms()

    # -- Authorization header -------------------------------------------

    def check(self, authorization: str | None) -> Decision:
        """Returns the decision, or raises Denied."""
        if not authorization:
            raise Denied("no Authorization header")
        # As tesp-api (FastAPI) splits it: the scheme, one space, the rest.
        scheme, _, credentials = authorization.partition(" ")
        scheme = scheme.lower()
        if scheme == "basic":
            if not self.config.basic_auth_enabled:
                raise Denied("Basic auth is disabled")
            # Passed on unchanged, so it must be what tesp-api can read.
            if not _is_basic_credentials(credentials):
                raise Denied("malformed Basic credentials")
            return Decision("basic", "Basic, checked by tesp-api")
        if scheme == "bearer":
            if not self.config.issuers:
                raise Denied("no token issuer is configured")
            return self._check_token(credentials.strip())
        raise Denied("unsupported Authorization scheme")

    # -- Bearer tokens --------------------------------------------------

    def _check_token(self, token: str) -> Decision:
        if len(token) > MAX_TOKEN_LENGTH or not JWS_RE.fullmatch(token):
            raise Denied("not a signed JWT")
        try:
            header = jwt.get_unverified_header(token)
            unverified = jwt.decode(token, options={"verify_signature": False})
        except jwt.PyJWTError as e:
            raise Denied(f"unreadable token ({type(e).__name__})") from None
        typ = header.get("typ")
        if not isinstance(typ, str) or typ.lower() not in ACCESS_TOKEN_TYPES:
            raise Denied("not an access token (typ is not at+jwt)")
        iss = unverified.get("iss")
        issuer = self.config.issuers.get(iss) if isinstance(iss, str) else None
        if issuer is None:
            raise Denied("issuer not configured")
        alg = header.get("alg")
        if alg not in issuer.algorithms:
            raise Denied("algorithm not allowed")
        key = self._signing_key(issuer, header.get("kid"), alg)
        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=[alg],
                issuer=issuer.issuer,
                audience=list(issuer.audiences) or None,
                leeway=self.config.leeway_seconds,
                options={
                    "require": ["exp", "iss", "sub", "client_id"],
                    "verify_aud": bool(issuer.audiences),
                },
            )
        except jwt.PyJWTError as e:
            raise Denied(f"invalid token ({type(e).__name__})") from None
        client_id = claims.get("client_id")
        if not isinstance(client_id, str) or \
                client_id not in issuer.allowed_client_ids:
            raise Denied("client not allowed")
        if client_id in issuer.exempt_client_ids:
            if not _audiences(claims.get("aud")) & issuer.exempt_audiences:
                raise Denied(f"exempt client {client_id}: token not for this service (aud)")
            return Decision("bearer", f"exempt client {client_id}",
                            client_id, exempt=True)
        if not issuer.require_entitlement:
            return Decision("bearer", f"client {client_id}, no entitlement needed",
                            client_id)
        if not self._entitled(issuer, token, claims):
            raise Forbidden(f"no matching entitlement (client {client_id})")
        return Decision("bearer", f"entitled, client {client_id}", client_id)

    def _warn_once(self, key: str, seconds: int = 3600) -> bool:
        """True the first time per key and period, for rate-limited warnings."""
        if self._warned.get(key):
            return False
        self._warned.put(key, True, self._clock() + seconds)
        return True

    def _resolve_endpoints(self, issuer: IssuerConfig,
                           state: _IssuerState) -> None:
        """Fills in jwks_uri and userinfo_endpoint; call with state.lock.

        From the configuration, else from the configured discovery document,
        which is read once.
        """
        if state.jwks_uri:
            return
        jwks_uri, userinfo = issuer.jwks_uri, issuer.userinfo_endpoint
        if not jwks_uri or (not userinfo and issuer.require_entitlement):
            try:
                doc = self._fetch(issuer.discovery_url,
                                  self.config.http_timeout_seconds)
            except FetchError as e:
                raise Unavailable(f"discovery failed: {e}") from None
            if not isinstance(doc, dict) or doc.get("issuer") != issuer.issuer:
                raise Denied("discovery document is for another issuer")
            jwks_uri = jwks_uri or doc.get("jwks_uri")
            userinfo = userinfo or doc.get("userinfo_endpoint")
        allow_http = self.config.allow_http
        try:
            state.jwks_uri = _https_only(jwks_uri, "jwks_uri", allow_http)
            if userinfo:
                state.userinfo_endpoint = _https_only(
                    userinfo, "userinfo_endpoint", allow_http)
        except ValueError as e:
            raise Denied(f"discovery: {e}") from None

    def _signing_key(self, issuer: IssuerConfig, kid: Any, alg: str) -> Any:
        """The issuer's public key for this kid, from its (cached) JWKS."""
        state = self._state[issuer.issuer]
        now = self._clock()
        key = self._cached_key(state, kid, now)
        if key is None:
            key = self._refreshed_key(issuer, state, kid, now)
        if key is None and kid is None and len(state.keys) == 1:
            key = next(iter(state.keys.values()))
        if key is None:
            raise Denied("unknown signing key")
        try:
            self._algorithms[alg].check_crypto_key_type(key)
        except jwt.PyJWTError:
            raise Denied("key type does not match the algorithm") from None
        return key

    def _cached_key(self, state: _IssuerState, kid: Any, now: float) -> Any:
        """A known key without waiting: fresh, or old while another request
        refreshes the keys. None if this request should refresh them."""
        key = state.keys.get(kid)
        if key is None or state.fetched_at is None:
            return None
        age = now - state.fetched_at
        if age < self.config.jwks_cache_seconds:
            return key
        if age < self.config.jwks_max_stale_seconds and state.refreshing:
            return key
        return None

    def _refreshed_key(self, issuer: IssuerConfig, state: _IssuerState,
                       kid: Any, now: float) -> Any:
        """Refetches the JWKS if due and allowed, or waits for the request
        that is fetching it. No lock is held during a fetch."""
        with state.lock:
            waiting_for = state.refreshing
            stale = (state.fetched_at is None or
                     now - state.fetched_at >= self.config.jwks_cache_seconds)
            # Refetch when the keys are old, or for an unknown kid (a rotated
            # key), but at most once per jwks_min_refresh_seconds, failed
            # attempts included: random kids or an unreachable issuer can't
            # make every request wait for a fetch.
            may_try = (state.attempted_at is None or
                       now - state.attempted_at >=
                       self.config.jwks_min_refresh_seconds)
            fetching = (waiting_for is None and may_try and
                        (state.keys.get(kid) is None or stale))
            if fetching:
                state.attempted_at = now
                state.refreshing = done = threading.Event()
        if waiting_for is not None:
            # Another request fetches: its result, or what we have.
            waiting_for.wait(2 * self.config.http_timeout_seconds + 1)
        elif fetching:
            self._fetch_jwks(issuer, state, now, done)
        elif state.fetched_at is None:
            # The first fetch failed and the backoff holds.
            raise Unavailable("JWKS fetch failed recently; retrying after "
                              f"{self.config.jwks_min_refresh_seconds}s")
        if state.fetched_at is None and not state.keys:
            raise Unavailable("JWKS fetch failed")
        # Keys the issuer couldn't confirm for too long are dropped: one it
        # revoked must not stay valid through a long outage.
        if (state.fetched_at is not None and
                now - state.fetched_at >= self.config.jwks_max_stale_seconds):
            state.keys = {}
            raise Unavailable("JWKS too old and the issuer can't be reached")
        return state.keys.get(kid)

    def _fetch_jwks(self, issuer: IssuerConfig, state: _IssuerState,
                    now: float, done: threading.Event) -> None:
        """Fetches the JWKS (this request only) and publishes the result."""
        try:
            self._resolve_endpoints(issuer, state)
            keys = self._parse_jwks(self._fetch(
                state.jwks_uri, self.config.http_timeout_seconds))
            if not keys:
                raise FetchError("no usable signing keys")
        except (Denied, FetchError) as e:
            log.warning("JWKS refresh of %s failed: %s", issuer.issuer, e)
            if not state.keys:
                raise Unavailable(f"JWKS fetch failed: {e}") from None
        else:
            with state.lock:
                state.keys = keys
                state.fetched_at = now
        finally:
            with state.lock:
                state.refreshing = None
            done.set()

    def _parse_jwks(self, jwks: Any) -> dict[str | None, Any]:
        keys: dict[str | None, Any] = {}
        if not isinstance(jwks, dict) or not isinstance(jwks.get("keys"), list):
            return keys
        for jwk in jwks["keys"]:
            if not isinstance(jwk, dict) or jwk.get("use", "sig") != "sig":
                continue
            try:
                key = jwt.PyJWK(jwk).key
            except (jwt.PyJWTError, ValueError, TypeError, KeyError):
                continue
            keys[jwk.get("kid")] = key
        return keys

    def _entitled(self, issuer: IssuerConfig, token: str, claims: dict) -> bool:
        cache_key = hashlib.sha256(
            (issuer.issuer + "\0" + token).encode()).hexdigest()
        cached = self._userinfo.get(cache_key)
        if cached is REFUSED:
            raise Denied("userinfo failed: refused this token recently")
        if cached is not None:
            return cached
        state = self._state[issuer.issuer]
        # Resolved with the JWKS already; no lock needed to read it.
        self._resolve_endpoints(issuer, state)
        endpoint = state.userinfo_endpoint
        if not endpoint:
            raise Denied("issuer has no userinfo endpoint")
        try:
            info = self._fetch(endpoint, self.config.http_timeout_seconds,
                               token=token)
        except FetchError as e:
            if e.unavailable:
                raise Unavailable(f"userinfo failed: {e}") from None
            # A 4xx: the issuer refuses this token (e.g. revoked). Remembered
            # for a minute, so a client retrying it doesn't reach the issuer.
            self._userinfo.put(cache_key, REFUSED, min(
                self._clock() + REFUSAL_CACHE_SECONDS, float(claims["exp"])))
            raise Denied(f"userinfo failed: {e}") from None
        if not isinstance(info, dict) or info.get("sub") != claims["sub"]:
            raise Denied("userinfo is for another subject")
        if ("eduperson_entitlement" not in info
                and self._warn_once(f"no-entitlement:{claims.get('client_id')}")):
            # Usually the client didn't ask for the scope that releases it
            # (Krini's default scopes don't): every user is refused. Logged
            # once an hour per client, not for every token.
            log.warning("userinfo has no eduperson_entitlement for a token of "
                        "client %s: does the client request the scope that "
                        "releases it?", _printable(claims.get("client_id"), 128))
        values = info.get("eduperson_entitlement") or []
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, list):
            values = []
        result = issuer.entitled([v for v in values if isinstance(v, str)])
        expires = min(self._clock() + self.config.userinfo_cache_seconds,
                      float(claims["exp"]))
        self._userinfo.put(cache_key, result, expires)
        return result


def _audiences(claim: Any) -> set[str]:
    """The aud claim as a set: a string or a list of strings (RFC 7519)."""
    if isinstance(claim, str):
        return {claim}
    if isinstance(claim, list):
        return {a for a in claim if isinstance(a, str)}
    return set()


def _is_basic_credentials(credentials: str) -> bool:
    try:
        decoded = base64.b64decode(credentials, validate=True)
    except (binascii.Error, ValueError):
        return False
    return b":" in decoded


class Handler(BaseHTTPRequestHandler):
    validator: Validator  # set by make_server
    slots: threading.BoundedSemaphore  # set by make_server
    server_version = "tes-validator"
    sys_version = ""
    # Seconds a client may take to send its request: a slow client must not
    # hold a thread for long.
    timeout = 10

    def do_GET(self) -> None:  # noqa: N802 (http.server's naming)
        if self.path == "/healthz":
            self._send(200)
            return
        if self.path != "/auth":
            self._send(404)
            return
        # At most MAX_CONCURRENT_CHECKS at once; more wait briefly, then get
        # 503 (the gateway passes it on), so a flood can't start
        # unbounded threads and calls to the issuer.
        if not self.slots.acquire(timeout=5):
            log.warning("too many concurrent checks")
            self._send(503)
            return
        try:
            self._check()
        finally:
            self.slots.release()

    def _check(self) -> None:
        authorization = self.headers.get("Authorization")
        try:
            decision = self.validator.check(authorization)
        except Unavailable as e:
            # Not the client's fault: 503, which nginx turns into its own
            # answer (see gateway.conf).
            log.warning("unavailable: %s", e)
            self._send(503)
            return
        except Forbidden as e:
            log.info("forbidden: %s", e)
            self._send(403)
            return
        except Denied as e:
            log.info("denied: %s", e)
            self._send(401, {"WWW-Authenticate": _challenge(authorization)})
            return
        except Exception as e:  # a bug must not let anyone in
            # Only the type: a message might quote the token.
            log.error("validator error: %s", type(e).__name__)
            self._send(401, {"WWW-Authenticate": _challenge(authorization)})
            return
        log.info("allowed: %s", decision.reason)
        headers = {"X-Tes-Auth-Kind": decision.kind}
        if decision.kind == "bearer":
            headers["X-Tes-Auth-Client"] = decision.client_id
            headers["X-Tes-Auth-Exempt"] = "1" if decision.exempt else "0"
        self._send(200, headers)

    def do_POST(self) -> None:  # noqa: N802 (http.server's naming)
        """Audit log of a task submitted by an exempt client (nginx mirror)."""
        if self.path != "/audit":
            self._send(404)
            return
        # Set by nginx from this validator's own answer, never by the client;
        # nginx sends only exempt clients' submissions.
        if self.headers.get("X-Tes-Auth-Exempt") != "1":
            self._send(204)
            return
        client = self.headers.get("X-Tes-Auth-Client", "")
        request_id = _printable(self.headers.get("X-Request-Id", "-"), 64)
        length = self.headers.get("Content-Length", "")
        if not length.isdigit() or int(length) > MAX_AUDIT_BODY_BYTES:
            # Still a line: a padded task must not escape the audit.
            log.info("audit: [%s] task from exempt client %s; body too large "
                     "or without length, tags not read", request_id,
                     _printable(client, 128))
            self._send(413)
            return
        if not self.slots.acquire(timeout=5):
            log.warning("audit: [%s] too busy, task from exempt client %s "
                        "not read", request_id, _printable(client, 128))
            self._send(503)
            return
        try:
            body = self.rfile.read(int(length))
            log.info("audit: [%s] %s", request_id, audit_line(
                client, body, self.validator.config.audit_tag_keys))
        finally:
            self.slots.release()
        self._send(204)

    def _send(self, status: int, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        # Request lines carry no token (it is in a header); keep them quiet.
        log.debug(format, *args)


_UNPRINTABLE = re.compile(r"[^\x20-\x7e]")


def _printable(value: Any, limit: int = 200) -> str:
    """A log-safe rendering: printable ASCII only, bounded, no newlines."""
    return _UNPRINTABLE.sub("?", str(value))[:limit]


def audit_line(client_id: str, body: bytes, keys: tuple[str, ...]) -> str:
    """Who a task of an exempt client is for: the configured tags only."""
    try:
        task = json.loads(body)
    except ValueError:
        task = None
    tags = task.get("tags") if isinstance(task, dict) else None
    if not isinstance(tags, dict):
        tags = {}
    who = ", ".join(f"{_printable(k, 64)}={_printable(tags[k])}"
                    for k in keys if k in tags)
    if not keys:
        # Without configured keys, only the names of the tags, no values.
        who = "no audit_tag_keys set; tags present: " + (
            ", ".join(sorted(_printable(k, 64) for k in tags)[:20]) or "none")
    return (f"task from exempt client {_printable(client_id, 128)} "
            f"for {who or 'nobody (no audit tags)'}")


def _challenge(authorization: str | None) -> str:
    if authorization and authorization.strip().lower().startswith("bearer"):
        return 'Bearer realm="tes", error="invalid_token"'
    return 'Bearer realm="tes"'


MAX_CONCURRENT_CHECKS = 64


def make_server(validator: Validator, host: str, port: int,
                max_concurrent: int = MAX_CONCURRENT_CHECKS) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {
        "validator": validator,
        "slots": threading.BoundedSemaphore(max_concurrent),
    })
    server = _Server((host, port), handler)
    server.daemon_threads = True
    return server


class _Server(ThreadingHTTPServer):
    """socketserver's default backlog of 5 drops connections in a burst."""

    request_queue_size = LISTEN_BACKLOG


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", required=True)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8081)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(message)s")
    with open(args.config, encoding="utf-8") as f:
        config = Config.from_dict(json.load(f))
    log.info("issuers: %s; Basic auth: %s", ", ".join(config.issuers) or "none",
             "on" if config.basic_auth_enabled else "off")
    server = make_server(Validator(config), args.host, args.port)
    server.serve_forever()
    return 0  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
