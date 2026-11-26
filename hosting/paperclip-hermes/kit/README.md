# Paperclip and Hermes host kit

This kit implements ADR 0001's host operations. Azure bootstrap, approval gates,
OIDC credential minting, receipt reconciliation and deployment orchestration are
separate work packages. Deployment remains unavailable until those gates and the
container acceptance workflow pass. See the [deployment design](../../../docs/adr/0001-azure-deployment.md).

Use the approved `ph-deploy`, `ph-power`, `ph-ops` and `ph-restore` workflows for
installation, upgrades, power and recovery. Their interfaces are specified in the
ADR; WP3 does not supply those workflows. Portal deployment steps are retired.

## Host configuration

The bootstrap agent fetches the approved commit into a root-owned checkout
`/opt/software-factory/kit-<sha>`. Its kit directory is
`hosting/paperclip-hermes/kit/`; `/opt/software-factory/current` points there.
Every installed checkout and its parents must be root-owned and not writable by
other users. Keep the Compose project name `paperclip-hermes` across checkouts.

Run `bootstrap.sh --non-interactive --prepare-only /data` to prepare an Ubuntu
24.04 host with its dedicated mounted data disk. It installs the firewall before
Docker starts, pins Docker's apt signing-key fingerprint (including an already
installed key), configures Docker and containerd on that disk, generates the
host-only delivery identity and leaves the applications and backup timer off.
**Docker IPv6 must stay off**. The host configuration refuses `ipv6=true` and
writes `ipv6=false`; the kit's metadata DROP rules protect IPv4 forwarding.
Do not enable IPv6 through Docker networks or daemon overrides.

For an initialized host, `bootstrap.sh --non-interactive /data` generates missing
random application keys and the sandbox SSH identity, migrates legacy dotenv
secrets into service files, validates configuration and starts the selected apps.
It does not generate human GitHub or model credentials. The fixture token must be
**fine-grained, expiring, and scoped to the single fixture repository** with only
the permissions its tasks require. The model key should have a hard spend limit.

Copy [.env.example](.env.example) to `.secrets/compose.env` and configure the
non-secret settings. `COMPOSE_PROFILES=paperclip,hermes` selects both apps; either
profile works alone. Set `DASHBOARD_SUBNET` and `DASHBOARD_GATEWAY` together or
leave both at their defaults. Secrets are root-owned 0600 files under `.secrets/`;
the Compose file mounts each only into its intended services. The database
password is shared by PostgreSQL and Paperclip. The fixture token is shared by
the coding services; the sandbox never receives a model or application key.

`/etc/paperclip-hermes/config.json` accepts `timezone` (default `Etc/UTC`) and
`shutdown_time` (default `22:00`), plus the non-secret `target` and `vm_id`
used by unattended backup receipts. Keep this file root-owned 0600. Bootstrap sets the host timezone and renders the
nightly timer 45 minutes before shutdown, without catch-up persistence. It enables
the timer only when validated `backup.env` exists; prepare-only always leaves it
disabled. The workflow start path renews credentials before the catch-up backup.

