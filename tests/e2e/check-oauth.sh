#!/usr/bin/env bash
# End-to-end checks of LS Login token access at the gateway (issue #26),
# against a stack rendered with tests/e2e/oauth-vars.yml and the mock
# identity provider of tests/e2e/compose.oauth.yml.
#   check-oauth.sh <base URL> <user> <password> <keys dir>
set -euo pipefail
base=$1 user=$2 password=$3 keys=$4
here=$(dirname "$0")
fail=0
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
# Every minted token, for the log check at the end. A file, because mint
# runs in command substitutions (subshells).
: > "$tmp/tokens"

mint() {
    local token
    token=$(python3 "$here/idp/mint_token.py" "$keys" "$@")
    printf '%s\n' "$token" >> "$tmp/tokens"
    printf '%s' "$token"
}

# expect <name> <status> <curl args...>
expect() {
    local name=$1 want=$2
    shift 2
    local got
    got=$(curl -sS -o "$tmp/body" -D "$tmp/headers" -w '%{http_code}' "$@" || true)
    if [ "$got" = "$want" ]; then
        echo "ok   $name ($got)"
    else
        echo "FAIL $name: expected $want, got $got" >&2
        head -c 500 "$tmp/body" >&2 || true
        echo >&2
        fail=1
    fi
}

bearer() { printf 'Authorization: Bearer %s' "$1"; }

expect "service-info stays public" 200 "$base/v1/service-info"
expect "no credentials" 401 "$base/v1/tasks"
grep -qi '^WWW-Authenticate: Bearer realm="tes"' "$tmp/headers" ||
    { echo "FAIL no Bearer challenge" >&2; fail=1; }

expect "entitled user, allowed client" 200 -H "$(bearer "$(mint)")" "$base/v1/tasks"
expect "task submission with a token" 200 -H "$(bearer "$(mint)")" \
    -H 'Content-Type: application/json' \
    -d '{"name": "e2e-token", "executors": [{"image": "alpine:3.20", "command": ["true"]}]}' \
    "$base/v1/tasks"
grep -q '"id"' "$tmp/body" || { echo "FAIL submission returned no id" >&2; fail=1; }
expect "exempt service client, no entitlement" 200 \
    -H "$(bearer "$(mint --client e2e-service --sub service-account)")" "$base/v1/tasks"
expect "exempt service client submits for a user" 200 \
    -H "$(bearer "$(mint --client e2e-service --sub service-account)")" \
    -H 'Content-Type: application/json' \
    -d '{"name": "e2e-on-behalf", "tags": {"user_sub": "e2e-user-42", "note": "not-for-the-log"}, "executors": [{"image": "alpine:3.20", "command": ["true"]}]}' \
    "$base/v1/tasks"
audit="task from exempt client e2e-service for user_sub=e2e-user-42"
for _ in 1 2 3 4 5; do
    docker logs tesp-validator 2>&1 | grep -qF "$audit" && break
    sleep 1
done
if docker logs tesp-validator 2>&1 | grep -qF "$audit"; then
    echo "ok   the audit log names the user"
else
    echo "FAIL no audit line for the exempt client's task" >&2
    fail=1
fi
if docker logs tesp-validator 2>&1 | grep -qF "not-for-the-log"; then
    echo "FAIL the audit log has other tags" >&2
    fail=1
fi
# A user can't make the audit log name someone else: forged gateway headers
# are replaced.
expect "user token with forged gateway headers" 200 -H "$(bearer "$(mint)")" \
    -H 'X-Tes-Auth-Exempt: 1' -H 'X-Tes-Auth-Client: forged-client' \
    -H 'Content-Type: application/json' \
    -d '{"name": "e2e-forged", "tags": {"user_sub": "forged-user"}, "executors": [{"image": "alpine:3.20", "command": ["true"]}]}' \
    "$base/v1/tasks"
sleep 1
if docker logs tesp-validator 2>&1 | grep -qE "forged-(user|client)"; then
    echo "FAIL forged headers reached the audit log" >&2
    fail=1
else
    echo "ok   forged gateway headers don't reach the audit log"
fi
# A user's own token is not audited this way (its sub is in the token).
if docker logs tesp-validator 2>&1 | grep -q "audit: task from exempt client e2e-portal"; then
    echo "FAIL a non-exempt client was audited as exempt" >&2
    fail=1
