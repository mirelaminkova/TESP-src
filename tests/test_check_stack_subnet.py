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

PROJECT = "tesp-api"


def network(name, subnets, net_id="0123456789abcdef", bridge_name=None, project=None):
    options = {}
    if bridge_name:
        options["com.docker.network.bridge.name"] = bridge_name
    labels = {}
    if project:
        labels = {"com.docker.compose.project": project, "com.docker.compose.network": "default"}
    return {
        "Name": name,
        "Id": net_id,
        "Options": options,
        "Labels": labels,
        "IPAM": {"Config": [{"Subnet": s} for s in subnets]},
    }


ROUTES = [
    {"dst": "default", "gateway": "10.77.0.1", "dev": "eth0"},
    {"dst": "10.77.0.0/24", "dev": "eth0"},
    {"dst": "10.77.0.1", "dev": "eth0"},
    {"dst": "172.17.0.0/16", "dev": "docker0"},
]


def test_a_free_subnet_has_no_conflicts():
    networks = [network("bridge", ["172.17.0.0/16"], bridge_name="docker0")]
    assert css.conflicts("172.16.97.0/24", networks, ROUTES, PROJECT) == []


def test_another_docker_network_conflicts_once():
    # Its bridge's route is the same conflict; it isn't reported twice.
    other = network("other_default", ["172.16.0.0/16"], net_id="feedfacecafe0000", project="other")
    routes = [{"dst": "172.16.0.0/16", "dev": "br-feedfacecafe"}]
    assert css.conflicts("172.16.97.0/24", [other], routes, PROJECT) == [
        "Docker network other_default (172.16.0.0/16)"
    ]


def test_a_smaller_network_inside_conflicts_too():
    networks = [network("small", ["172.16.97.128/28"])]
    assert css.conflicts("172.16.97.0/24", networks, [], PROJECT) == [
        "Docker network small (172.16.97.128/28)"
    ]


def test_the_stacks_own_network_and_its_route_are_ignored():
    # A redeploy: the stack's network exists, with the same or an old subnet.
    own = network("tesp-api_default", ["172.16.97.0/24"], net_id="88247bdb9adc0011", project=PROJECT)
    routes = ROUTES + [{"dst": "172.16.97.0/24", "dev": "br-88247bdb9adc"}]
    assert css.conflicts("172.16.97.0/24", [own], routes, PROJECT) == []


def test_the_own_network_is_found_by_its_labels_not_its_name():
    # Another project's network with the same name still counts.
    own = network("x", ["172.16.97.0/24"], bridge_name="tesp0", project=PROJECT)
    lookalike = network("tesp-api_default", ["172.16.97.0/24"], project="other")
    other_net = network("y", ["172.16.97.0/24"], project=PROJECT)
    other_net["Labels"]["com.docker.compose.network"] = "auth"
    routes = [{"dst": "172.16.97.0/24", "dev": "tesp0"}]
    assert css.conflicts("172.16.97.0/24", [own, lookalike, other_net], routes, PROJECT) == [
        "Docker network tesp-api_default (172.16.97.0/24)",
        "Docker network y (172.16.97.0/24)",
    ]


def test_a_host_route_conflicts():
    routes = ROUTES + [{"dst": "172.16.0.0/12", "dev": "wg0"}]
    assert css.conflicts("172.16.97.0/24", [], routes, PROJECT) == ["route 172.16.0.0/12 dev wg0"]


def test_a_single_address_route_conflicts():
    routes = [{"dst": "172.16.97.5", "dev": "tun0"}]
    assert css.conflicts("172.16.97.0/24", [], routes, PROJECT) == ["route 172.16.97.5 dev tun0"]


def test_a_route_without_a_device_names_its_type():
    routes = [{"dst": "172.16.0.0/12", "type": "blackhole"}, {"dst": "172.16.97.0/28"}]
    assert css.conflicts("172.16.97.0/24", [], routes, PROJECT) == [
        "route 172.16.0.0/12 blackhole",
        "route 172.16.97.0/28 without a device",
    ]


def test_vpn_split_default_routes_are_ignored():
    routes = [{"dst": "0.0.0.0/1", "dev": "tun0"}, {"dst": "128.0.0.0/1", "dev": "tun0"}]
    assert css.conflicts("172.16.97.0/24", [], routes, PROJECT) == []


def test_the_default_route_and_ipv6_subnets_are_ignored():
    networks = [network("v6", ["fd00::/64"])]
    routes = [{"dst": "default", "dev": "eth0"}]
    assert css.conflicts("172.16.97.0/24", networks, routes, PROJECT) == []


