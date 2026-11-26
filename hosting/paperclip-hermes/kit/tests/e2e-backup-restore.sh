#!/usr/bin/env bash
# Run on a disposable Linux Docker host. Creates only local Compose resources.
set +x
set -euo pipefail
umask 077
[[ $EUID -eq 0 ]] || { printf '%s\n' 'Run this drill as root.' >&2; exit 1; }
for command in docker age age-keygen python3 tar ssh-keygen; do command -v "$command" >/dev/null; done
kit=$(cd -- "$(dirname -- "$0")/.." && pwd -P)
# Restore requires root-owned parents nobody else can write, so not /var/tmp.
work=$(mktemp -d /var/lib/paperclip-hermes-drill.XXXXXXXX)
source_kit=$work/source
restored_kit=$work/restored
upload=$work/upload
mkdir -m 0700 "$source_kit" "$upload"
id=$(python3 -c 'import secrets; print(secrets.token_hex(6))')
source_project=drill-source-$id
restored_project=drill-restored-$id
source_compose() { (cd "$source_kit" && docker compose --project-name "$source_project" --env-file .secrets/compose.env "$@"); }
restored_compose() { (cd "$restored_kit" && docker compose --project-name "$restored_project" --env-file .secrets/compose.env "$@"); }
cleanup() {
    status=$?
    trap - EXIT
    keep=$(printenv KEEP_DRILL || true)
    if [[ $keep == 1 ]]; then
        printf 'Retained throwaway drill at %s (exit %s)\n' "$work" "$status"
    else
        # rm -v also deletes the sandbox's anonymous /opt/data volume (a VOLUME
        # inherited from the Hermes base image), which down --volumes misses.
        if [[ -f $source_kit/.secrets/compose.env ]]; then
            source_compose rm -fsv sandbox >/dev/null 2>&1 || status=1
            source_compose down --volumes --remove-orphans >/dev/null 2>&1 || status=1
        fi
        if [[ -f $restored_kit/.secrets/compose.env ]]; then
            restored_compose rm -fsv sandbox >/dev/null 2>&1 || status=1
            restored_compose down --volumes --remove-orphans >/dev/null 2>&1 || status=1
        fi
        # Delete only the exact mktemp tree, never a computed project/volume path.
        case "$work" in /var/lib/paperclip-hermes-drill.*) rm -rf -- "$work" ;; *) status=1 ;; esac
    fi
    exit "$status"
}
trap cleanup EXIT
# Copy public kit sources only; never take the caller's live secret directory.
tar -C "$kit" --exclude=.secrets --exclude=.git --exclude=data --exclude=__pycache__ -cf - . | tar -C "$source_kit" -xf -
python3 - "$source_kit" <<'PY'
import os
from pathlib import Path
import secrets
import sys
root = Path(sys.argv[1])
private = root / '.secrets'
private.mkdir(mode=0o700)
lines = []
for line in (root / '.env.example').read_text().splitlines():
    if line and not line.startswith('#'):
        name, _, value = line.partition('=')
        if not value:
            value = secrets.token_hex(32)
        if name == 'TAILSCALE_HOSTNAME':
            value = 'drill-' + secrets.token_hex(6) + '.test.ts.net'
        elif name == 'FIXTURE_REPOSITORY':
            value = 'fixture-owner/drill'
        elif name == 'DASHBOARD_SUBNET':
            value = '172.29.245.0/24'
        elif name == 'DASHBOARD_GATEWAY':
            value = '172.29.245.1'
        lines.append(name + '=' + value)
(private / 'compose.env').write_text('\n'.join(lines) + '\n')
(private / 'github-token').write_text(secrets.token_hex(32) + '\n')
# Neither project publishes a host port. This test never needs Tailscale or a
# provider login, and can coexist with a stack already publishing 3100/9119.
compose = root / 'compose.yaml'
text = compose.read_text()
for line in text.splitlines(keepends=True):
    if line.strip().startswith('ports:'):
        text = text.replace(line, '')
