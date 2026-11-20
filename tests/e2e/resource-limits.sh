#!/usr/bin/env bash
# End-to-end check of how TES resource requests meet SLURM's limits (see
# "Resource limits" in the README). Sends tasks to a deployed endpoint and
# checks the state each one ends in. Needs bash 4.4+, curl and python3; the
# node must be able to pull the executor images from Docker Hub. CI can't run
# it (no SLURM): run it on a test workspace after deploying.
#
#   read -rsp 'TES password: ' TES_PASSWORD && echo && export TES_PASSWORD
#   TES_URL=https://<fqdn>/tes tests/e2e/resource-limits.sh [case...]
#
# Environment:
#   TES_URL       the TES base URL, without /v1 (on the workspace itself:
#                 http://127.0.0.1:8080)
#   TES_USER      Basic auth user (default: tes)
#   TES_PASSWORD  Basic auth password (the welcome page shows it). Required.
#                 Passed to curl in a config file descriptor, not on its
#                 command line.
#   CURL_OPTS     extra curl options, split on spaces (no quoting), for
#                 example "--cacert /etc/ssl/certs/ca.crt"
#   TIMEOUT       seconds to wait for each task to finish (default: 240)
#   REJECT_SECONDS  seconds within which an oversize task must fail to count
#                 as rejected at submission (default: 60)
#
# Cases (default: all, in this order):
#   fits          cpu_cores 1, ram_gb 0.5: must reach COMPLETE. Always runs
#                 first, and counts in the summary: it is the control for the
#                 cases that expect failure.
#   cpu-too-big   cpu_cores 4096: must fail (EXECUTOR_ERROR/SYSTEM_ERROR)
#                 within REJECT_SECONDS, not stay QUEUED/RUNNING (issue #21)
#   ram-too-big   ram_gb 4096: the same. SLURM rejected oversize memory at
#                 submission even before #21, so this case guards the memory
#                 path, not the #21 fix.
#   ram-within    ram_gb 1, holds 256 MiB for 30 s: must reach COMPLETE
#   ram-overuse   ram_gb 0.5, holds 1.5 GiB for 90 s: must be killed by
#                 SLURM (EXECUTOR_ERROR/SYSTEM_ERROR, "exceeded memory
#                 limit" in its logs) before it finishes (issue #13). Runs
#                 ram-within first: the control for the same image.
# Not run by default (name it):
#   cold-image    ram_gb 0.25 with a large image (COLD_IMAGE, default
#                 rocker/tidyverse:4.4.1, about 2 GB) that isn't cached on
#                 the node: must reach COMPLETE, the conversion fitting the
#                 task's memory (issue #42). Inconclusive (fails) if the image
#                 was cached: clear it with `apptainer cache clean` as the
#                 Pulsar user, or pick another COLD_IMAGE. TIMEOUT must cover
#                 the download (default 240 s; use 900).
# tesp-api reports no reason for a rejection yet (mirelaminkova/tesp-api#66);
# on the workspace, `sacct` and slurmctld's log show it.
#
# Exit status: 0 when every case passes, 1 otherwise, 2 for an unknown case.
# A task still running when TIMEOUT expires is cancelled and its case fails.
set -euo pipefail

: "${TES_URL:?set TES_URL, for example https://<fqdn>/tes}"
: "${TES_PASSWORD:?set TES_PASSWORD (Basic auth password of user tes)}"
TES_USER=${TES_USER:-tes}
TIMEOUT=${TIMEOUT:-240}
# A submission SLURM rejects fails within seconds; a later failure has some
# other cause (image pull, Pulsar) and doesn't count as a rejection.
REJECT_SECONDS=${REJECT_SECONDS:-60}
read -r -a curl_opts <<<"${CURL_OPTS:-}"

ALL_CASES=(fits cpu-too-big ram-too-big ram-within ram-overuse)
OPT_CASES=(cold-image)
COLD_IMAGE=${COLD_IMAGE:-rocker/tidyverse:4.4.1}
# hog <MiB> <seconds>: a shell command that holds that much memory, every
# page written (bytes * n), then prints "held <MiB> MiB"
HOG_IMAGE=python:3.13-alpine
hog() {
    printf 'python3 -c %q' \
        "import time; b = b'x' * ($1 << 20); time.sleep($2); print('held $1 MiB')"
}
ram_within_ran='' ram_within_ok=''
FAILED_STATES=" EXECUTOR_ERROR SYSTEM_ERROR "
failures=0
passes=0
control_ok=

