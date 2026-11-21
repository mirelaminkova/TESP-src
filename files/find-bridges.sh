#!/bin/sh
# Prints the name of every Linux bridge on this host, one per line, for the
# task network fence (issue #49; playbook.yml, tesp_task_fence_interfaces).
# A bridge that carries a default route is the host's uplink (netplan
# "bridges:", some cloud and bare-metal images): fencing it would cut tasks
# off from image registries and S3, so it is left out.
set -eu
uplinks=$( { ip -4 -o route show default; ip -6 -o route show default; } 2>/dev/null \
    | sed -n 's/.* dev \([^ ]*\).*/\1/p')
for b in /sys/class/net/*/bridge; do
    [ -d "$b" ] || continue
    b=${b%/bridge}; b=${b##*/}
    for u in $uplinks; do [ "$u" = "$b" ] && continue 2; done
    printf '%s\n' "$b"
done
