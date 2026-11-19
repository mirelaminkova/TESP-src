#!/usr/bin/env bash
# Checks the task network fence (templates/task-fence.sh.j2, issue #45) in a
# throwaway network namespace, so it never touches the host's firewall:
#
#   sudo tests/e2e/check-task-fence.sh <rendered tesp-task-fence> <task user>
#
# The fence must have been rendered for <task user> (render-task-fence.yml
# uses "nobody"). Root stands in for every other user. Stand-ins for the
# workspace: services on loopback and on the node's own addresses, and, in a
# second namespace behind veth links, containers behind "docker0", a "br-*"
# bridge and a custom-named one, the cloud metadata address and a host
# outside. Every target
# is first shown reachable without the fence, so a "refused" afterwards is the
# fence's doing. Needs root, iproute2, iptables, ip6tables, setpriv, python3.
set -euo pipefail

if [ "${TESP_FENCE_IN_NETNS:-}" != 1 ]; then
    [ $# = 2 ] || { echo "usage: $0 <rendered fence script> <task user>" >&2; exit 2; }
    [ "$(id -u)" = 0 ] || { echo "$0: needs root (it runs in its own network namespace)" >&2; exit 2; }
    # --mount: `ip netns add` below bind-mounts under /run/netns; a private
    # mount namespace keeps that (and the peer namespace) off the host.
    exec env TESP_FENCE_IN_NETNS=1 unshare --net --mount -- "$0" "$@"
fi

fence=$1
task_user=$2
fails=0
pass() { printf 'PASS %s\n' "$1"; }
fail() { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }

# ---- Stand-ins ------------------------------------------------------------
mount -t tmpfs tmpfs /run   # private /run: netns mounts, xtables lock
ip netns add peer
ip link set lo up
ip netns exec peer ip link set lo up
# The node's own address, on a link that goes nowhere.
ip link add eth0 type dummy
ip addr add 10.99.0.10/24 dev eth0
ip link set eth0 up
ip -6 addr add fd00:99::10/64 dev eth0 nodad
# veth <name>:<our address> <peer addresses...>: a link whose far end is in "peer".
veth() {
    local name=${1%%:*} addr=${1#*:}; shift
    ip link add "$name" type veth peer name "p-$name" netns peer
    ip addr add "$addr" dev "$name"; ip link set "$name" up
    for a in "$@"; do ip netns exec peer ip addr add "$a" dev "p-$name"; done
    ip netns exec peer ip link set "p-$name" up
}
veth docker0:172.31.0.1/24 172.31.0.2/24
veth br-tesp:172.30.0.1/24 172.30.0.2/24 172.30.0.3/24
# A bridge with a name no default pattern covers (a Docker network with
# com.docker.network.bridge.name, lxdbr0, ...): render-task-fence.yml passes
# it as found on the host, as the playbook does (issue #49).
veth tesp-custom:172.29.0.1/24 172.29.0.2/24
veth ext0:198.51.100.1/24 198.51.100.2/24 169.254.169.254/32
ip -6 addr add fd00:51::1/64 dev ext0 nodad
ip netns exec peer ip -6 addr add fd00:51::2/64 dev p-ext0 nodad
ip route add 169.254.169.254/32 dev ext0
ip netns exec peer ip route add default dev p-ext0

# tcp|udp HOST PORT [USER] -> open | refused | unreachable | timeout
probe() {
    local who=(); [ -n "${4:-}" ] && who=(setpriv --reuid="$4" --regid="$(id -g "$4")" --clear-groups)
    # PROBE_NETNS=peer: probe from the outside host instead.
    [ -n "${PROBE_NETNS:-}" ] && who=(ip netns exec "$PROBE_NETNS" "${who[@]}")
    "${who[@]}" python3 -I - "$@" <<'EOF'
import socket, sys
proto, host, port = sys.argv[1], sys.argv[2], int(sys.argv[3])
fam = socket.AF_INET6 if ":" in host else socket.AF_INET
s = socket.socket(fam, socket.SOCK_STREAM if proto == "tcp" else socket.SOCK_DGRAM)
s.settimeout(1.5)
try:
    s.connect((host, port))
    if proto == "udp":          # the listeners echo
        s.send(b"ping"); s.recv(16)
    print("open")
except ConnectionRefusedError:
    print("refused")
except TimeoutError:
    print("timeout")
except OSError:                 # EHOSTUNREACH, ENETUNREACH, EPERM, ...
    print("unreachable")
EOF
}

# serve tcp|udp HOST PORT [USER] [NETNS]: a listener (UDP: echo) for the
# rest of the run.
pids=()
trap 'kill "${pids[@]}" 2>/dev/null || true' EXIT
serve() {
    local cmd=(python3 -I -c '
import socket, sys
proto, host, port = sys.argv[1], sys.argv[2], int(sys.argv[3])
fam = socket.AF_INET6 if ":" in host else socket.AF_INET
s = socket.socket(fam, socket.SOCK_STREAM if proto == "tcp" else socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
if fam == socket.AF_INET6:      # [::]:8913 next to Pulsar on 0.0.0.0:8913
    s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
s.bind((host, port))
if proto == "tcp":
    s.listen(8)
    while True:
        c, _ = s.accept(); c.close()
while True:
    d, a = s.recvfrom(64)
    try:
        s.sendto(d, a)
    except PermissionError:     # the fence drops answers from the task user
        pass
' "$1" "$2" "$3")
    [ -n "${4:-}" ] && cmd=(setpriv --reuid="$4" --regid="$(id -g "$4")" --clear-groups "${cmd[@]}")
    [ -n "${5:-}" ] && cmd=(ip netns exec "$5" "${cmd[@]}")
    "${cmd[@]}" &
    pids+=($!)
}

# TCP and UDP targets that tasks must not reach, and two they need.
# Pulsar's port at the node's own addresses, a bridge gateway included (the
# host.docker.internal path), is the task user's own listener below.
fenced_tcp=(127.0.0.1:8080 127.0.0.1:22 10.99.0.10:443 ::1:8080 fd00:99::10:22
            127.0.0.1:8913 10.99.0.10:8913 172.31.0.1:8913
            172.31.0.2:27017 172.30.0.2:8080 172.30.0.3:8913 172.29.0.2:5432 169.254.169.254:80)
fenced_udp=(127.0.0.1:5353 10.99.0.10:5353 ::1:5353 172.31.0.2:5353)
for t in 127.0.0.1:8080 127.0.0.1:22 10.99.0.10:443 ::1:8080 fd00:99::10:22 127.0.0.1:6817 127.0.0.53:53; do
    serve tcp "${t%:*}" "${t##*:}"
done
for t in 127.0.0.1:5353 10.99.0.10:5353 ::1:5353 127.0.0.53:53; do serve udp "${t%:*}" "${t##*:}"; done
for t in 172.31.0.2:27017 172.30.0.2:8080 172.30.0.3:8913 172.29.0.2:5432 169.254.169.254:80 198.51.100.2:443; do
    serve tcp "${t%:*}" "${t##*:}" "" peer
done
for t in 172.31.0.2:5353 198.51.100.2:53; do serve udp "${t%:*}" "${t##*:}" "" peer; done
# The task user's own listeners: Pulsar on its port (IPv4 only, like
# uwsgi), and a task on [::]:8913 and on high ports.
serve tcp 0.0.0.0 8913 "$task_user"
serve tcp :: 8913 "$task_user"
serve tcp 0.0.0.0 45678 "$task_user"
serve tcp :: 45679 "$task_user"
serve udp 0.0.0.0 45680 "$task_user"

# split HOST:PORT: sets the globals host and port (IPv6 hosts keep their colons).
split() { host=${1%:*}; port=${1##*:}; }
expect() {  # expect NAME WANT PROTO HOST PORT [USER]
    local got; got=$(probe "$3" "$4" "$5" "${6:-}")
    if [ "$got" = "$2" ]; then pass "$1: $got"; else fail "$1: $got, want $2"; fi
}

# ---- Before the fence: every target is reachable, also by the task user -----
ready() {  # ready tcp|udp HOST PORT: 50 tries for the listener to answer root
    local _; for _ in $(seq 50); do [ "$(probe "$1" "$2" "$3")" = open ] && return 0; sleep 0.2; done; return 1
}
for t in "${fenced_tcp[@]}" 127.0.0.1:6817 127.0.0.53:53 198.51.100.2:443 127.0.0.1:45678 ::1:45679 ::1:8913; do
    split "$t"; ready tcp "$host" "$port" || fail "tcp listener $t never came up"
done
for t in "${fenced_udp[@]}" 127.0.0.53:53 198.51.100.2:53 127.0.0.1:45680; do
    split "$t"; ready udp "$host" "$port" || fail "udp listener $t never came up"
done
for t in "${fenced_tcp[@]}"; do split "$t"; expect "no fence: task -> tcp $t" open tcp "$host" "$port" "$task_user"; done
for t in "${fenced_udp[@]}"; do split "$t"; expect "no fence: task -> udp $t" open udp "$host" "$port" "$task_user"; done
expect "no fence: root -> task listening on tcp 45678" open tcp 127.0.0.1 45678
expect "no fence: root -> task listening on udp 45680" open udp 127.0.0.1 45680
PROBE_NETNS=peer expect "no fence: outside host -> task listening on tcp 45678" open tcp 198.51.100.1 45678
PROBE_NETNS=peer expect "no fence: outside host -> task listening on udp 45680" open udp 198.51.100.1 45680
PROBE_NETNS=peer expect "no fence: outside host -> task on [::]:8913" open tcp fd00:51::1 8913

# ---- The fence ------------------------------------------------------------
out=$(sh "$fence")
if [ "$out" = changed ]; then pass "first run reports: changed"; else fail "first run printed: $out"; fi

for t in "${fenced_tcp[@]}"; do split "$t"; expect "task -/-> tcp $t" refused tcp "$host" "$port" "$task_user"; done
# A REJECT in OUTPUT fails a local UDP send with EPERM ("unreachable").
for t in "${fenced_udp[@]}"; do split "$t"; expect "task -/-> udp $t" unreachable udp "$host" "$port" "$task_user"; done
expect "task -> DNS on loopback (tcp)" open tcp 127.0.0.53 53 "$task_user"
expect "task -> DNS on loopback (udp)" open udp 127.0.0.53 53 "$task_user"
expect "task -> slurmctld on loopback (Pulsar's DRMAA)" open tcp 127.0.0.1 6817 "$task_user"
expect "task -> outside host (tcp, not fenced)" open tcp 198.51.100.2 443 "$task_user"
expect "task -> outside host (udp, not fenced)" open udp 198.51.100.2 53 "$task_user"
expect "root -> loopback 8080 (other users unaffected)" open tcp 127.0.0.1 8080
expect "root -> docker0 container (other users unaffected)" open tcp 172.31.0.2 27017
expect "root -> Pulsar's port (Pulsar still answers)" open tcp 127.0.0.1 8913
expect "root -> task listening on tcp 45678 (no answer)" timeout tcp 127.0.0.1 45678
expect "root -> task listening on node address :45678" timeout tcp 10.99.0.10 45678
expect "root -> task listening on IPv6 :45679" timeout tcp ::1 45679
expect "root -> task listening on udp 45680 (no answer)" timeout udp 127.0.0.1 45680
# From another host: the replies aren't to a local address, so only the
# fence's "no answering" rule stops them.
PROBE_NETNS=peer expect "outside host -> task listening on tcp 45678" timeout tcp 198.51.100.1 45678
PROBE_NETNS=peer expect "outside host -> task listening on udp 45680" timeout udp 198.51.100.1 45680
PROBE_NETNS=peer expect "outside host -> task on [::]:8913 (no answer over IPv6)" timeout tcp fd00:51::1 8913
# Pulsar answers pulsar-proxy (on a bridge) and loopback, nobody else.
PROBE_NETNS=peer expect "container on br-* -> Pulsar's port (answers)" open tcp 172.30.0.1 8913
PROBE_NETNS=peer expect "outside host -> Pulsar's port (no answer)" timeout tcp 198.51.100.1 8913

# ---- Idempotent: a second run leaves the same rules ------------------------
saved_rules() { for t in iptables ip6tables; do "$t"-save -t filter | grep -v '^#' | sed 's/\[[0-9]*:[0-9]*\]//'; done; }
before=$(saved_rules)
out=$(sh "$fence")
after=$(saved_rules)
if [ "$before" = "$after" ]; then pass "second run: same rules"; else fail "second run changed the rules"; diff <(echo "$before") <(echo "$after") || true; fi
if [ -z "$out" ]; then pass "second run reports nothing"; else fail "second run printed: $out"; fi
for t in iptables ip6tables; do
    n=$("$t" -S OUTPUT | grep -c -- '-j TESP-TASKS' || true)
    if [ "$n" = 1 ]; then pass "$t: one jump to TESP-TASKS in OUTPUT"; else fail "$t: $n jumps to TESP-TASKS in OUTPUT"; fi
    first=$("$t" -S OUTPUT | sed -n 2p)
    case $first in *TESP-TASKS*) pass "$t: the jump is OUTPUT's first rule" ;; *) fail "$t: OUTPUT starts with: $first" ;; esac
done
expect "after the second run: task -/-> loopback 8080" refused tcp 127.0.0.1 8080 "$task_user"

# ---- Repair: what ufw does to OUTPUT, then a run of the script -------------
for t in iptables ip6tables; do "$t" -I OUTPUT 1 -o lo -j ACCEPT; done
expect "a rule ahead of the jump: task -> loopback 8080 gets through" open tcp 127.0.0.1 8080 "$task_user"
out=$(sh "$fence")
for t in iptables ip6tables; do
    s=$("$t" -S OUTPUT)
    case $(sed -n 2p <<<"$s") in *TESP-TASKS*) pass "$t: run after a rule ahead: the jump is first again" ;; *) fail "$t: OUTPUT starts with: $(sed -n 2p <<<"$s")" ;; esac
    n=$(grep -c -- '-j TESP-TASKS' <<<"$s" || true)
    if [ "$n" = 1 ]; then pass "$t: still one jump"; else fail "$t: $n jumps"; fi
done
if [ "$out" = changed ]; then pass "that run reports: changed"; else fail "that run printed: $out"; fi
expect "after the repair: task -/-> loopback 8080" refused tcp 127.0.0.1 8080 "$task_user"
for t in iptables ip6tables; do "$t" -F OUTPUT; done
expect "jump dropped (OUTPUT flushed): task -> loopback 8080 gets through" open tcp 127.0.0.1 8080 "$task_user"
sh "$fence" >/dev/null
expect "after a run: task -/-> loopback 8080 again" refused tcp 127.0.0.1 8080 "$task_user"

# A jump left for another uid (the user re-created) in first place.
uid=$(id -u "$task_user")
for t in iptables ip6tables; do
    "$t" -F OUTPUT; "$t" -A OUTPUT -m owner --uid-owner 4242 -j TESP-TASKS
done
expect "stale jump for uid 4242: task -> loopback 8080 gets through" open tcp 127.0.0.1 8080 "$task_user"
sh "$fence" >/dev/null
for t in iptables ip6tables; do
    s=$("$t" -S OUTPUT | tail -n +2)
    want="-A OUTPUT -m owner --uid-owner $uid -j TESP-TASKS"
    if [ "$s" = "$want" ]; then pass "$t: stale jump replaced by the task user's"; else fail "$t: OUTPUT is: $s"; fi
done
expect "after replacing it: task -/-> loopback 8080" refused tcp 127.0.0.1 8080 "$task_user"
# A run with nothing to do inserts and deletes nothing (no "-I"/"-D" call).
calls=$(sh -x "$fence" 2>&1 | grep -cE '^\+ ip6?tables -w -[ID] OUTPUT' || true)
if [ "$calls" = 0 ]; then pass "a run with nothing to do touches no OUTPUT rule"; else fail "a run with nothing to do made $calls -I/-D calls"; fi

# ---- Removal (--remove, tesp_task_fence_enabled: false; issue #48) ---------
# Another jump to the chain further down, and a stale one for another uid:
# --remove takes every jump, then the chain, in both families.
for t in iptables ip6tables; do
    "$t" -A OUTPUT -o lo -j ACCEPT
    "$t" -A OUTPUT -m owner --uid-owner 4242 -j TESP-TASKS
done
out=$(sh "$fence" --remove 2>&1) || fail "--remove exited non-zero: $out"
if [ "$out" = changed ]; then pass "--remove reports: changed"; else fail "--remove printed: $out"; fi
for t in iptables ip6tables; do
    if "$t" -S TESP-TASKS >/dev/null 2>&1; then fail "$t: chain TESP-TASKS still there"; else pass "$t: chain TESP-TASKS gone"; fi
    n=$("$t" -S OUTPUT | grep -c -- 'TESP-TASKS' || true)
    if [ "$n" = 0 ]; then pass "$t: no jump left in OUTPUT"; else fail "$t: $n jump(s) left in OUTPUT"; fi
    if "$t" -S OUTPUT | grep -qx -- '-A OUTPUT -o lo -j ACCEPT'; then pass "$t: other OUTPUT rules kept"; else fail "$t: other OUTPUT rules lost"; fi
done
expect "after --remove: task -> loopback 8080" open tcp 127.0.0.1 8080 "$task_user"
expect "after --remove: task -> [::1]:8080" open tcp ::1 8080 "$task_user"
out=$(sh "$fence" --remove 2>&1) || fail "--remove exited non-zero: $out"
if [ -z "$out" ]; then pass "a second --remove changes nothing"; else fail "a second --remove printed: $out"; fi
# Only the chain, with no jump (an earlier --remove that stopped part-way).
iptables -N TESP-TASKS; iptables -A TESP-TASKS -j RETURN
out=$(sh "$fence" --remove 2>&1) || fail "--remove exited non-zero: $out"
if [ "$out" = changed ] && ! iptables -S TESP-TASKS >/dev/null 2>&1; then pass "--remove takes a chain left without a jump"; else fail "--remove with only the chain: printed '$out', chain $(iptables -S TESP-TASKS >/dev/null 2>&1 && echo kept || echo gone)"; fi
# --remove doesn't need the task user (it may be gone).
if out=$(sh "$fence" --remove 2>&1) && [ -z "$out" ]; then pass "--remove with nothing to do exits 0, silent"; else fail "--remove with nothing to do: $out"; fi
for t in iptables ip6tables; do "$t" -D OUTPUT -o lo -j ACCEPT; done
# Turned on again, the fence is whole.
out=$(sh "$fence")
if [ "$out" = changed ]; then pass "a run after --remove reports: changed"; else fail "a run after --remove printed: $out"; fi
expect "on again: task -/-> loopback 8080" refused tcp 127.0.0.1 8080 "$task_user"
expect "on again: task -/-> [::1]:8080" refused tcp ::1 8080 "$task_user"
# An unknown argument is an error, not a run that applies (or removes) rules.
if sh "$fence" --bogus >/dev/null 2>&1; then fail "an unknown argument was accepted"; else pass "an unknown argument is refused"; fi

echo
if [ "$fails" = 0 ]; then echo "task fence: all checks passed"; else echo "task fence: $fails check(s) failed"; exit 1; fi
