#!/usr/bin/env bash
# End-to-end checks of the TES gateway (issue #25) against a running stack.
#   check-gateway.sh <base URL> <user> <password> <expected service-info.json>
set -euo pipefail
base=$1 user=$2 password=$3 expected=$4
fail=0
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

# expect <name> <status> <curl args...>: the request answers with <status>.
expect() {
    local name=$1 want=$2
    shift 2
    local got
    # A transport error shows as 000 instead of ending the script.
    got=$(curl -sS -o "$tmp/body" -w '%{http_code}' "$@" || true)
    if [ "$got" = "$want" ]; then
        echo "ok   $name ($got)"
    else
        echo "FAIL $name: expected $want, got $got" >&2
        head -c 500 "$tmp/body" >&2 || true
        echo >&2
        fail=1
    fi
}

expect "service-info is public" 200 "$base/v1/service-info"
python3 - "$tmp/body" "$expected" <<'PY' || fail=1
import json, sys
got, want = (json.load(open(p)) for p in sys.argv[1:3])
if got != want:
    sys.exit(f"FAIL service-info differs from the rendered file:\n{got}\n{want}")
assert got["type"] == {"group": "org.ga4gh", "artifact": "tes", "version": "1.0.0"}
print("ok   service-info is the rendered one")
PY
ctype=$(curl -sS -o /dev/null -w '%{content_type}' "$base/v1/service-info")
case $ctype in
    application/json*) echo "ok   service-info content type ($ctype)" ;;
    *) echo "FAIL service-info content type: $ctype" >&2; fail=1 ;;
esac
expect "service-info is read-only" 405 -X POST -d '{}' "$base/v1/service-info"
curl -sS -o /dev/null -D - -X POST "$base/v1/service-info" | grep -qi '^Allow: GET, HEAD' ||
    { echo "FAIL 405 without Allow: GET, HEAD" >&2; fail=1; }
expect "service-info with a trailing slash is ours too" 200 "$base/v1/service-info/"
cmp -s "$tmp/body" "$expected" || { echo "FAIL trailing-slash service-info is not the rendered file" >&2; fail=1; }

# tesp-api answers a request without Basic credentials with a 500; the
# gateway answers 401 itself.
expect "tasks without credentials" 401 "$base/v1/tasks"
grep -q '^Basic realm=' <(curl -sS -o /dev/null -D - "$base/v1/tasks" | sed -n 's/^[Ww][Ww][Ww]-[Aa]uthenticate: //p') ||
    { echo "FAIL no WWW-Authenticate: Basic challenge" >&2; fail=1; }
expect "tasks with a Bearer token" 401 -H 'Authorization: Bearer abc.def.ghi' "$base/v1/tasks"
expect "tasks with empty Basic credentials" 401 -H 'Authorization: Basic ' "$base/v1/tasks"
expect "tasks with a wrong password" 401 -u "$user:wrong-$password" "$base/v1/tasks"
expect "tasks with Basic credentials" 200 -u "$user:$password" "$base/v1/tasks"
expect "task submission reaches tesp-api" 200 -u "$user:$password" \
    -H 'Content-Type: application/json' \
    -d '{"name": "e2e", "executors": [{"image": "alpine:3.20", "command": ["true"]}]}' \
    "$base/v1/tasks"
grep -q '"id"' "$tmp/body" || { echo "FAIL submission returned no id" >&2; fail=1; }
expect "Swagger UI is still served" 200 "$base/swagger-ui.html"

# tesp-api must be reachable only through the gateway.
if ! ports=$(docker port tesp-api); then
    echo "FAIL no tesp-api container" >&2
    fail=1
elif [ -n "$ports" ]; then
    echo "FAIL tesp-api publishes a host port: $ports" >&2
    fail=1
else
    echo "ok   tesp-api has no host port"
fi

exit "$fail"
