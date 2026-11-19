"""Unit tests for files/check_stack_subnet.py (issue #44).

The playbook runs it before `docker compose up`: the stack's pinned subnet
must not overlap another Docker network or a route on the workspace. Run with:

    python3 -m pytest tests/
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "files"))

import check_stack_subnet as css  # noqa: E402


def network(name, subnets, net_id="0123456789abcdef", bridge_name=None):
    options = {}
    if bridge_name:
        options["com.docker.network.bridge.name"] = bridge_name
    return {
        "Name": name,
        "Id": net_id,
        "Options": options,
        "IPAM": {"Config": [{"Subnet": s} for s in subnets]},
    }


ROUTES = [
    {"dst": "default", "gateway": "10.77.0.1", "dev": "eth0"},
    {"dst": "10.77.0.0/24", "dev": "eth0"},
    {"dst": "10.77.0.1", "dev": "eth0"},
    {"dst": "172.17.0.0/16", "dev": "docker0"},
]


def test_a_free_subnet_has_no_conflicts():
    networks = [network("bridge", ["172.17.0.0/16"])]
    assert css.conflicts("172.16.97.0/24", networks, ROUTES, "tesp-api_default") == []


def test_another_docker_network_conflicts():
    networks = [network("other_default", ["172.16.0.0/16"], net_id="feedfacecafe0000")]
    found = css.conflicts("172.16.97.0/24", networks, [], "tesp-api_default")
    assert found == ["Docker network other_default (172.16.0.0/16)"]


def test_a_smaller_network_inside_conflicts_too():
    networks = [network("small", ["172.16.97.128/28"])]
    assert css.conflicts("172.16.97.0/24", networks, [], "x_default") == [
        "Docker network small (172.16.97.128/28)"
    ]


def test_the_stacks_own_network_and_its_route_are_ignored():
    # A redeploy: the stack's network exists, with the same or an old subnet.
    own = network("tesp-api_default", ["172.16.97.0/24"], net_id="88247bdb9adc0011")
    routes = ROUTES + [{"dst": "172.16.97.0/24", "dev": "br-88247bdb9adc"}]
    assert css.conflicts("172.16.97.0/24", [own], routes, "tesp-api_default") == []


def test_a_custom_bridge_name_of_the_own_network_is_ignored():
    own = network("tesp-api_default", ["172.16.97.0/24"], bridge_name="tesp0")
    routes = [{"dst": "172.16.97.0/24", "dev": "tesp0"}]
    assert css.conflicts("172.16.97.0/24", [own], routes, "tesp-api_default") == []


def test_a_host_route_conflicts():
    routes = ROUTES + [{"dst": "172.16.0.0/12", "dev": "wg0"}]
    assert css.conflicts("172.16.97.0/24", [], routes, "x_default") == [
        "route 172.16.0.0/12 dev wg0"
    ]


def test_a_single_address_route_conflicts():
    routes = [{"dst": "172.16.97.5", "dev": "tun0"}]
    assert css.conflicts("172.16.97.0/24", [], routes, "x_default") == [
        "route 172.16.97.5 dev tun0"
    ]


def test_the_default_route_and_ipv6_subnets_are_ignored():
    networks = [network("v6", ["fd00::/64"])]
    routes = [{"dst": "default", "dev": "eth0"}]
    assert css.conflicts("172.16.97.0/24", networks, routes, "x_default") == []


def test_networks_without_ipam_config_are_ignored():
    networks = [
        {"Name": "host", "Id": "aa", "IPAM": {"Config": None}},
        {"Name": "none", "Id": "bb", "IPAM": {}},
        {"Name": "odd", "Id": "cc", "IPAM": {"Config": [{"Gateway": "1.2.3.4"}]}},
    ]
    assert css.conflicts("172.16.97.0/24", networks, [], "x_default") == []


def test_main_reports_conflicts_and_fails(monkeypatch, capsys):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["docker", "network", "ls"]:
            out = "aaa\nbbb\n"
        elif cmd[:3] == ["docker", "network", "inspect"]:
            out = json.dumps([network("other", ["172.16.97.0/25"])])
        else:
            out = json.dumps(ROUTES)
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setattr(css.subprocess, "run", run)
    assert css.main(["172.16.97.0/24", "tesp-api_default"]) == 1
    err = capsys.readouterr().err
    assert "172.16.97.0/24 is in use" in err
    assert "Docker network other (172.16.97.0/25)" in err
    assert "src_tes_stack_subnet" in err
    assert ["docker", "network", "inspect", "aaa", "bbb"] in calls
    assert ["ip", "-j", "-4", "route", "show"] in calls


def test_main_with_no_networks_and_a_free_subnet(monkeypatch, capsys):
    def run(cmd, **kwargs):
        out = "" if cmd[:3] == ["docker", "network", "ls"] else json.dumps(ROUTES)
        assert cmd[:3] != ["docker", "network", "inspect"]
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setattr(css.subprocess, "run", run)
    assert css.main(["172.16.97.0/24", "tesp-api_default"]) == 0
    assert "172.16.97.0/24 is free" in capsys.readouterr().out


def test_main_without_docker_checks_the_routes(monkeypatch, capsys):
    # --check on a new workspace: Docker isn't installed yet.
    def run(cmd, **kwargs):
        if cmd[0] == "docker":
            raise FileNotFoundError(cmd[0])
        routes = [{"dst": "172.16.0.0/16", "dev": "eth1"}]
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(routes), stderr="")

    monkeypatch.setattr(css.subprocess, "run", run)
    assert css.main(["172.16.97.0/24", "tesp-api_default"]) == 1
    assert "route 172.16.0.0/16 dev eth1" in capsys.readouterr().err


def test_main_rejects_a_bad_subnet(capsys):
    assert css.main(["172.16.97.1/24", "tesp-api_default"]) == 2
    assert "not an IPv4 network" in capsys.readouterr().err


def test_main_needs_two_arguments(capsys):
    assert css.main(["172.16.97.0/24"]) == 2
    assert "usage" in capsys.readouterr().err


def test_the_script_runs_as_a_program():
    # The playbook runs it with python3 (ansible.builtin.script).
    script = Path(css.__file__)
    done = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, check=False
    )
    assert done.returncode == 2
    assert "usage" in done.stderr
