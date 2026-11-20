#!/usr/bin/env bash
# Runs every unit test in tests/: the Python tests (pytest, 100% coverage of
# the token validator), then the playbooks tests/*.yml. Needs ansible-core,
# and the packages in tests/requirements.txt and
# files/tes-validator/requirements.txt.
set -euo pipefail
cd "$(dirname "$0")/.."
echo "== tests/test_*.py"
# No __pycache__ or .pytest_cache next to the validator: its directory is the
# image build context.
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider \
    --cov=tes_validator --cov=check_stack_subnet --cov-branch --cov-report=term-missing \
    --cov-fail-under=100 tests/
for t in tests/*.yml; do
    echo "== $t"
    ansible-playbook -i localhost, -c local "$t"
done
