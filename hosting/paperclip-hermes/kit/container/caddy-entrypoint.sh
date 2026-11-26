#!/bin/sh
set -eu
caddy run --config /etc/caddy/Caddyfile --adapter caddyfile &
caddy_pid=$!
# Forward the stop signal, then wait for Caddy to drain before exiting.
trap 'kill -TERM "$caddy_pid" 2>/dev/null || true; wait "$caddy_pid"; exit $?' TERM INT
# Share only the public root certificate; never mount Caddy's private CA data
# into Paperclip, whose coding workers can read their container's mounts.
count=0
while [ ! -s /data/caddy/pki/authorities/local/root.crt ]; do
    kill -0 "$caddy_pid"
    count=$((count + 1))
    [ "$count" -lt 60 ] || exit 1
    sleep 1
done
cp /data/caddy/pki/authorities/local/root.crt /public-ca/root.crt
chmod 0644 /public-ca/root.crt
wait "$caddy_pid"
