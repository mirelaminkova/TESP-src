"""Checks that the TESP stack's subnet is free on this workspace (issue #44).

    python3 check_stack_subnet.py SUBNET OWN_NETWORK

The playbook runs it before `docker compose up`. SUBNET (tesp_stack_subnet)
must not overlap another Docker network or a route of the host: Docker would
refuse to create the network, or traffic to that range would go to the wrong
place. OWN_NETWORK is the stack's own network (<project>_default); it and its
bridge's routes are left out, so a redeploy passes. Exits 0 if the subnet is
free, 1 with the conflicts on stderr, 2 on bad arguments.
"""

from __future__ import annotations

import ipaddress
import json
import subprocess
import sys

BRIDGE_NAME_OPTION = "com.docker.network.bridge.name"


def _bridge(net: dict) -> str:
    """The host interface of a Docker bridge network."""
    options = net.get("Options") or {}
    return options.get(BRIDGE_NAME_OPTION) or "br-" + net.get("Id", "")[:12]


def conflicts(subnet: str, networks: list, routes: list, own_network: str) -> list:
    """What overlaps subnet: other Docker networks, then routes."""
    wanted = ipaddress.ip_network(subnet)
    found = []
    own_devs = set()
    for net in networks:
        if net.get("Name") == own_network:
            own_devs.add(_bridge(net))
            continue
        for config in (net.get("IPAM") or {}).get("Config") or []:
            other = config.get("Subnet")
            if not other:
                continue
            other_net = ipaddress.ip_network(other, strict=False)
            if other_net.version == 4 and other_net.overlaps(wanted):
                found.append(f"Docker network {net.get('Name')} ({other})")
    for route in routes:
        dst, dev = route.get("dst"), route.get("dev", "")
        if dst == "default" or dev in own_devs:
            continue
        if ipaddress.ip_network(dst, strict=False).overlaps(wanted):
            found.append(f"route {dst} dev {dev}")
    return found


def _run(*cmd: str) -> str:
    return subprocess.run(list(cmd), capture_output=True, text=True, check=True).stdout


def main(argv: list) -> int:
    if len(argv) != 2:
        print("usage: check_stack_subnet.py SUBNET OWN_NETWORK", file=sys.stderr)
        return 2
    subnet, own_network = argv
    try:
        ipaddress.IPv4Network(subnet)
    except ValueError:
        print(f"{subnet} is not an IPv4 network (a.b.c.d/n)", file=sys.stderr)
        return 2
    try:
        ids = _run("docker", "network", "ls", "-q").split()
    except FileNotFoundError:
        # Not installed yet: only under --check on a new workspace.
        ids = []
    networks = json.loads(_run("docker", "network", "inspect", *ids)) if ids else []
    routes = json.loads(_run("ip", "-j", "-4", "route", "show"))
    found = conflicts(subnet, networks, routes, own_network)
    if found:
        print(
            f"The stack's subnet {subnet} is in use on this workspace: "
            + "; ".join(found)
            + ". Pick a free one with src_tes_stack_subnet.",
            file=sys.stderr,
        )
        return 1
    print(f"{subnet} is free")
    return 0


if __name__ == "__main__":  # pragma: no cover (tests run it as a program)
    sys.exit(main(sys.argv[1:]))
