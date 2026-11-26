#!/usr/bin/env bash
set -euo pipefail
umask 077

die() { printf '%s\n' "$*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die 'Run as root with sudo.'
prepare_only=no
non_interactive=no
while [[ ${1:-} == --* ]]; do
    case "$1" in
        --prepare-only) prepare_only=yes ;;
        --non-interactive) non_interactive=yes ;;
        *) die 'Unknown option' ;;
    esac
    shift
done
if [[ $non_interactive == yes ]]; then export DEBIAN_FRONTEND=noninteractive; fi
[[ $# -eq 1 ]] || die 'Usage: sudo ./bootstrap.sh [--prepare-only] [--non-interactive] /absolute/data/mount'
# Ubuntu 24.04 ships Python 3.12 (tomllib), flock and mountpoint.
# shellcheck source=/dev/null
source /etc/os-release
[[ $ID == ubuntu && $VERSION_ID == 24.04 ]] || die 'Ubuntu 24.04 is required.'
mount=$(readlink -f -- "$1")
[[ $mount != / && $mount =~ ^/[A-Za-z0-9_./-]+$ ]] || die 'Use a dedicated absolute mount path without spaces.'
mountpoint -q -- "$mount" || die 'Mount the persistent data disk first (and configure /etc/fstab).'
kit=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
cd -- "$kit"
# A root-only lock directory (shared with backup-restore.py), never
# world-writable /run/lock; open without truncating whatever is there.
install -d -m 0700 -o root -g root /run/paperclip-hermes
exec 9>>/run/paperclip-hermes/bootstrap.lock
flock -n 9 || die 'Another bootstrap is running.'
if [[ $prepare_only == no ]]; then
    python3 kit-control.py prepare-config
    python3 host-config.py validate-env
fi
if [[ -f .secrets/bootstrap-mount ]]; then
    [[ $(cat .secrets/bootstrap-mount) == "$mount" ]] || die 'Data mount changed; migrate deliberately before bootstrapping.'
fi

# Write only when bytes differ, preserving mtimes and avoiding daemon restarts
# on a second invocation. The temporary is on the destination filesystem.
install_changed() {
    local src=$1 dst=$2 mode=$3
    if [[ ! -f $dst ]] || ! cmp -s -- "$src" "$dst" || [[ $(stat -c '%a:%u:%g' "$dst") != "${mode#0}:0:0" ]]; then
        install -D -m "$mode" -- "$src" "$dst" || die "Cannot install $dst"
        return 0
    fi
    return 1
}

# Install the firewall hook BEFORE the package can auto-start the daemon.
if ! command -v iptables >/dev/null || ! command -v rsync >/dev/null || ! command -v curl >/dev/null || ! command -v ssh-keygen >/dev/null || ! command -v gpg >/dev/null; then
    apt-get update
    apt-get install -y ca-certificates curl iptables rsync openssh-client gnupg age
fi
restart=no
reload=no
if install_changed block-metadata.sh /usr/local/sbin/paperclip-hermes-block-metadata 0755; then restart=yes; fi
temp=$(mktemp -d)
trap 'rm -rf -- "$temp"' EXIT
cat >"$temp/docker.conf" <<EOF
[Unit]
RequiresMountsFor=$mount/docker $mount/containerd
[Service]
ExecStartPre=/usr/bin/mountpoint -q -- $mount
ExecStartPre=/usr/local/sbin/paperclip-hermes-block-metadata
EOF
cat >"$temp/containerd.conf" <<EOF
[Unit]
RequiresMountsFor=$mount/containerd
[Service]
ExecStartPre=/usr/bin/mountpoint -q -- $mount
EOF
if install_changed "$temp/docker.conf" /etc/systemd/system/docker.service.d/paperclip-hermes.conf 0644; then reload=yes; restart=yes; fi
if install_changed "$temp/containerd.conf" /etc/systemd/system/containerd.service.d/paperclip-hermes.conf 0644; then reload=yes; restart=yes; fi
if [[ $reload == yes ]]; then systemctl daemon-reload; fi

# Docker 29's containerd image store is separate from data-root. Put BOTH on
# the mount. Copy stopped state, retaining the old directories for recovery.
# firewall-backend in daemon.json needs Docker Engine 29 or later; an older
# preinstalled engine would refuse the edited configuration and stay stopped.
if command -v dockerd >/dev/null; then
    engine=$(dockerd --version | awk '{print $3}' | cut -d. -f1)
    [[ $engine =~ ^[0-9]+$ && $engine -ge 29 ]] || die "Docker Engine ${engine:-unknown} is older than 29; upgrade it before bootstrapping."
fi
# Plain assignments, so set -e stops the script if either check fails.
config_state=$(python3 host-config.py changed "$mount")
if [[ $config_state == yes ]]; then
    roots=$(python3 host-config.py roots)
    read -r old_docker old_containerd <<<"$roots"
    for pair in "$old_docker:$mount/docker" "$old_containerd:$mount/containerd"; do
        src=${pair%%:*}; dst=${pair#*:}
        [[ $src == "$dst" || $dst != "$src/"* && $src != "$dst/"* ]] || die 'Overlapping data roots are unsafe to copy.'
        if [[ $src != "$dst" && -d $dst ]] && [[ -n $(find "$dst" -mindepth 1 -print -quit) ]]; then
            die "Destination $dst is nonempty; reconcile any interrupted migration first."
        fi
    done
    if systemctl cat docker.service >/dev/null 2>&1; then systemctl stop docker.service docker.socket; fi
    if systemctl is-active --quiet containerd; then systemctl stop containerd.service; fi
    for pair in "$old_docker:$mount/docker" "$old_containerd:$mount/containerd"; do
        src=${pair%%:*}; dst=${pair#*:}
        install -d -m 0711 -- "$dst"
        if [[ $src != "$dst" && -d $src ]]; then rsync -aHAX --numeric-ids -- "$src/" "$dst/"; fi
    done
    python3 host-config.py write "$mount"
    restart=yes
fi

install -d -m 0755 /etc/apt/keyrings
if [[ ! -s /etc/apt/keyrings/docker.asc ]]; then
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o "$temp/docker.asc"
    fingerprint=$(gpg --show-keys --with-colons "$temp/docker.asc" | awk -F: '$1=="fpr" {print $10; exit}')
    [[ $fingerprint == 9DC858229FC7DD38854AE2D88D81803C0EBFCD88 ]] || die 'Docker apt signing key fingerprint mismatch.'
    install -m 0644 "$temp/docker.asc" /etc/apt/keyrings/docker.asc
fi
fingerprint=$(gpg --show-keys --with-colons /etc/apt/keyrings/docker.asc | awk -F: '$1=="fpr" {print $10; exit}')
[[ $fingerprint == 9DC858229FC7DD38854AE2D88D81803C0EBFCD88 ]] || die 'Installed Docker apt signing key fingerprint mismatch.'
cat >"$temp/docker.sources" <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: noble
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
repository_changed=no
if install_changed "$temp/docker.sources" /etc/apt/sources.list.d/docker.sources 0644; then repository_changed=yes; fi
packages=(docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin)
missing=no
for package in "${packages[@]}"; do
    if [[ $(dpkg-query -W -f='${Status}' "$package" 2>/dev/null || true) != 'install ok installed' ]]; then missing=yes; fi
done
if [[ $missing == yes ]]; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get -o Dpkg::Options::=--force-confold install -y "${packages[@]}"
elif [[ $repository_changed == yes ]]; then
    apt-get update
fi
if [[ $restart == yes ]]; then
    systemctl restart containerd.service
    systemctl restart docker.service
fi
systemctl enable --now containerd.service docker.service >/dev/null
/usr/local/sbin/paperclip-hermes-block-metadata
[[ $(docker info --format '{{.DockerRootDir}}') == "$mount/docker" ]] || die 'Docker data root does not match.'

install -d -m 0700 -o root -g root /etc/paperclip-hermes
if [[ ! -f /etc/paperclip-hermes/delivery.key ]]; then
    command -v age-keygen >/dev/null || { apt-get update; apt-get install -y age; }
    age-keygen -o /etc/paperclip-hermes/delivery.key
    chmod 0600 /etc/paperclip-hermes/delivery.key
fi
if [[ $prepare_only == yes ]]; then
    python3 kit-control.py configure-host --prepare-only
else
    python3 kit-control.py configure-host
fi

# A recovery host needs the daemon/firewall prepared without ever starting
# a scheduler or requiring replacement application credentials.
if [[ $prepare_only == yes ]]; then
    # restore.sh decrypts the transferred ciphertext with age (Ubuntu universe).
    if ! command -v age >/dev/null; then
        apt-get update
        apt-get install -y age
    fi
    install -d -m 0700 -o root -g root .secrets
    if [[ ! -f .secrets/bootstrap-mount ]]; then printf '%s\n' "$mount" > .secrets/bootstrap-mount; fi
    printf '%s\n' 'Host prepared. No stack started; use restore.sh --fenced into an empty directory.'
    exit 0
fi

# This SSH identity authorizes only the tool sandbox; it is not a GitHub or
# model credential. Preserve it on repeat runs and in encrypted configuration backups.
if [[ ! -f .secrets/sandbox_ed25519 ]]; then
    [[ ! -e .secrets/sandbox_ed25519.pub ]] || die 'Public SSH key exists without its private key.'
    ssh-keygen -q -t ed25519 -N '' -C tool-sandbox -f .secrets/sandbox_ed25519
fi
[[ -s .secrets/sandbox_ed25519.pub ]] || die 'Sandbox public key is missing.'
chmod 0600 .secrets/sandbox_ed25519
chmod 0644 .secrets/sandbox_ed25519.pub
if [[ ! -f .secrets/bootstrap-mount ]]; then printf '%s\n' "$mount" > .secrets/bootstrap-mount; fi
docker compose --env-file .secrets/compose.env config -q
docker compose --env-file .secrets/compose.env up -d --build --wait --wait-timeout 300
printf '%s\n' 'Stack started. Use the approval-gated workflows and sfctl for operator sign-ins.'
