#!/usr/bin/env bash
# End-to-end check of how TES resource requests meet SLURM's limits (see
# "Resource limits" in the README). Sends tasks to a deployed endpoint and
# checks the state each one ends in. Needs bash, curl and python3; the node
# must be able to pull the executor images from Docker Hub.
#
#   TES_URL=https://<fqdn>/tes TES_PASSWORD=... tests/e2e/resource-limits.sh [case...]
#
# Environment:
#   TES_URL       the TES base URL, without /v1 (on the workspace itself:
#                 http://127.0.0.1:8080)
#   TES_USER      Basic auth user (default: tes)
#   TES_PASSWORD  Basic auth password (the welcome page shows it). Required.
#   CURL_OPTS     extra curl options, for example "--cacert /path/ca.crt"
#   TIMEOUT       seconds to wait for each task to finish (default: 240)
#
# Cases (default: all, in this order):
#   fits          cpu_cores 1, ram_gb 0.5: must reach COMPLETE
#   cpu-too-big   cpu_cores 4096: must fail (EXECUTOR_ERROR/SYSTEM_ERROR),
#                 not stay QUEUED/INITIALIZING/RUNNING (issue #21)
#   ram-too-big   ram_gb 4096: must fail the same way
#
# Exit status: 0 when every case passes, 1 otherwise. A task still running
# when TIMEOUT expires is cancelled and its case fails.
set -euo pipefail

: "${TES_URL:?set TES_URL, for example https://<fqdn>/tes}"
: "${TES_PASSWORD:?set TES_PASSWORD (Basic auth password of user tes)}"
TES_USER=${TES_USER:-tes}
TIMEOUT=${TIMEOUT:-240}
read -r -a curl_opts <<<"${CURL_OPTS:-}"

ALL_CASES=(fits cpu-too-big ram-too-big)
FAILED_STATES=" EXECUTOR_ERROR SYSTEM_ERROR "
failures=0

# Prints the response body; fails on an HTTP error.
tes() {
    curl -fsS "${curl_opts[@]}" -u "$TES_USER:$TES_PASSWORD" \
        -H 'Content-Type: application/json' "$@"
}

# json <python expression over j> < document
json() { python3 -c "import json,sys; j=json.load(sys.stdin); print($1)"; }

# task <cpu_cores|null> <ram_gb|null> <image> <shell command>: a task document
task() {
    python3 - "$@" <<'EOF'
import json, sys
cpu, ram, image, cmd = sys.argv[1:5]
res = {}
if cpu != "null":
    res["cpu_cores"] = int(cpu)
if ram != "null":
    res["ram_gb"] = float(ram)
print(json.dumps({
    "name": "resource-limits e2e",
    "resources": res,
    "executors": [{"image": image, "command": ["sh", "-c", cmd]}],
}))
EOF
}

# run <case> <task json>: submits it and waits; sets STATE, ID, DETAIL.
run() {
    local t0 state
    ID=$(tes -X POST -d "$2" "$TES_URL/v1/tasks" | json 'j["id"]')
    t0=$(date +%s)
    while :; do
        state=$(tes "$TES_URL/v1/tasks/$ID?view=MINIMAL" | json 'j["state"]')
        case "$state" in
            COMPLETE|EXECUTOR_ERROR|SYSTEM_ERROR|CANCELED|PREEMPTED) break ;;
        esac
        if (( $(date +%s) - t0 >= TIMEOUT )); then
            tes -X POST -d '{}' "$TES_URL/v1/tasks/$ID:cancel" >/dev/null || true
            state="$state(timeout ${TIMEOUT}s, cancelled)"
            break
        fi
        sleep 5
    done
    STATE=$state
    # What the task reports about why it ended: exit code, stdout, stderr and
    # system logs (tesp-api fills in little of this, mirelaminkova/tesp-api#66).
    DETAIL=$(tes "$TES_URL/v1/tasks/$ID?view=FULL" | python3 -c '
import json, sys
j = json.load(sys.stdin)
parts = []
for log in j.get("logs") or []:
    parts += log.get("system_logs") or []
    for e in log.get("logs") or []:
        parts.append("exit_code=%s" % e.get("exit_code"))
        parts += [e.get("stdout") or "", e.get("stderr") or ""]
text = " | ".join(p.strip().replace("\n", " ") for p in parts if p and p.strip())
print(text[-300:] if text else "no reason reported")
')
    echo "   $1: task $ID -> $STATE in $(( $(date +%s) - t0 ))s; $DETAIL"
}

pass() { echo "PASS $1"; }
fail() { echo "FAIL $1"; failures=$((failures + 1)); }

expect_complete() {  # <case>
    if [ "$STATE" = COMPLETE ]; then pass "$1"; else fail "$1 (expected COMPLETE)"; fi
}

expect_failed() {  # <case>
    if [[ "$FAILED_STATES" == *" $STATE "* ]]; then pass "$1"
    else fail "$1 (expected EXECUTOR_ERROR or SYSTEM_ERROR)"; fi
}

case_fits() {
    run fits "$(task 1 0.5 alpine:3.20 'echo fits')"
    expect_complete fits
}

case_cpu-too-big() {
    run cpu-too-big "$(task 4096 null alpine:3.20 'echo should not run')"
    expect_failed cpu-too-big
}

case_ram-too-big() {
    run ram-too-big "$(task null 4096 alpine:3.20 'echo should not run')"
    expect_failed ram-too-big
}

cases=("$@")
[ ${#cases[@]} -gt 0 ] || cases=("${ALL_CASES[@]}")
for c in "${cases[@]}"; do
    if [[ " ${ALL_CASES[*]} " != *" $c "* ]]; then
        echo "unknown case: $c (known: ${ALL_CASES[*]})" >&2
        exit 2
    fi
    "case_$c"
done

echo "Summary: $(( ${#cases[@]} - failures )) passed, $failures failed"
[ "$failures" -eq 0 ]