The Hermes Dockerfile pins `gh=2.46.0-3` and
`openssh-server=1:10.0p1-7+deb13u4` against Debian trixie. Versions were checked
against the [Debian package index](https://deb.debian.org/debian/dists/trixie/main/binary-amd64/Packages.xz).
If a package is retired from that mirror, rebuilding fails until a reviewed pin
update; do not remove the version constraint. Docker's key fingerprint was
verified against its [published signing key](https://download.docker.com/linux/ubuntu/gpg).
All container bases and runtime images retain immutable digest pins.

## Operator commands

`sfctl` runs as root from the installed kit; install a host wrapper or invoke
`/opt/software-factory/current/sfctl` directly. Human secrets travel through
Tailscale SSH check mode:

```sh
tailscale ssh -t admin@host sudo /opt/software-factory/current/sfctl secret set openai-api-key
pm-cli read key | tailscale ssh admin@host sudo /opt/software-factory/current/sfctl secret set openai-api-key --stdin
```

Without `--stdin`, a non-TTY is refused and the interactive prompt never echoes.
`secret set github-token` obtains the actual expiry from GitHub's response header.
Secret names and the backup allowlist are defined by `SECRET_NAMES` and
`SECRET_FILES` in `backup-restore.py`. Secrets never become Compose interpolation
values; restart the affected service through the approved operation after delivery.

Other commands:

- `status`: non-secret journal, backup receipt and worker-token expiry.
- `claim-url`: the Paperclip claim URL, for the operator's private terminal.
- `delivery-recipient`: the public recipient of the host's 0600 delivery key.
- `set-backup-credential`: envelope ciphertext on stdin; validates a replacement
  before atomically installing host-only `backup.env`. Never supply plaintext.
- `restore-key-ephemeral`: a one-use identity in `/run/paperclip-hermes/restore.key`;
  returns only its recipient and refuses to overwrite an existing restore key.
- `set-hostname <fqdn>`: validates and updates the MagicDNS FQDN, then recreates services.
- `messaging off|on`: sets or removes the restore fence. Enabling individual
  messaging platforms still requires their normal configuration and credentials.
- `upgrade <sha> --run-id <id>` and `rollback`: the approved host upgrade/recovery
  protocol; neither command writes Azure approval tags.

`join-tailnet.sh --policy-receipt <path>` consumes envelope ciphertext on stdin.
Before decrypting, it requires a root-only receipt that records reviewed,
passing enforcing lint and an applied policy with the same SHA-256. Report-only
baseline evidence cannot authorize a join. The approval-gated tailnet workflow
must issue that receipt after verifying the live policy. Join uses Tailscale SSH,
`tag:pcpilot`, and Serve for the installed apps. The
[policy fragment](tailscale-policy.hujson) is input for the future tailnet module,
never a standalone replacement for an existing tailnet policy.

## Backup and restore

Install [backup.env.example](backup.env.example) outside the checkout at
`/etc/paperclip-hermes/backup.env` (root 0600, parent 0700). Keep Shared Key off.
The validator accepts only a container-scoped **user-delegation SAS**, with
`skoid`, `sktid`, `skt`, `ske`, `sks=b`, `skv`, `se`, `sr=c`, `spr=https` and
`sp=cw` (or `c` after the deployment self-test). Both expiries must be valid and
at most seven days away, and the SAS cannot outlive its delegation key. Service,
account, stored-policy, duplicate-field, expired and broader-permission forms
are rejected locally. No Shared Key fallback exists.

`backup.sh --reason nightly --target <target> --vm-id <vm-resource-id>` encrypts
a consistent data/configuration bundle to the public recipient. No private
long-term key goes on the host. Only installed writers stop; PostgreSQL remains
running for its dump. Hermes-only skips the absent database; Paperclip-only skips
the Hermes CLI. Writers restart after ordinary backups, including failure paths.

`--canary` uploads encrypted empty content to prove the upload credential, without
stopping writers or advancing the successful data-backup clock. Every upload has
non-secret metadata: kit commit, image digests, schema, recipient fingerprint,
target/VM identity, backup ID, completion time and reason. Archives use unique
`backups/` names and successful receipts use unique `receipts/<target>/` objects.
Receipts include the ciphertext SHA-256, size and returned blob version/ETag.
`last-backup.json`, `last-canary.json` and `last-op.json` are atomic, durable files under
`/data/paperclip-hermes/`; an unresolved upgrade journal survives routine backups.
A receipt publication failure fails the operation even if its encrypted archive
has already landed.

`--reason pre-stop` and `--reason final` require `--stop-operation-id`. They mark
`pre_stop=true` and keep writers stopped after successful publication, including
while encrypting and uploading. A failed backup restarts the old writers. The
workflow must persist the matching observed-stop receipt and enforce the
15-minute window; WP3 does not perform ARM deallocation or receipt reconciliation.

For recovery, fetch the backup metadata, check out **its recorded kit commit**,
and run that checkout's `restore.sh`. The restore refuses different archived code
or pins; it never executes code from an archive. Use a fresh Compose project and
an empty root-owned target reached only through root-owned, non-writable parents:

```sh
bash restore.sh --fenced --archive /data/staged.age \
  --identity /run/paperclip-hermes/restore.key --staged \
  --target /opt/software-factory/restored --project restored --work-dir /data
```

The operator first re-encrypts the selected backup to that ephemeral recipient
and downloads the staged ciphertext onto the host. `--staged` requires the
identity in `/run` and removes it on success or failure. `--verify-only` decrypts
and checks the manifest and every payload hash without creating a project or
starting any service. Full restore imports the database and Hermes portable state,
checks the migration journal and leaves messaging fenced. The long-term identity
must never be supplied to this command. Local offline drills can use
`backup.sh --local-dir <private-directory> --recipient <public-recipient>`.

## Upgrade recovery

The deployment workflow must complete the fresh off-site backup and Terraform
apply before calling `upgrade.sh <approved-workflow-sha> --run-id <id>`.
`backup.sh`, `restore.sh`, `upgrade.sh` and all `sfctl` mutations share the root-only
`/run/paperclip-hermes/op.lock`; contention refuses the operation.

The upgrade refuses unless available space is at least 2.2 times the volume
bytes plus the database dump estimate. It then creates `/data/rollback/<run>`
(root 0700): stop installed application writers, dump the live PostgreSQL, stop
PostgreSQL **only after the dump succeeds**, then archive all stopped volumes.
The rollback point records hashes, the previous commit and image pins. Hermes-only
skips database operations. A failed dump restarts the previously running stack
with a health wait, aborts before archives or candidate installation and alerts
if restart also fails.

The candidate is fetched by full commit SHA, pulled by digest and started with
Compose's health wait. A failed candidate health check activates the previous
checkout first and invokes its rollback implementation, restoring its data and
checking health again. Failed rollback stops the stack and emits an `ops-alert`
referring to the last off-site backup receipt. Healthy candidates retain a
`healthy-awaiting-handoff` journal; another upgrade is blocked until the approved
deployment finishes handoff or rolls back. WP3 preserves the journal and rollback
point; the later ops/deploy package owns approval tags, finalization and pruning
after six successful checks or 24 running hours. Do not prune an unresolved handoff.

`health.sh` prints one JSON line under 4 KB and exits nonzero for a failed check.
It covers installed Compose service health, Paperclip through Caddy, Hermes's
TLS-verified 401 through Caddy, metadata DROP rules/counters, IPv6-off policy,
the migration journal and Tailscale Serve. No credential or application log is
included in its output.

## Verification

`bash ci/check.sh` from the repository root runs all kit unit tests, including
the profile matrix's `docker compose config`, secret isolation, SAS rejection,
rollback failure recovery and lock exclusion. It requires Docker Compose v2 or
later for configuration rendering; the daemon is unnecessary for unit tests.

`sudo env "PATH=$PATH" bash ci/kit-e2e.sh` runs the real container acceptance cases:
upgrade with forced health failure and data rollback; failing dump with the old
stack healthy; and backup under a previous fixture commit, rejection by a different
commit, then fenced restore through the backup's own commit. The weekly and PR
`kit-e2e` workflow runs these on a Docker-capable Ubuntu runner. The fixture
releases use the pinned Paperclip/PostgreSQL/Caddy stack without cloud credentials.
Hermes-only and combined profile sequencing are also covered by unit tests;
live Hermes and cloud control probes remain deployment release gates in the ADR.
