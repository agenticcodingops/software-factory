#!/bin/sh
set -eu
# Called before EVERY Docker daemon start. Docker then attaches this chain
# before restoring containers. Do not flush another firewall's rules.
if ! iptables -w -n -L DOCKER-USER >/dev/null 2>&1; then
    iptables -w -N DOCKER-USER
fi
# Keep the kit rules ahead of any RETURN without flushing unrelated rules.
# Match iptables' canonical -S output so repeat runs preserve rule counters.
place_rule() {
    position=$1 expected=$2
    shift 2
    current=$(iptables -w -S DOCKER-USER | sed -n "$((position + 1))p")
    count=$(iptables -w -S DOCKER-USER | grep -Fxc -- "$expected" || true)
    if [ "$current" != "$expected" ] || [ "$count" != 1 ]; then
        while iptables -w -C DOCKER-USER "$@" 2>/dev/null; do
            iptables -w -D DOCKER-USER "$@"
        done
        iptables -w -I DOCKER-USER "$position" "$@"
    fi
}
place_rule 1 '-A DOCKER-USER -d 169.254.169.254/32 -j DROP' \
    -d 169.254.169.254/32 -j DROP
# Azure WireServer/HostGAPlugin. Port 53 on this IP remains available for DNS.
place_rule 2 '-A DOCKER-USER -d 168.63.129.16/32 -p tcp -m tcp --dport 80 -j DROP' \
    -d 168.63.129.16/32 -p tcp -m tcp --dport 80 -j DROP
place_rule 3 '-A DOCKER-USER -d 168.63.129.16/32 -p tcp -m tcp --dport 32526 -j DROP' \
    -d 168.63.129.16/32 -p tcp -m tcp --dport 32526 -j DROP