fi

# A valid token without the group: 403, not 401 (logging in again won't help).
expect "user without the entitlement" 403 -H "Origin: https://portal.e2e.test" \
    -H "$(bearer "$(mint --sub plain-user)")" "$base/v1/tasks"
grep -qiF 'Access-Control-Allow-Origin: https://portal.e2e.test' "$tmp/headers" ||
    { echo "FAIL no CORS header on 403" >&2; fail=1; }
grep -qi '^WWW-Authenticate: Bearer realm="tes", error="insufficient_scope"' "$tmp/headers" ||
    { echo "FAIL no insufficient_scope challenge on 403" >&2; fail=1; }
expect "client not allowed" 401 -H "$(bearer "$(mint --client other-client)")" "$base/v1/tasks"
grep -qi '^WWW-Authenticate: Bearer realm="tes", error="invalid_token"' "$tmp/headers" ||
    { echo "FAIL no invalid_token challenge" >&2; fail=1; }
expect "expired token" 401 -H "$(bearer "$(mint --exp-in -600)")" "$base/v1/tasks"
expect "ID token (typ JWT)" 401 -H "$(bearer "$(mint --typ JWT)")" "$base/v1/tasks"
expect "signed with another key" 401 -H "$(bearer "$(mint --other-key)")" "$base/v1/tasks"

expect "Basic still works from an allowed source" 200 -u "$user:$password" "$base/v1/tasks"
# The runner's connection comes from a Docker range the gateway trusts for
# X-Real-IP (in production only the host nginx sets it).
expect "Basic from a source not allowed" 401 -u "$user:$password" \
    -H 'X-Real-IP: 203.0.113.9' "$base/v1/tasks"
expect "exempt client's token for another service" 401 \
    -H "$(bearer "$(mint --client e2e-service --sub service-account --aud other-backend)")" \
    "$base/v1/tasks"
expect "wrong Basic password" 401 -u "$user:wrong-$password" "$base/v1/tasks"
# Two Authorization headers: a token of a user without the entitlement, and
# Basic credentials. nginx rejects the request (400) or the validator sees
# one header; either way it must not get through.
got=$(curl -sS -o /dev/null -w '%{http_code}' -H "$(bearer "$(mint --sub plain-user)")" \
    -H "Authorization: Basic $(printf '%s' "$user:wrong-$password" | base64 -w0)" \
    "$base/v1/tasks" || true)
# 403 if nginx passes the first header (a valid token without the group).
case $got in
    400|401|403) echo "ok   two Authorization headers ($got)" ;;
    *) echo "FAIL two Authorization headers: got $got" >&2; fail=1 ;;
esac

# CORS for a browser portal (Krini): only the configured origin.
portal=https://portal.e2e.test
before=$(docker logs tesp-validator 2>&1 | wc -l)
expect "CORS preflight of the portal" 204 -X OPTIONS -H "Origin: $portal" \
    -H 'Access-Control-Request-Method: POST' \
    -H 'Access-Control-Request-Headers: authorization,content-type' "$base/v1/tasks"
for h in "Access-Control-Allow-Origin: $portal" 'Access-Control-Allow-Methods: GET, POST, OPTIONS' \
         'Access-Control-Allow-Headers: authorization, content-type' 'Access-Control-Max-Age: 600' \
         'Vary: Origin'; do
    grep -qiF "$h" "$tmp/headers" || { echo "FAIL preflight lacks $h" >&2; fail=1; }
done
grep -qi '^Access-Control-Allow-Credentials' "$tmp/headers" && { echo "FAIL credentials allowed" >&2; fail=1; }
if [ "$(docker logs tesp-validator 2>&1 | wc -l)" != "$before" ]; then
    echo "FAIL the preflight reached the validator" >&2
    fail=1
fi
expect "CORS preflight of another origin" 401 -X OPTIONS -H 'Origin: https://evil.e2e.test' \
    -H 'Access-Control-Request-Method: POST' "$base/v1/tasks"