# Prints the response body; fails on an HTTP error. The credentials go
# through a config file descriptor so they don't show up in ps.
tes() {
    local u="$TES_USER:$TES_PASSWORD"
    u=${u//\\/\\\\}; u=${u//\"/\\\"}  # curl config quoting: \ and "
    curl -q -fsS "${curl_opts[@]}" -K <(printf 'user = "%s"\n' "$u") \
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

# run <case> <task json>: submits it and waits. Sets ID, STATE (the final
# state, or SUBMIT_ERROR / "<state>(timeout ...)"), SECONDS_TAKEN, and
# DETAIL (all the task's logs; only the end is printed).
# Never exits the script: a failed poll is retried until TIMEOUT.
run() {
    local t0 state=UNKNOWN
    ID='' STATE='' DETAIL='' SECONDS_TAKEN=0
    t0=$(date +%s)
    if ! ID=$(tes -X POST -d "$2" "$TES_URL/v1/tasks" | json 'j["id"]'); then
        STATE=SUBMIT_ERROR
        echo "   $1: submitting the task failed"
        return 0
    fi
    while :; do
        state=$(tes "$TES_URL/v1/tasks/$ID?view=MINIMAL" | json 'j["state"]') \
            || state=POLL_ERROR
        case "$state" in
            COMPLETE|EXECUTOR_ERROR|SYSTEM_ERROR|CANCELED|PREEMPTED) break ;;
        esac
        if (( $(date +%s) - t0 >= TIMEOUT )); then
            tes -X POST -d '{}' "$TES_URL/v1/tasks/$ID:cancel" >/dev/null || true
            echo "   $1: cancelled task $ID after ${TIMEOUT}s; check squeue for a left-over job"
            state="$state(timeout ${TIMEOUT}s)"
            break
        fi
        sleep 5
    done
    STATE=$state
    SECONDS_TAKEN=$(( $(date +%s) - t0 ))
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
print(text or "no reason reported")
') || DETAIL="task logs unavailable"
    local shown=$DETAIL
    (( ${#shown} <= 300 )) || shown=${shown: -300}
    echo "   $1: task $ID -> $STATE in ${SECONDS_TAKEN}s; $shown"
}

pass() { echo "PASS $1"; passes=$((passes + 1)); }
fail() { echo "FAIL $1"; failures=$((failures + 1)); }

expect_complete() {  # <case>
    if [ "$STATE" = COMPLETE ]; then pass "$1"; else fail "$1 (expected COMPLETE)"; fi
}

# The task must fail, and fast enough to be a rejection at submission. Without
# a passing control (fits) a failure proves nothing about SLURM.
expect_rejected() {  # <case>
    if [ -z "$control_ok" ]; then
        fail "$1 (inconclusive: the control task 'fits' didn't complete)"
    elif [[ "$FAILED_STATES" != *" $STATE "* ]]; then
        fail "$1 (expected EXECUTOR_ERROR or SYSTEM_ERROR)"
    elif (( SECONDS_TAKEN > REJECT_SECONDS )); then
        fail "$1 (failed after ${SECONDS_TAKEN}s > REJECT_SECONDS=$REJECT_SECONDS: not a rejection at submission)"
    else
        pass "$1"
    fi
}

case_fits() {
    run fits "$(task 1 0.5 alpine:3.20 'echo fits')"
    expect_complete fits
    if [ "$STATE" = COMPLETE ]; then control_ok=1; fi
}

case_cpu-too-big() {
    run cpu-too-big "$(task 4096 null alpine:3.20 'echo should not run')"
    expect_rejected cpu-too-big
}

case_ram-too-big() {
    run ram-too-big "$(task null 4096 alpine:3.20 'echo should not run')"
    expect_rejected ram-too-big
}

case_ram-within() {
    [ -z "$ram_within_ran" ] || return 0
    ram_within_ran=1
    run ram-within "$(task 1 1 "$HOG_IMAGE" "$(hog 256 30)")"
    expect_complete ram-within
    if [ "$STATE" = COMPLETE ]; then ram_within_ok=1; fi
}

# The task must fail because SLURM killed it for its memory: the hog never
# finished, and slurmstepd's message reached the task's logs.
case_ram-overuse() {
    case_ram-within  # the control; runs once
    run ram-overuse "$(task 1 0.5 "$HOG_IMAGE" "$(hog 1536 90)")"
    if [ -z "$ram_within_ok" ]; then
        fail "ram-overuse (inconclusive: the control task 'ram-within' didn't complete)"
    elif [[ "$FAILED_STATES" != *" $STATE "* ]]; then
        fail "ram-overuse (expected EXECUTOR_ERROR or SYSTEM_ERROR)"
    elif [[ "$DETAIL" == *"held 1536 MiB"* ]]; then
        fail "ram-overuse (the task held its memory to the end: not killed)"
    elif [[ "$DETAIL" != *"exceeded memory limit"* ]]; then
        fail "ram-overuse (failed without SLURM's \"exceeded memory limit\")"
    else
        pass ram-overuse
    fi
}

# Apptainer converts an uncached image inside the task's job; the
# conversion must fit a small ram_gb.
case_cold-image() {
    run cold-image "$(task 1 0.25 "$COLD_IMAGE" 'echo cold image ok')"
    if [[ "$DETAIL" == *"Using cached SIF image"* ]]; then
        fail "cold-image (inconclusive: $COLD_IMAGE was cached)"
    elif [ "$STATE" != COMPLETE ]; then
        fail "cold-image (expected COMPLETE)"
    else
        pass cold-image
    fi
}

cases=("$@")
[ ${#cases[@]} -gt 0 ] || cases=("${ALL_CASES[@]}")
for c in "${cases[@]}"; do
    if [[ " ${ALL_CASES[*]} ${OPT_CASES[*]} " != *" $c "* ]]; then
        echo "unknown case: $c (known: ${ALL_CASES[*]} ${OPT_CASES[*]})" >&2
        exit 2
    fi
done
# The control always runs, first and once.
run_cases=(fits)
for c in "${cases[@]}"; do [ "$c" = fits ] || run_cases+=("$c"); done
for c in "${run_cases[@]}"; do "case_$c"; done

echo "Summary: $passes passed, $failures failed"
[ "$failures" -eq 0 ]
