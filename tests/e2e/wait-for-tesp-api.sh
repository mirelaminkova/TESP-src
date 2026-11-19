#!/usr/bin/env bash
# Waits up to 5 minutes until tesp-api answers through the gateway.
#   wait-for-tesp-api.sh <base URL> <user> <password>
set -euo pipefail
base=$1 user=$2 password=$3
for _ in $(seq 1 60); do
    if curl -fsS -o /dev/null -u "$user:$password" "$base/v1/tasks"; then
        echo "tesp-api answers through the gateway"
        exit 0
    fi
    sleep 5
done
echo "no answer from tesp-api through the gateway within 5 minutes" >&2
exit 1