grep -qi '^Access-Control-Allow-Origin' "$tmp/headers" && { echo "FAIL another origin got CORS headers" >&2; fail=1; }
expect "token call from the portal" 200 -H "Origin: $portal" -H "$(bearer "$(mint)")" "$base/v1/tasks"
grep -qiF "Access-Control-Allow-Origin: $portal" "$tmp/headers" || { echo "FAIL no CORS header on 200" >&2; fail=1; }
grep -qiF 'Access-Control-Expose-Headers: WWW-Authenticate, Retry-After' "$tmp/headers" ||
    { echo "FAIL no Access-Control-Expose-Headers" >&2; fail=1; }
expect "401 for the portal" 401 -H "Origin: $portal" "$base/v1/tasks"
grep -qiF "Access-Control-Allow-Origin: $portal" "$tmp/headers" || { echo "FAIL no CORS header on 401" >&2; fail=1; }

# The issuer down: a token that needs userinfo gets 503, not 401.
docker stop -t 2 "$(docker ps -qf name=idp)" >/dev/null
expect "issuer down: 503, not 401" 503 -H "$(bearer "$(mint --sub entitled-new-user)")" "$base/v1/tasks"
grep -qi '^Retry-After: 30' "$tmp/headers" || { echo "FAIL no Retry-After on 503" >&2; fail=1; }

# Rate limit per client address (20/s; up to 40 more are queued, the rest
# get 429). The runner is a Basic source here, so exempt; X-Real-IP (trusted
# from the runner's Docker range) makes it another, limited address.
burst() {  # burst <name> <extra curl args...>: 100 parallel requests
    local name=$1
    shift
    rm -rf "$tmp/burst"
    mkdir -p "$tmp/burst"
    for i in $(seq 1 100); do
        curl -s -o /dev/null -D "$tmp/burst/$i" -w '%{http_code}\n' \
            -H "Origin: $portal" "$@" "$base/v1/tasks" &
    done | grep -c '^429$' || true
    wait
}
n429=$(burst limited -H 'X-Real-IP: 203.0.113.9')
if [ "$n429" -gt 0 ]; then
    echo "ok   rate limit: $n429 of 100 got 429"
else
    echo "FAIL rate limit: no 429 in a burst of 100" >&2
    fail=1
fi
# A 429 carries the CORS headers and Retry-After, so a portal can back off.
h429=$(grep -l '^HTTP/1.1 429' "$tmp"/burst/* | head -1)
if [ -n "$h429" ] && grep -qiF "Access-Control-Allow-Origin: $portal" "$h429" &&
        grep -qi '^Retry-After: 1' "$h429"; then
    echo "ok   429 carries Allow-Origin and Retry-After"
else
    echo "FAIL 429 without Allow-Origin or Retry-After" >&2
    fail=1
fi
sleep 3
# An exempt source (the Basic source, the co-located Nextflow) is never
# limited: a 429 would abort its Nextflow run.
n429=$(burst exempt)
if [ "$n429" = 0 ]; then
    echo "ok   exempt source: no 429 in a burst of 100"
else
    echo "FAIL exempt source got $n429 x 429" >&2
    fail=1
fi
# A client polling one request after another at the rate is queued, never
# refused (no nodelay).
n429=0
for _ in $(seq 1 60); do
    c=$(curl -s -o /dev/null -w '%{http_code}' -H 'X-Real-IP: 203.0.113.10' "$base/v1/tasks" || true)
    [ "$c" = 429 ] && n429=$((n429 + 1))
    sleep 0.04
done
if [ "$n429" = 0 ]; then
    echo "ok   sequential polling at the limit: no 429"
else
    echo "FAIL sequential polling got $n429 x 429" >&2
    fail=1
fi
sleep 3

# No token may end up in the logs.
docker logs tesp-validator > "$tmp/validator.log" 2>&1
docker logs tesp-gateway > "$tmp/gateway.log" 2>&1
minted=$(wc -l < "$tmp/tokens")
leaked=0
while read -r token; do
    # The signature part is unique to each token.
    if grep -qF "${token##*.}" "$tmp/validator.log" "$tmp/gateway.log"; then
        leaked=1
    fi
done < "$tmp/tokens"
# This script mints more than 10; fewer means the recording broke and the
# check below would prove nothing.
if [ "$minted" -lt 10 ]; then
    echo "FAIL only $minted tokens recorded for the log check" >&2
    fail=1
elif [ "$leaked" = 1 ]; then
    echo "FAIL a token is in the logs" >&2
    fail=1
else
    echo "ok   none of $minted tokens is in the validator and gateway logs"
fi

exit "$fail"
