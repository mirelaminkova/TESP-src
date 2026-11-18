#!/usr/bin/env bash
# Runs every unit test in tests/: the playbooks tests/*.yml. Needs only
# ansible-core. Python tests (pytest) come with the first Python code.
set -euo pipefail
cd "$(dirname "$0")/.."
for t in tests/*.yml; do
    echo "== $t"
    ansible-playbook -i localhost, -c local "$t"
done
