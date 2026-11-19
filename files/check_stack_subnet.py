"""Checks that the TESP stack's subnet is free on this workspace (issue #44).

    python3 check_stack_subnet.py SUBNET PROJECT

The playbook runs it before `docker compose up`. SUBNET (tesp_stack_subnet)
must not overlap another Docker network or a route of the host: Docker would
refuse to create the network, or traffic to that range would go to the wrong
place. The stack's own network (Compose project PROJECT, network "default",
found by its labels) and its bridge's routes are left out, so a redeploy
passes.

Exits 0 if the subnet is free and prints, as JSON, the own network's current
subnet ({"own_subnet": "172.18.0.0/16"}, or null if there is none yet), so the
playbook can recreate a network an earlier version left with another subnet.
Exits 1 with the conflicts (or a failed command) on stderr, 2 on bad
arguments.
"""

from __future__ import annotations

import ipaddress
import json
import subprocess
import sys

BRIDGE_NAME_OPTION = "com.docker.network.bridge.name"
PROJECT_LABEL = "com.docker.compose.project"
NETWORK_LABEL = "com.docker.compose.network"


def _bridge(net: dict) -> str:
    """The host interface of a Docker bridge network."""
    options = net.get("Options") or {}
    return options.get(BRIDGE_NAME_OPTION) or "br-" + net.get("Id", "")[:12]


def _subnets(net: dict) -> list:
    configs = (net.get("IPAM") or {}).get("Config") or []
    return [c["Subnet"] for c in configs if c.get("Subnet")]


def is_own(net: dict, project: str) -> bool:
    """Whether net is the Compose project's default network."""
    labels = net.get("Labels") or {}
    return labels.get(PROJECT_LABEL) == project and labels.get(NETWORK_LABEL) == "default"


def conflicts(subnet: str, networks: list, routes: list, project: str) -> list:
    """What overlaps subnet: other Docker networks, then other routes."""
    wanted = ipaddress.ip_network(subnet)
    found = []
    # Routes of Docker's bridges: the own one's don't count, the others' are
    # reported as their networks.
    bridge_devs = set()
    for net in networks:
        bridge_devs.add(_bridge(net))
        if is_own(net, project):
            continue
        for other in _subnets(net):
            other_net = ipaddress.ip_network(other, strict=False)
            if other_net.version == 4 and other_net.overlaps(wanted):
                found.append(f"Docker network {net.get('Name')} ({other})")
    for route in routes:
        dst, dev = route.get("dst"), route.get("dev")
        if dst == "default" or dev in bridge_devs:
            continue
        route_net = ipaddress.ip_network(dst, strict=False)
        # A VPN's two /1 routes stand in for the default route; the bridge's
        # own, more specific route still wins for the subnet.
        if route_net.prefixlen <= 1:
            continue
        if route_net.overlaps(wanted):
            where = f"dev {dev}" if dev else route.get("type", "without a device")
            found.append(f"route {dst} {where}")
    return found


def own_subnet(networks: list, project: str):
    """The stack's own network's subnet, or None."""
    for net in networks:
        if is_own(net, project):
            subnets = _subnets(net)
            return subnets[0] if subnets else None
    return None


def _run(*cmd: str) -> str:
    return subprocess.run(list(cmd), capture_output=True, text=True, check=True).stdout


def _networks() -> list:
    try:
        ids = _run("docker", "network", "ls", "-q").split()
    except FileNotFoundError:
        # Not installed yet: only under --check on a new workspace.
        return []
    networks = []
    # One at a time: a network removed in between is skipped, not fatal.
    for net_id in ids:
        try:
            networks += json.loads(_run("docker", "network", "inspect", net_id))
        except subprocess.CalledProcessError as e:
            if "No such network" not in (e.stderr or ""):
                raise
    return networks


def main(argv: list) -> int:
    if len(argv) != 2:
        print("usage: check_stack_subnet.py SUBNET PROJECT", file=sys.stderr)
        return 2
    subnet, project = argv
    try:
        ipaddress.IPv4Network(subnet)
    except ValueError:
        print(f"{subnet} is not an IPv4 network (a.b.c.d/n)", file=sys.stderr)
        return 2
    try:
        networks = _networks()
        routes = json.loads(_run("ip", "-j", "-4", "route", "show"))
    except subprocess.CalledProcessError as e:
        print(f"{' '.join(e.cmd)} failed: {(e.stderr or '').strip()}", file=sys.stderr)
        return 1
    found = conflicts(subnet, networks, routes, project)
    if found:
        print(
            f"The stack's subnet {subnet} is in use on this workspace: "
            + "; ".join(found)
            + ". Pick a free one with src_tes_stack_subnet.",
            file=sys.stderr,
        )
        return 1
    print(json.dumps({"own_subnet": own_subnet(networks, project)}))
    return 0


if __name__ == "__main__":  # pragma: no cover (tests run it as a program)
    sys.exit(main(sys.argv[1:]))
