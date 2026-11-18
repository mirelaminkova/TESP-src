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

expect "user without the entitlement" 401 -H "$(bearer "$(mint --sub plain-user)")" "$base/v1/tasks"
grep -qi '^WWW-Authenticate: Bearer realm="tes", error="invalid_token"' "$tmp/headers" ||
    { echo "FAIL no invalid_token challenge" >&2; fail=1; }
expect "client not allowed" 401 -H "$(bearer "$(mint --client other-client)")" "$base/v1/tasks"
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
case $got in
    400|401) echo "ok   two Authorization headers ($got)" ;;
    *) echo "FAIL two Authorization headers: got $got" >&2; fail=1 ;;
esac

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
