#!/bin/sh
set -eu
/bin/sh /kit/fixture-credentials.sh 10000
install -d -m 0755 /run/sshd /etc/ssh/hostkeys
if [ ! -s /etc/ssh/hostkeys/ssh_host_ed25519_key ]; then
    ssh-keygen -q -t ed25519 -N '' -f /etc/ssh/hostkeys/ssh_host_ed25519_key
fi
install -m 0444 /run/secrets/sandbox_ssh_public_key /run/kit/authorized_keys
chown 10000:10000 /workspace
# The upstream image's hermes user has /opt/data as its home; in this sandbox
# that is disposable, with no model credentials or gateway home mounted.
install -d -m 0700 -o 10000 -g 10000 /opt/data
exec /usr/sbin/sshd -D -e -f /etc/ssh/sshd_config
