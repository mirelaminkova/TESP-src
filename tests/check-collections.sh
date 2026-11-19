#!/usr/bin/env bash
# Issue #31: installs the collections in requirements.yml into a scratch
# directory and checks that each one's requires_ansible (meta/runtime.yml)
# includes the ansible-core that runs this script, so it is meaningful only
# under SRC's ansible-core 2.16, as in CI's src-ansible-core job. Needs
# network access (Galaxy, GitHub), so it isn't part of tests/run.sh.
set -euo pipefail
cd "$(dirname "$0")/.."
dir=$(mktemp -d)
trap 'rm -rf "$dir"' EXIT
if ! ansible-galaxy collection install -r requirements.yml -p "$dir" \
     >"$dir/install.log" 2>&1; then
  cat "$dir/install.log" >&2
  exit 1
fi
# The Python that runs ansible-core, which brings `packaging` and PyYAML:
# the last "(...)" on ansible's "python version = ..." line.
python=$(ansible --version | sed -n 's/^ *python version = .*(\(\/[^()]*\))$/\1/p')
[ -x "$python" ] || { echo "can't find ansible-core's Python" >&2; exit 1; }
"$python" - "$dir/ansible_collections" <<'PY'
import pathlib, sys
import yaml
from ansible.release import __version__ as core
from packaging.specifiers import SpecifierSet

print(f"ansible-core {core}")
bad = 0
collections = sorted(p for p in pathlib.Path(sys.argv[1]).glob("*/*") if p.is_dir())
if not collections:
    sys.exit("no collection was installed")
for path in collections:
    name = ".".join(path.parts[-2:])
    runtime = path / "meta" / "runtime.yml"
    meta = yaml.safe_load(runtime.read_text()) if runtime.exists() else None
    spec = (meta or {}).get("requires_ansible")
    if spec is None:
        print(f"ok   {name}: no requires_ansible")
    elif SpecifierSet(spec).contains(core, prereleases=True):
        print(f"ok   {name}: requires_ansible {spec} includes {core}")
    else:
        print(f"FAIL {name}: requires_ansible {spec} excludes {core}")
        bad += 1
sys.exit(1 if bad else 0)
PY