def test_networks_without_ipam_config_are_ignored():
    networks = [
        {"Name": "host", "Id": "aa", "IPAM": {"Config": None}},
        {"Name": "none", "Id": "bb", "IPAM": {}},
        {"Name": "odd", "Id": "cc", "IPAM": {"Config": [{"Gateway": "1.2.3.4"}]}},
    ]
    assert css.conflicts("172.16.97.0/24", networks, [], PROJECT) == []


def test_own_subnet():
    own = network("tesp-api_default", ["172.18.0.0/16"], project=PROJECT)
    assert css.own_subnet([network("a", ["10.0.0.0/8"]), own], PROJECT) == "172.18.0.0/16"
    assert css.own_subnet([network("a", ["10.0.0.0/8"])], PROJECT) is None
    assert css.own_subnet([network("tesp-api_default", [], project=PROJECT)], PROJECT) is None


def fake_run(networks, routes, missing=()):
    """subprocess.run standing in for docker and ip."""
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["docker", "network", "ls"]:
            out = "".join(f"{n['Id']}\n" for n in networks) + "".join(f"{m}\n" for m in missing)
        elif cmd[:3] == ["docker", "network", "inspect"]:
            if cmd[3] in missing:
                raise subprocess.CalledProcessError(
                    1, cmd, stderr=f"Error response from daemon: No such network: {cmd[3]}"
                )
            out = json.dumps([n for n in networks if n["Id"] == cmd[3]])
        else:
            out = json.dumps(routes)
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    return run, calls


def test_main_reports_conflicts_and_fails(monkeypatch, capsys):
    networks = [network("other", ["172.16.97.0/25"], net_id="aaa")]
    run, calls = fake_run(networks, ROUTES)
    monkeypatch.setattr(css.subprocess, "run", run)
    assert css.main(["172.16.97.0/24", PROJECT]) == 1
    err = capsys.readouterr().err
    assert "172.16.97.0/24 is in use" in err
    assert "Docker network other (172.16.97.0/25)" in err
    assert "src_tes_stack_subnet" in err
    assert ["docker", "network", "inspect", "aaa"] in calls
    assert ["ip", "-j", "-4", "route", "show"] in calls


def test_main_prints_the_own_networks_old_subnet(monkeypatch, capsys):
    # Left by an earlier version with Docker's choice; a network removed
    # between ls and inspect is skipped.
    own = network("tesp-api_default", ["172.18.0.0/16"], net_id="bbb", project=PROJECT)
    run, _ = fake_run([own], ROUTES, missing=["gone"])
    monkeypatch.setattr(css.subprocess, "run", run)
    assert css.main(["172.16.97.0/24", PROJECT]) == 0
    assert json.loads(capsys.readouterr().out) == {"own_subnet": "172.18.0.0/16"}


def test_main_with_no_networks_and_a_free_subnet(monkeypatch, capsys):
    run, calls = fake_run([], ROUTES)
    monkeypatch.setattr(css.subprocess, "run", run)
    assert css.main(["172.16.97.0/24", PROJECT]) == 0
    assert json.loads(capsys.readouterr().out) == {"own_subnet": None}
    assert not [c for c in calls if c[:3] == ["docker", "network", "inspect"]]


def test_main_reports_a_failed_command(monkeypatch, capsys):
    def run(cmd, **kwargs):
        raise subprocess.CalledProcessError(
            1, cmd, stderr="Cannot connect to the Docker daemon at unix:///var/run/docker.sock\n"
        )

    monkeypatch.setattr(css.subprocess, "run", run)
    assert css.main(["172.16.97.0/24", PROJECT]) == 1
    err = capsys.readouterr().err
    assert "docker network ls -q failed: Cannot connect to the Docker daemon" in err


def test_main_reports_a_failed_inspect(monkeypatch, capsys):
    def run(cmd, **kwargs):
        if cmd[:3] == ["docker", "network", "ls"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="aaa\n", stderr="")
        raise subprocess.CalledProcessError(1, cmd, stderr="permission denied")

    monkeypatch.setattr(css.subprocess, "run", run)
    assert css.main(["172.16.97.0/24", PROJECT]) == 1
    assert "docker network inspect aaa failed: permission denied" in capsys.readouterr().err


def test_main_without_docker_checks_the_routes(monkeypatch, capsys):
    # --check on a new workspace: Docker isn't installed yet.
    def run(cmd, **kwargs):
        if cmd[0] == "docker":
            raise FileNotFoundError(cmd[0])
        routes = [{"dst": "172.16.0.0/16", "dev": "eth1"}]
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(routes), stderr="")

    monkeypatch.setattr(css.subprocess, "run", run)
    assert css.main(["172.16.97.0/24", PROJECT]) == 1
    assert "route 172.16.0.0/16 dev eth1" in capsys.readouterr().err


def test_main_rejects_a_bad_subnet(capsys):
    assert css.main(["172.16.97.1/24", PROJECT]) == 2
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