compose.write_text(text)
PY
ssh-keygen -q -t ed25519 -N '' -f "$source_kit/.secrets/sandbox_ed25519"
age-keygen -o "$work/identity" 2>"$work/age-keygen.log"
recipient=$(age-keygen -y "$work/identity")
source_compose up -d --build --wait --wait-timeout 600
source_compose exec -T postgres psql -X -v ON_ERROR_STOP=1 -U paperclip -d paperclip <<'SQL'
INSERT INTO companies (name, issue_prefix, status)
VALUES ('Recovery drill', 'DRL', 'paused') RETURNING id AS company_id \gset
INSERT INTO issues (company_id, title, status, identifier, issue_number)
VALUES (:'company_id', 'Recovery drill known issue', 'backlog', 'DRL-1', 1);
SQL
# Seed a real session through the pinned compatibility API, plus portable memory
# and an inactive named profile whose messaging policy import must disable.
source_compose exec -T --user 10000:10000 hermes /opt/hermes/.venv/bin/python - <<'PY'
from pathlib import Path
from hermes_state import SessionDB
home = Path('/opt/data')
with SessionDB() as db:
    db.create_session('recovery-drill-session', source='cli')
(home / 'memories').mkdir(exist_ok=True)
(home / 'memories/MEMORY.md').write_text('Recovery drill memory\n')
profile = home / 'profiles/drill'
profile.mkdir(parents=True, exist_ok=True)
(profile / 'config.yaml').write_text('platforms:\n  telegram:\n    enabled: true\n')
PY
source_compose exec -T --user 1000:1000 paperclip sh -c 'mkdir -p /paperclip/instances/default/agents/drill; printf "%s\n" "Recovery drill instruction" > /paperclip/instances/default/agents/drill/AGENTS.md'
source_compose exec -T caddy sha256sum /data/caddy/pki/authorities/local/root.crt >"$work/ca-before"
(cd "$source_kit" && bash backup.sh --project "$source_project" --local-dir "$upload" --recipient "$recipient")
archive=$(find "$upload" -maxdepth 1 -type f -name '*.tar.age')
[[ $(find "$upload" -maxdepth 1 -type f -name '*.tar.age' | wc -l) -eq 1 ]]
# The source is now fenced: its containers (including restart policies) and
# networks are removed, while its volumes remain solely for later cleanup.
source_compose rm -fsv sandbox
source_compose down
# Decryption consumes only the uploaded ciphertext and a throwaway identity.
bash "$source_kit/restore.sh" --fenced --archive "$archive" --identity "$work/identity" --target "$restored_kit" --project "$restored_project"
restored_compose exec -T postgres psql -XAt -v ON_ERROR_STOP=1 -U paperclip -d paperclip -c "SELECT identifier || ': ' || title FROM issues WHERE identifier='DRL-1'" | tee "$work/issue-result"
grep -Fx 'DRL-1: Recovery drill known issue' "$work/issue-result"
restored_compose exec -T --user 10000:10000 hermes /opt/hermes/.venv/bin/python - <<'PY'
from pathlib import Path
from hermes_state import SessionDB
from ruamel.yaml import YAML
home = Path('/opt/data')
with SessionDB(read_only=True) as db:
    assert db.get_session('recovery-drill-session') is not None
assert (home / 'memories/MEMORY.md').read_text() == 'Recovery drill memory\n'
policy = YAML(typ='safe').load((home / 'profiles/drill/config.yaml').read_text())
assert policy['platforms']['telegram']['enabled'] is False
# KIT_MESSAGING_OFF: every platform Hermes knows is explicitly off in the root too.
root = YAML(typ='safe').load((home / 'config.yaml').read_text())
assert root['platforms'] and all(value == {'enabled': False} for value in root['platforms'].values())
assert all(value['enabled'] is False for value in policy['platforms'].values())
print('Hermes session present: recovery-drill-session; memory present; messaging disabled')
PY
# Restore adds only KIT_MESSAGING_OFF=1; every other setting is the original.
grep -Fx 'KIT_MESSAGING_OFF=1' "$restored_kit/.secrets/compose.env"
grep -vx 'KIT_MESSAGING_OFF=1' "$restored_kit/.secrets/compose.env" | cmp "$source_kit/.secrets/compose.env" -
cmp "$source_kit/.secrets/sandbox_ed25519" "$restored_kit/.secrets/sandbox_ed25519"
restored_compose exec -T caddy sha256sum /data/caddy/pki/authorities/local/root.crt >"$work/ca-after"
cmp "$work/ca-before" "$work/ca-after"
restored_compose exec -T paperclip grep -Fx 'Recovery drill instruction' /paperclip/instances/default/agents/drill/AGENTS.md
printf '%s\n' 'PASS: second Compose project recovered known issue, session, memory, instruction, secrets and original CA.'
