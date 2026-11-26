# Paperclip and Hermes Compose kit

One dedicated Ubuntu 24.04 host runs Paperclip, PostgreSQL 17, Hermes Agent,
Caddy and a separate SSH tool sandbox. The host must already have a persistent
data disk mounted through `/etc/fstab`, Tailscale, and a firewall denying public
inbound traffic. Provisioning those resources belongs to the approved deployment
workflows; all sign-ins stay with the operator. This kit creates no cloud,
Tailscale or GitHub resource.

This kit implements ADR 0001's host operations. Azure bootstrap, approval gates,
OIDC credential minting, receipt reconciliation and deployment orchestration are
separate work packages. Deployment remains unavailable until those gates and the
container acceptance workflow pass. See the [deployment design](../../../docs/adr/0001-azure-deployment.md).

Use the approved `ph-deploy`, `ph-power`, `ph-ops` and `ph-restore` workflows for
installation, upgrades, power and recovery. Their interfaces are specified in the
ADR; WP3 does not supply those workflows. The host procedures below document
preparation, verification and recovery for those operations; they are not a
Portal deployment flow.

## Connections and image pins

```text
operator on tailnet -> Tailscale Serve :443  -> 127.0.0.1:3100 -> Paperclip
operator on tailnet -> Tailscale Serve :9443 -> 127.0.0.1:9119 -> Hermes dashboard
Paperclip -> https://caddy:443 -> http://hermes:8642
Paperclip -> postgres:5432
Hermes terminal/file tools -> SSH sandbox:2222 -> /workspace
```

With both profiles selected, only 3100 and 9119 are published, both on IPv4
loopback; a single profile publishes only its application's port. PostgreSQL, Caddy,
the raw Hermes API and sandbox SSH publish no ports. Database, TLS and gateway
links are private Compose networks. The agents and sandbox have outbound
access for registries, GitHub and the model provider; they get no tailnet grants.
Neither a host network nor a Docker socket is mounted into a container.

Accepted pilot exposure: Hermes binds its API (8642) and dashboard (9119) to
`0.0.0.0`, including its shared sandbox-network interface. Code in the SSH
sandbox can reach both over plain HTTP, bypassing Caddy; API endpoints other
than the health check require the bearer key, and the dashboard requires its
separate login. Treat code in
this pilot as trusted and keep it confined to the fixture repository. Bridge-only
listener binding is deferred until a live Docker test proves both the Caddy
upstream and the `127.0.0.1:9119` publish/Serve route still work; this revision
does not claim that network isolation. The acceptance check below records it.

Registry index digests were resolved on 25 November 2026, verified against
the manifest bytes and checked for Linux AMD64 and ARM64 entries:

| Image | Multi-platform index digest |
|---|---|
| `ghcr.io/paperclipai/paperclip:2026.1117.0` | `sha256:bdf38d10544dc6c831e16dc027f23ad586353ef3be1a1b2c6979e63b956aceb1` |
| `nousresearch/hermes-agent:v0.23.4` | `sha256:ef0650d523ee1b5ab32c779acabc446d9936869a9932591553688a62bcf0c42f` |
| `postgres:17` | `sha256:87c8b42f423b45d5247e6aa8fdb2e7762fec164c68a50fe3eb07e7bfcaf4dbcf` |
| `caddy:2-alpine` | `sha256:003a97808e781aa9f7e75cc2f43d34d461099031faf5e20faefb9f61126836f0` |

Hermes **v0.23.4** was the latest stable release returned by the upstream
[release API](https://api.github.com/repos/NousResearch/hermes-agent/releases/latest).
It replaces the research's v0.23.3. The kit follows v0.23.4's supervised dashboard,
API enablement and SSH backend settings; live compatibility still needs the
operator checks below. `Dockerfile.hermes` derives two local images from this
pinned base: the runtime adds `gh`, and the sandbox adds an SSH server. These
local builds use the distribution's package repository; retain the built image
IDs if byte-identical rebuilds are needed.

Caddy issues internal certificates for `caddy` and `paperclip.caddy`. The latter
proxies to `paperclip:3100` with the configured MagicDNS Host and forwarded Host
headers for health
checks; the operator still reaches Paperclip through Serve. Both sites bind only
to the `tls` network address (the `paperclip.caddy` alias) and loopback. Neither
site listens on `gateway`, even when a client forges SNI or Host. Hermes and the
sandbox cannot use these listeners. The local Hermes health probe resolves
`caddy` to loopback while verifying its certificate. Caddy's startup copies
only the public root certificate into `public_ca`; Paperclip mounts that volume read-only
and uses `NODE_EXTRA_CA_CERTS=/caddy-ca/root.crt`. Caddy's private root key stays
in `caddy_data`. No TLS verification bypass is configured. Keep that CA during
recovery; after deliberately rotating it, restart Paperclip to reload the root.

The Hermes Dockerfile pins `gh=2.46.0-3` and
`openssh-server=1:10.0p1-7+deb13u4` against Debian trixie. Versions were checked
against the [Debian package index](https://deb.debian.org/debian/dists/trixie/main/binary-amd64/Packages.xz).
If a package is retired from that mirror, rebuilding fails until a reviewed pin
update; do not remove the version constraint. Docker's key fingerprint was
verified against its [published signing key](https://download.docker.com/linux/ubuntu/gpg).
All container bases and runtime images retain immutable digest pins.

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
disabled. The future workflow start path must renew credentials before explicitly running
its catch-up backup; the systemd timer does not catch up at boot.

## Prepare the dedicated host

Use a root-owned Linux checkout at the installation path described above, outside
the repository's normal working copy if that copy is shared with other users.
All checkout parents must be root-owned and not writable by other users. Use
`/data` for the dedicated disk: WP3 stores operation receipts under
`/data/paperclip-hermes` and rollback points under `/data/rollback`.
Do not put any secret into a command argument, issue, transcript or committed file.

1. Format and mount the persistent disk, for example at `/data`, and
   configure its stable filesystem UUID in `/etc/fstab`. On Azure a new data
   disk is raw; find it with `lsblk` (usually `/dev/disk/azure/scsi1/lun0`)
   and format it only when it is new, since formatting erases it:

   ~~~sh
   sudo mkfs.ext4 -L pcdata /dev/disk/azure/scsi1/lun0
   sudo mkdir -p /data
   echo "UUID=$(sudo blkid -s UUID -o value /dev/disk/azure/scsi1/lun0) /data ext4 defaults,nofail 0 2" | sudo tee -a /etc/fstab
   sudo mount -a && mountpoint /data
   ~~~

   The bootstrap refuses an unmounted
   directory. Use a dedicated host: it configures the host's Docker daemon and
   may stop Docker/containerd once to relocate existing state. Do not use an
   existing daemon with live restore enabled, competing daemon command-line
   settings, distro `docker.io` packages or unrelated workloads.
2. Use [tailscale-policy.hujson](tailscale-policy.hujson) only as a documented
   fragment for the tailnet module. Import the existing whole policy privately,
   expand aliases, replace `OPERATOR_LOGIN` and the admin user, and remove or
   narrow additive broad rules that would let another user reach the tag or let
   the tag reach another tailnet device. The fragment delegates `tag:pcpilot`
   ownership to `tag:pc-deployer` and grants the operator only TCP 22, 443 and 9443.
   Render both policy tests and `sshTests` for the real operator and representative
   denied users, tags and hosts. A reviewed, passing enforcing lint and matching
   applied-policy receipt must precede host join; report-only evidence cannot
   authorize it. Use `join-tailnet.sh` as described below to tag the host
   `tag:pcpilot`, enable Tailscale SSH and configure Serve. Enable HTTPS
   certificates for Serve. Keep public inbound denied and leave Funnel disabled.
3. In this directory, prepare root-owned private files:

   ```sh
   sudo install -d -m 0700 -o root -g root .secrets
   sudo install -m 0600 -o root -g root .env.example .secrets/compose.env
   sudo vi .secrets/compose.env
   sudo ./sfctl secret set github-token
   sudo ./sfctl secret set openai-api-key
   ```

   Use the root editor above (or your installed root editor); `sudoedit` can
   refuse paths below a user-writable checkout. Keep `.secrets` root-owned 0700
   and its files root-owned 0600. In `.secrets/compose.env`, set
   `TAILSCALE_HOSTNAME` to the host's full MagicDNS name ending `.ts.net`, and
   `FIXTURE_REPOSITORY` to `owner/repository`. Set `COMPOSE_PROFILES` to
   `paperclip,hermes`, `paperclip` or `hermes`, and select an available
   `HERMES_MODEL`. Non-secret settings must be literal, without quotes,
   whitespace, inline comments or interpolation; bootstrap validates them without
   printing their contents. The example contains no credential.

   Bootstrap generates **eight independent values**, using a separate random
   32-byte hex value per missing password/signing/key file. The master key, both
   Paperclip authentication secrets, approval signing secret, database password,
   gateway key and dashboard password/session secret must all differ. Hex keeps
   the database URL unambiguous. Existing values are preserved; legacy dotenv
   secrets are migrated into the separate files and removed from `compose.env`.
   Deliver the operator's OpenAI API project key through
   `sfctl secret set openai-api-key`, with an enforced hard spend limit; it is required when Hermes
   is selected. Deliver the fixture token through `sfctl secret set github-token`.
   Both prompts require a TTY and do not echo. Human credentials are never
   generated by bootstrap or stored as Compose interpolation values. For a
   deliberate manual replacement, `openssl rand -hex 32` still generates one
   independent application secret; deliver it through `sfctl secret set` using
   the corresponding secret file name.

   The token file contains only the fixture-only fine-grained GitHub token and
   a trailing newline. Scope it to one fixture repository: Contents, Issues and
   Pull requests write, Metadata read, short expiry. The fixture ruleset must
   require a PR, approval of the latest push and CI, with only the operator able
   to update the default branch. Token creation and rulesets are operator work.
4. Run:

   ```sh
   sudo ./bootstrap.sh /data
   ```

The bootstrap installs Docker Engine and Compose from Docker's Ubuntu repository,
sets the daemon's `local` log driver with 10 MB / three-file rotation, and places
both Docker's data root and containerd's separate image store on the mount.
Existing stopped state is copied with ownership/xattrs preserved; original
directories are retained, not deleted. A nonempty migration destination or a
changed mount marker requires manual reconciliation rather than an overwrite.
An interrupted copy is intentionally not resumed automatically.

The systemd Docker drop-in requires the mount and installs three DROP rules
before every daemon start, including reboots: all traffic to IMDS
`169.254.169.254`, and TCP ports 80 and 32526 on Azure WireServer/HostGAPlugin
`168.63.129.16`. DNS on port 53 stays available. The rules lead `DOCKER-USER`,
ahead of a possible RETURN; the hook never flushes other firewall rules.
The daemon uses Docker's iptables backend. **Docker IPv6 must stay off**:
bootstrap refuses `ipv6=true` and writes `ipv6=false`; the metadata DROP rules
protect IPv4 forwarding. Do not enable IPv6 through networks or daemon overrides.
Docker's apt signing-key fingerprint is checked even for an already installed key.
Both daemon configuration and package installation are skipped when already correct. Repeating the same bootstrap
retains keys, state and configuration, adds no rule, and does not restart an
unchanged daemon or recreate unchanged containers; cached image builds are reused.
It does not upgrade packages on repeat runs. Maintain OS/engine security updates
separately, then repeat containment checks after an upgrade or reboot.

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

## Private HTTPS and operator sign-in

`join-tailnet.sh` configures the selected apps' Serve routes. On an already
joined host, inspect or reapply the corresponding routes (omit an unselected app):

```sh
sudo tailscale serve --bg --https=443 http://127.0.0.1:3100
sudo tailscale serve --bg --https=9443 http://127.0.0.1:9119
sudo tailscale serve status
```

Open `https://<MagicDNS FQDN>/` and `https://<MagicDNS FQDN>:9443/` from an
operator device on the tailnet. Paperclip's public/authentication URL and allowed
hostnames all derive from `TAILSCALE_HOSTNAME`. `PAPERCLIP_API_URL` is explicitly
`http://127.0.0.1:3100` for agents and the GitHub broker inside the Paperclip
container; public/authentication URLs still provide browser links and origins. The pinned server derives trusted
origins from that explicit authentication URL and hostname allowlist; there is
no invented `TRUSTED_ORIGINS` environment setting. The Hermes dashboard's public
URL derives from the same name with port 9443. Its trusted proxy is the gateway
of the dedicated dashboard bridge. If its subnet conflicts, change both
`DASHBOARD_SUBNET` and `DASHBOARD_GATEWAY` before starting.

The operator completes these steps:

1. Claim the Paperclip instance and create the human login through its private
   URL. Keep the claim link private. Use authenticated/private mode.
2. Sign the unmodified Codex CLI in through **Paperclip's OpenAI connector** with
   its subscription/device-code flow after enabling that flow in the account's
   security settings. Paperclip Runner uses session-specific provider homes;
   signing in from `docker exec` does not sign in the actual Runner session.
   Check login status from a real Runner session. No model login ships here.
3. Sign in to the Hermes dashboard as `operator` with its separate password.
   Hermes is explicitly configured for the OpenAI **API key** provider. Do not
   use subscription OAuth or device-code sign-in in Hermes. Keep the key's hard
   spend limit enforced.
4. Configure a Paperclip `hermes_gateway` agent with base URL `https://caddy`
   and the separate `HERMES_GATEWAY_KEY` as its bearer secret, stored through
   Paperclip's encrypted secret store. Plain non-loopback HTTP is rejected by
   the adapter. A reverse callback into Paperclip would require a separately
   scoped credential; none is configured here.
5. Add the fixture token to Paperclip's GitHub connection and select that
   connection for the coding worker, as in pilot step 10. In these runs,
   **Paperclip's broker supplies git and gh credentials**. It disables system
   Git configuration (`GIT_CONFIG_SYSTEM=/dev/null`, `GIT_CONFIG_NOSYSTEM=1`),
   so the kit's `/etc/gitconfig` helper does not run. The kit's `gh` wrapper
   preserves a nonempty broker-provided `GH_TOKEN`. The helper and fallback
   token wrapper serve Hermes, the sandbox, and Paperclip runs without a GitHub
   connection when host Git access is enabled. Verify a real git push from a
   Paperclip coding run with the connection selected, using the check below;
   a root-container test does not exercise this credential mode.

Host token files stay root-only. Root entrypoints copy the token into a tmpfs
file owned by the runtime user, mode 0400. Only when `GH_TOKEN` is unset or
empty does the `gh` wrapper read that file and export it to its child process.
The token is also present in the sandbox for Hermes tool `git` and `gh`.
The sandbox SSH identity grants access only to this disposable execution
container. It mounts `/workspace`, its own SSH host keys and the fixture token;
it has no Hermes home, provider environment, backup credentials, host directory
or Docker socket. The server accepts no forwarded environment variables, no
passwords, no port forwarding and no root login. SSH tool/file execution uses
the configured backend; do not add local-shell tools, local MCP servers or
plugins that bypass that boundary.

Known pilot limitation: Paperclip coding workers share the Paperclip container
and can read its master key and signing secrets. Keep the pilot confined to one
fixture repository. The SSH sandbox is a container boundary on this same host,
not a separate VM. Hermes's authoritative configuration and its home `.env` are
reapplied at container startup; persist operator choices in the kit configuration
and host non-secret `.secrets/compose.env` settings; deliver secrets with
`sfctl secret set`. Dashboard edits would be overwritten at the next start.

## Persistent paths and backup contract

The default Compose project name is `paperclip-hermes`; recovery tests can override it with `--project`. Named volumes live under
`<data mount>/docker/volumes/<volume name>/_data`; use `docker volume inspect`
to confirm on the host. Capture consistent logical database dumps, application
state and original secret values, not just Docker's live directories. The host-only
[backup and restore workflow](#nightly-backup-and-fenced-restore-pilot-check-8) below implements this contract.

| Host location / volume | Container path | Contents / recovery requirement |
|---|---|---|
| Kit checkout (entire directory) | `/kit` helpers, read-only | Compose, Dockerfile, bootstrap, Caddy, SSH, Git and Hermes policy |
| Kit `.secrets/compose.env` (0600 root; parent 0700 root) | Non-secret Compose settings per service | Hostname, fixture repository, model, dashboard subnet/gateway, selected profiles and messaging fence |
| Kit `.secrets/{postgres-password,better-auth-secret,paperclip-agent-jwt-secret,paperclip-tool-action-signing-secret,paperclip-secrets-master-key}` (0600 root) | `/run/secrets/` in intended services | DB password shared only by PostgreSQL and Paperclip; authentication, JWT, approval signing and master keys only in Paperclip; its entrypoint exports the runtime values and constructs `DATABASE_URL` |
| Kit `.secrets/{hermes-gateway-key,hermes-dashboard-basic-auth-password,hermes-dashboard-basic-auth-secret,openai-api-key}` (0600 root) | `/run/secrets/` in Hermes only | Gateway, dashboard and provider secrets exported by the Hermes entrypoint; never mounted into the sandbox |
| Kit `.secrets/github-token` (0600 root) | `/run/secrets/fixture_github_token` in both agents and sandbox | Fixture token copied into each `/run/kit/github-token` tmpfs for Hermes, sandbox and Paperclip runs without a connection; connected Paperclip runs use broker git/gh credentials |
| Kit `.secrets/sandbox_ed25519{,.pub}` | Hermes `/run/secrets/sandbox_ssh_key`, sandbox `/run/secrets/sandbox_ssh_public_key` | Sandbox-only identity; private copy `/run/kit/sandbox_ed25519` in Hermes |
| Kit `.secrets/bootstrap-mount` | Host only | Original mount guard |
| `paperclip-hermes_postgres` | `/var/lib/postgresql/data` | PostgreSQL 17 data; use `pg_dump -U paperclip -d paperclip` |
| `paperclip-hermes_paperclip` | `/paperclip` | Entire home, including `instances/default/config.json`, `storage/`, logs, company/agent instructions, checkouts, worktrees and Runner/provider homes; instruction files are not all in storage |
| `paperclip-hermes_hermes` | `/opt/data` | Entire Hermes home: config, `.env`, `auth.json` if created, profiles, memory, sessions, skills, logs and uploads; use the pinned `hermes backup` command as well |
| `paperclip-hermes_sandbox` | `/workspace` | Hermes tool checkouts, worktrees and unpushed changes; not covered merely by backing up the gateway home |
| `paperclip-hermes_sandbox_hostkeys` | `/etc/ssh/hostkeys` | Sandbox server identity; retain with Hermes `known_hosts` to avoid key mismatches after restore |
| `paperclip-hermes_caddy_data` | `/data` | **Private CA**: `caddy/pki/authorities/local/` including root and intermediate keys; encrypted backup required |
| `paperclip-hermes_caddy_config` | `/config` | Caddy autosave state |
| `paperclip-hermes_public_ca` | Caddy `/public-ca`, Paperclip `/caddy-ca` read-only | Public `root.crt` only; regenerate from restored Caddy data |
| `<data mount>/docker` | Host only | Engine state, local image layers, volumes and rotating container logs |
| `<data mount>/containerd` | Host only | Separate containerd image/snapshot store on newer engines |
| `/etc/docker/daemon.json`, `/etc/containerd/config.toml` | Host only | Data-root, image-store and logging settings |
| `/etc/systemd/system/{docker,containerd}.service.d/paperclip-hermes.conf`, `/usr/local/sbin/paperclip-hermes-block-metadata` | Host only | Mount dependencies and persistent metadata block |
| `/etc/apt/keyrings/docker.asc`, `/etc/apt/sources.list.d/docker.sources` | Host only | Docker package source |
| `/etc/fstab`, `/var/lib/tailscale` | Host only | Operator-managed mount and tailnet state; handle node re-enrolment deliberately on restore |
| `/etc/paperclip-hermes/config.json` (0600 root) | Host only | Timezone, shutdown time, backup target and VM resource identity |
| `/etc/paperclip-hermes/delivery.key` (0600 root) | **Never mounted** | Host delivery identity for encrypted join and backup-credential envelopes; not the long-term backup decryption identity |
| `/data/paperclip-hermes/{last-backup,last-canary,last-op,github-token}.json` | Host only | Durable receipts, operation journal and fixture-token expiry; excluded from application recovery payloads |
| `/data/rollback/<run>` (0700 root) | Host only | Local upgrade rollback point; sensitive plaintext, separate from encrypted off-site recovery |
| `/run/paperclip-hermes/{op.lock,restore.key}` | Host only, volatile | Shared operation lock and one-use staged-restore identity |
| `/opt/software-factory/current` | Host only | Active kit symlink; project name stays stable across checkouts |
| `/etc/paperclip-hermes/backup.env` (0600 root; parent 0700 root) | **Never mounted** | Backup destination SAS and public age recipient; exclude from every service and sandbox; long-term backup decryption identity must never be stored on this host |

The `.dockerignore` excludes all secrets and state from the build context. The
runtime `/run/kit` copies and `/tmp` are ephemeral. Never commit configuration
backups containing secrets. Recovery must preserve the original three Paperclip
signing/auth secrets, master key, database password, gateway key and Caddy CA;
generating replacements can invalidate pending approvals or encrypted records.

## Verification and containment (pilot check 6)

Static checks, from this directory, without live credentials:

```sh
docker compose --env-file .env.example config -q
shellcheck bootstrap.sh block-metadata.sh backup.sh restore.sh upgrade.sh health.sh join-tailnet.sh sfctl container/*.sh tests/e2e-backup-restore.sh
python3 -B -m unittest discover -s tests
# From repository root, after staging only the kit:
git diff --cached -- hosting/paperclip-hermes/kit | gitleaks stdin --redact
```

The static check reads the committed non-secret example directly; bootstrap
refuses to deploy it. Live commands use the root-only `.secrets/compose.env`
explicitly, including `sudo docker compose --env-file .secrets/compose.env config -q`.
Do not keep a second host `.env` in the checkout.
CI is the authority. A Windows Docker CLI can parse Compose without an engine;
the build, second-run/reboot behavior and the following checks require Linux.
The credential/firewall integration tests require a POSIX shell and Python 3.
The Hermes startup regression requires `ruamel.yaml==0.18.16`, matching the
pinned image; a missing dependency is reported as a skipped test.

`bash ci/check.sh` from the repository root runs the repository checks and all
kit unit tests, including profile-matrix Compose rendering, secret isolation, SAS
rejection, rollback failure recovery and lock exclusion. Docker Compose v2 or
later is required for configuration rendering; no daemon is needed for unit tests.

On the live host, run `sudo ./health.sh` and `sudo ./sfctl status`, then record
output and date. The health summary supplements the explicit probes below.
Application-specific probes require their profile; the gateway and simultaneous
pilot checks require both profiles. Record unselected applications as not tested.
Never print `.secrets/compose.env`, tokens or full container environments. First check listeners, rules and mounts:

```sh
sudo ss -lntp
sudo iptables -S DOCKER-USER
sudo systemctl cat docker.service containerd.service
sudo docker compose --env-file .secrets/compose.env ps
for service in $(sudo docker compose --env-file .secrets/compose.env config --services); do
  sudo docker inspect --format '{{json .Mounts}}' "$(sudo docker compose --env-file .secrets/compose.env ps -q "$service")"
done
```

Test all three metadata/host-agent endpoints from **every container's network
namespace**. Only a timeout or no route passes: a refused or completed TCP
connection, or any HTTP response (including 400/401/403), fails. Missing tools and other errors
also fail. This uses the Ubuntu host's Python and `nsenter` (util-linux),
entering only the container's network namespace. It tests the same forwarded
traffic path without depending on diagnostic tools in the service images.

```sh
set -eu
for service in $(sudo docker compose --env-file .secrets/compose.env config --services); do
  container=$(sudo docker compose --env-file .secrets/compose.env ps -q "$service")
  pid=$(sudo docker inspect --format '{{.State.Pid}}' "$container")
  test "$pid" -gt 0
  sudo nsenter --target "$pid" --net python3 - "$service" <<'PY'
import errno
import socket
import sys
# A DROP rule shows as a timeout; containers on internal-only networks have no
# route at all. A refusal means something answered, so it fails the check.
for host, port in [('169.254.169.254', 80), ('168.63.129.16', 80), ('168.63.129.16', 32526)]:
    try:
        socket.create_connection((host, port), timeout=3).close()
    except TimeoutError:
        print(sys.argv[1], host, port, 'blocked (timeout)')
    except OSError as error:
        if error.errno in (errno.ENETUNREACH, errno.EHOSTUNREACH):
            print(sys.argv[1], host, port, 'blocked (no route)')
        else:
            raise SystemExit(f'FAIL: {sys.argv[1]} {host}:{port}: {error}')
    else:
        raise SystemExit(f'FAIL: {sys.argv[1]} can reach {host}:{port}')
PY
done
# Check all three DROP counters increased. Port 53 must have no kit DROP rule.
sudo iptables -vnL DOCKER-USER
```

After installing the backup job below, use its fixed host-only settings path.
The host existence check must succeed, then every container read must fail:

```sh
BACKUP_ENV_FILE=/etc/paperclip-hermes/backup.env
sudo test -s "$BACKUP_ENV_FILE"
sudo stat -c '%U %a %n' "$BACKUP_ENV_FILE"       # root 600
for service in $(sudo docker compose --env-file .secrets/compose.env config --services); do
  sudo docker compose --env-file .secrets/compose.env exec -T "$service" sh -c 'test ! -r "$1"' sh "$BACKUP_ENV_FILE"
done
```

In a **real Hermes terminal tool turn**, execute the following, substituting the
same backup path. Running it with `docker exec hermes` tests the gateway instead
of the execution boundary and is not evidence of a pass.

```sh
id
pwd                            # /workspace (or a checkout below it)
test ! -r /opt/data/.env
test ! -r /opt/data/auth.json
test ! -r /etc/paperclip-hermes/backup.env
test ! -S /var/run/docker.sock
env | cut -d= -f1 | grep -E '^(OPENAI_API_KEY|API_SERVER_KEY|HERMES_DASHBOARD_BASIC_AUTH_PASSWORD)$'
# grep MUST find no variable; its exit 1 is expected here.
git ls-remote https://github.com/OWNER/FIXTURE.git HEAD
gh api repos/OWNER/FIXTURE --jq .full_name
```

Inspect mounts above for **any alternative path** to the backup file or Hermes
home; a failed read of one spelling alone is not sufficient. Do not add mount
access or environment forwarding to make a tool pass. An absent backup job is
"not yet tested", never a containment pass.

From an actual Paperclip coding run with the pilot's GitHub connection selected
(and its configured Git author identity), verify broker mode and push a new
fixture branch. Substitute the fixture owner/repository; keep the branch for
the operator to inspect and later remove. Record the run ID and pushed SHA.
Never print the token.

```sh
set -eu
test "$GIT_CONFIG_SYSTEM" = /dev/null
test "$GIT_CONFIG_NOSYSTEM" = 1
# The broker leaves GH_TOKEN empty in the run environment and injects it only
# into the gh and git it launches; the gh call and the push prove credentials.
test -z "${GH_TOKEN:-}"
gh api repos/OWNER/FIXTURE --jq .full_name
checkout=$(mktemp -d)
git clone https://github.com/OWNER/FIXTURE.git "$checkout"
cd "$checkout"
branch="pilot/credential-check-$(date -u +%Y%m%dT%H%M%SZ)"
git switch -c "$branch"
git commit --allow-empty -m "chore: verify pilot broker credentials"
git push origin "HEAD:refs/heads/$branch"
git ls-remote origin "refs/heads/$branch"
git rev-parse HEAD
```

A successful push must create that branch with the same SHA. Also run the
Hermes read checks above in a real tool turn to cover the kit helper/wrapper.

On the host, check the internal API as Paperclip's runtime uid 1000, then
verify internal TLS both positively and negatively from Paperclip:

```sh
sudo docker compose --env-file .secrets/compose.env exec -T --user 1000:1000 paperclip node -e '
 if(process.env.PAPERCLIP_API_URL!=="http://127.0.0.1:3100")process.exit(1);
 fetch(process.env.PAPERCLIP_API_URL+"/api/health", {
   headers:{host:process.env.PAPERCLIP_ALLOWED_HOSTNAMES}
 }).then(r=>{if(!r.ok)throw Error("health status "+r.status);console.log("internal API healthy")})
 .catch(e=>{console.error(e.message);process.exit(1)});'
sudo docker compose --env-file .secrets/compose.env exec -T --user 1000:1000 paperclip node -e '
 fetch("https://caddy/v1/models").then(r=>{
   if(r.status!==401)throw Error("expected gateway auth rejection, got "+r.status);
   console.log("TLS verified; gateway requires bearer auth");
 }).catch(e=>{console.error(e.message);process.exit(1)});'
sudo docker compose --env-file .secrets/compose.env exec -T --user 1000:1000 paperclip env -u NODE_EXTRA_CA_CERTS node -e '
 fetch("https://caddy/v1/models").then(()=>process.exit(1)).catch(e=>{
   if(!["UNABLE_TO_VERIFY_LEAF_SIGNATURE","SELF_SIGNED_CERT_IN_CHAIN","UNABLE_TO_GET_ISSUER_CERT_LOCALLY"].includes(e.cause?.code)){
     console.error(e.cause?.code);process.exit(1);
   } console.log("untrusted CA correctly rejected");
 });'
```

Record the accepted sandbox-network exposure from the SSH sandbox: both TCP
connections below currently succeed. Do not send a bearer key or dashboard
password over this HTTP path. Also verify the dashboard still loads through
its private Serve URL; neither raw port may appear on the host's public bind.

```sh
sudo docker compose --env-file .secrets/compose.env exec -T sandbox /opt/hermes/.venv/bin/python - <<'PY'
import socket
for port in (8642, 9119):
    socket.create_connection(('hermes', port), timeout=3).close()
    print('accepted pilot exposure: sandbox -> hermes', port)
PY
```

Then submit a small task through the configured `hermes_gateway` agent and
record its Paperclip/Hermes run IDs, demonstrating bearer-authenticated traffic
over that verified TLS route. From a machine outside the tailnet, run
`nmap -Pn -p- --open "$PUBLIC_IPV4"`; no open port is the pass condition.
Repeat check 6 after a reboot, checking the DROP rule before starting work.
Stop the pilot if containment or fixture least privilege fails.

## Capacity and interrupted runs (pilot checks 3 and 7)

During a simultaneous fixture build and Hermes task, capture:

```sh
sudo docker stats --no-stream
free -b
df -h / /data
sudo journalctl -k --since today | grep -Ei 'oom|out of memory|killed process' || true
```

Record peak memory and disk usage during the run, plus the host provider's CPU
credit balance before/after: no OOM, memory below 85%, disks below 70%, and CPU
credits above the pilot's agreed floor. A single `docker stats` sample is not a
peak measurement. Provider metrics collection stays with the operator.

For check 7, start a task, record both run IDs, branch head and PR, then:

```sh
sudo docker compose --env-file .secrets/compose.env restart
sudo docker compose --env-file .secrets/compose.env ps
sudo docker compose --env-file .secrets/compose.env logs --since 10m --tail 200 paperclip hermes
```

Reconcile those same IDs, the branch and PR in the dashboards; there must be no
duplicate PR or second Hermes run for the same Paperclip run. Separately let
the operator's scheduled host shutdown interrupt a run, start the host again,
and reconcile the same records. Docker restart policies preserve services, not
a guarantee of application-level exactly-once recovery. Record a failure if a
run cannot be reconciled; do not silently resubmit it.

## Nightly backup and fenced restore (pilot check 8)

The host job briefly stops the selected application writers (Paperclip, Hermes,
the tool sandbox and Caddy when installed), leaving PostgreSQL running when the
Paperclip profile is selected. Hermes-only skips the database; Paperclip-only
skips the Hermes CLI and home payload. With both profiles selected it takes a
custom-format pg_dump, the entire /paperclip home (including instructions, uploads and retained logs), the pinned Hermes full ZIP
backup, Caddy data/config/public CA and sandbox SSH host keys. It also packs this
kit's authoritative configuration, original non-secret Compose settings, all
separate application secret files, fixture token and sandbox SSH identity.
These secrets stay encrypted in the uploaded recovery set. The Docker engine store, host OS and sandbox workspaces are not
copied. There is no promise that an interrupted external task completed safely.
Schedule the pause before host shutdown and reconcile interrupted work as in
check 7; an active job may be interrupted by the nightly backup too.

The manifest records UTC capture start/end, the kit's git commit, every service's image ID and registry
RepoDigests (local Hermes builds may have no RepoDigest), the full applied
Paperclip migration journal and SHA-256 hashes of every payload. The encrypted
configuration retains the pinned upstream references. Locally built Hermes
images include distribution packages; an image ID records the build but does
not make its layers recoverable. Retain images separately if byte-identical
rebuilds are required. Restore checks that this kit's image pins equal the
manifest's and that the restored journal equals the captured journal before
starting applications.

**Restore trusts only data and secrets from the blob.** age gives
confidentiality, not authenticity, and the manifest travels in the same
payload. Restore therefore takes every script, Compose file, Dockerfile,
container helper, unit and the Hermes startup policy `hermes-config.yaml` from
the kit checkout running it, and refuses an archive whose copies of those differ
byte for byte: check out the kit commit that made the backup. The manifest
records that commit (`git rev-parse HEAD` of the kit, or `unknown` when the kit
has no trusted commit provenance) and the refusal message prints it; record it
in the pilot log as well and update it on every kit update. A restored install
records the trusted restoring checkout's commit and code hashes in root-only
`.kit-provenance.json`, so later upgrades and rollback can resolve that release
without Git metadata in the restored tree. Recovery copies the restored secrets
into the previous checkout before activating it. From the archive
restore takes only `.secrets/*` and the volume, database and Hermes home
payloads. As defence in depth, the policy is checked against a baseline fixed
in `container/validate-hermes-config.py` (only the model, terminal and dashboard
sections; the OpenAI provider and URL; the SSH sandbox terminal without
environment passthrough; only the dashboard keys the kit sets) with the pinned
image's YAML parser before any service reads it, and again at every start.

**The restored Hermes home is trusted data.** Its skills, memory, sessions,
profiles and their configuration come from the hermes.zip inside the blob and
run inside the Hermes container (its root config.yaml is rewritten from this
kit's policy at every start; named profiles keep theirs, with every messaging
platform switched off until you turn messaging back on). Nothing checks their content, so restore only a blob
you expect: confirm its name and time against the nightly runs. That trust
rests on the container's time-based immutability policy and the host holding a
Create/Write-only SAS that can add blobs but not read, list or replace them.

**Backups are encrypted, not signed.** The manifest's hashes prove the payloads
match each other, not who made them: anyone with the public age recipient can
encrypt a bundle, and whoever holds the host's SAS (root on the VM) can add one
under a new name. A signature would not help, because its key would sit on the
same VM. The volume archives (Paperclip's instance, Caddy's CA and keys, the
sandbox host keys) are extracted as root with their recorded owners, so a
forged blob is a full compromise of the restored stack. Restore only a blob
whose name and time match your own record of a nightly run, made before any
suspected compromise of the VM.

The following additional publication and stop semantics apply to the same
recovery contract:

`backup.sh --reason nightly --target <target> --vm-id <vm-resource-id>` encrypts
a consistent data/configuration bundle to the public recipient. No private
long-term key goes on the host. Only installed writers stop; PostgreSQL remains
running for its dump. Hermes-only skips the absent database; Paperclip-only skips
the Hermes CLI. Writers restart after ordinary backups, including failure paths.

`--canary` uploads encrypted empty content to prove the upload credential, without
stopping writers or advancing the successful data-backup clock. Every archive
upload has non-secret metadata: kit commit, image-pin and schema digests with counts, recipient fingerprint,
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

### Install the host job

Install age on the dedicated Ubuntu host:

~~~sh
sudo apt-get update
sudo apt-get install -y age
sudo install -d -m 0700 -o root -g root /etc/paperclip-hermes
# On the host, obtain only its public delivery recipient:
sudo ./sfctl delivery-recipient
~~~

Generate the age identity on an operator device and keep it in the operator's
password manager. Prepare a private copy of `backup.env.example` on the operator
device. Put only the public age1 recipient into BACKUP_AGE_RECIPIENT. Never store
the long-term private identity on the backup host. Set BACKUP_CONTAINER_SAS_URL
to the full HTTPS container user-delegation SAS URL, as a literal unquoted value.
Encrypt the settings to the host's public delivery recipient before sending them
through the approved operation to `sfctl set-backup-credential` on stdin. That
command validates the replacement before atomically installing host-only
`/etc/paperclip-hermes/backup.env`; it accepts ciphertext only. Do not source
this file, export its settings, put the URL in a command argument or enable shell
tracing. This path is separate from .secrets/compose.env and mounted into no
container. The script checks that it is outside the kit and outside every
service's mount sources. Keep both files at their documented paths.

For encrypted delivery from the operator device, use the public recipient
returned above and a private settings file; no plaintext secret travels in argv:

```sh
age --encrypt --recipient "$HOST_DELIVERY_RECIPIENT" --output backup.env.age backup.env
cat backup.env.age | tailscale ssh admin@host sudo /opt/software-factory/current/sfctl set-backup-credential
```

The same host-only file can still be installed manually in a private root
session with the original commands below. Use one installation method per
credential update; `sfctl set-backup-credential` always requires ciphertext,
while these commands edit the root-only file directly:

~~~sh
sudo install -m 0600 -o root -g root backup.env.example /etc/paperclip-hermes/backup.env
sudo vi /etc/paperclip-hermes/backup.env
~~~

The approved deployment creates the destination in a separate failure boundary
with versioning and time-based immutability. Keep Shared Key off. The validator
accepts only a container-scoped **user-delegation SAS**, with `skoid`, `sktid`,
`skt`, `ske`, `sks=b`, `skv`, `se`, `sr=c`, `spr=https` and `sp=cw`.
Write permission is required for the pinned block-upload API version. Both expiries must be valid and at most seven days
away, and the SAS cannot outlive its delegation key; the key's lifetime must not
exceed seven days. Service, account, stored-policy, duplicate-field, expired and
broader-permission forms are rejected locally. Read, List and Delete are never
host permissions. No Shared Key fallback exists. Renew the credential before
expiry through encrypted delivery; these scripts create no resource or policy.

Set `timezone` and `shutdown_time` in root-owned 0600
`/etc/paperclip-hermes/config.json` to match the VM's shutdown schedule, and set
its non-secret `target` and full Azure `vm_id` for unattended receipts. The
defaults are `Etc/UTC` and `22:00`; the backup timer is rendered 45 minutes before
shutdown (21:15 with those defaults). Bootstrap applies the configured timezone;
verify it with the host checks:

~~~sh
sudo timedatectl set-timezone Europe/London   # use config.json's zone if different
timedatectl                                   # Time zone: line must match
~~~

Keep `config.json` authoritative: a later bootstrap or `sfctl configure-host`
reapplies its timezone and re-renders the timer.

Run one backup interactively before enabling the timer:

~~~sh
sudo bash backup.sh
~~~

Only the resulting unique blob name is printed. Every run uses a UTC timestamp
and random UUID. The uploader sends HTTPS PUT requests, refuses redirects and
conditional overwrites, and treats any unexpected status as failure. It never
passes the SAS into Docker, a child-process argument/environment or logs.
No read/list/delete is attempted. Check blob existence using an operator's
separate read credential, not the host's SAS.

`sudo ./sfctl configure-host` installs the supplied units, renders the timer
from `config.json` and enables it only with validated `backup.env`; prepare-only
leaves it disabled. The service uses `/opt/software-factory/current` for both
WorkingDirectory and ExecStart. If installing manually, set both in the
**installed** service to the **absolute checkout path** and render the timer
after installing the supplied default, before enabling it. Edit only the installed
copy and keep the kit checkout unmodified: restore compares the kit's code byte
for byte with the checkout of the recorded commit, and the manifest's `kit_modified` flag
records local changes under the kit directory (files elsewhere in the clone
do not count). Restore does not compare or install the units.

~~~sh
sudo install -m 0644 systemd/paperclip-hermes-backup.service /etc/systemd/system/
sudo install -m 0644 systemd/paperclip-hermes-backup.timer /etc/systemd/system/
sudo ./sfctl configure-host
sudo systemctl edit --full paperclip-hermes-backup.service
sudo systemctl daemon-reload
sudo systemctl enable --now paperclip-hermes-backup.timer
sudo systemctl list-timers paperclip-hermes-backup.timer
sudo journalctl -u paperclip-hermes-backup.service --since today
~~~

The timer runs 45 minutes before the configured shutdown with
`Persistent=false`; it does not catch up at boot. The future workflow start path
must renew credentials and explicitly run its catch-up backup, which pauses the
selected application writers. After starting such a backup, wait for it to finish
before starting work: `systemctl status paperclip-hermes-backup.service` must
show it inactive with a successful result (not "activating"). A nonzero service
result means no successful backup; monitor it and inspect container health.
To re-run a failed backup, wait until the stack is healthy (`docker
compose --env-file .secrets/compose.env ps` shows every selected service running and
healthy), then run `sudo systemctl start paperclip-hermes-backup.service` and
confirm `systemctl status paperclip-hermes-backup.service` shows a successful
result. The job only waits for running services and never starts one, so do not
start services you stopped on purpose just to make it pass; it names any
service that is not running.
The host-level `/run/paperclip-hermes/op.lock`, in a root-only directory,
prevents overlapping backup, restore, upgrade and `sfctl` mutations. Contention
refuses the operation. Staging is root-only under /var/tmp by default; --work-dir
selects a larger host encrypted filesystem. Allow room for the database,
application archives and ciphertext. Restore needs about twice the
unencrypted backup size in its work directory (the decrypted bundle and the
unpacked payloads exist together), plus the restored volumes on the data disk.
A restore that fails before any container is created empties its target
directory; one that fails later removes the partial project's containers
(including one-off `run` containers a signal left running) and keeps its
named volumes and target for inspection, so delete those (or pick a new
project name and target) before retrying. During backup capture, normal errors
and SIGTERM, SIGINT, SIGHUP or SIGQUIT remove any one-off Hermes CLI container
and delete staged plaintext first, then restart the originally running writers;
further signals are ignored until cleanup ends. Ordinary backups restart writers
after capture, before encryption/upload, so upload failures leave them running.
Successful pre-stop/final backups keep writers stopped through publication;
a failure after stopping them restarts them with a health wait. Restarts use
`compose up --no-recreate --no-build --pull never --wait`, which also works
with Compose 2.38 (whose `start` command has no `--wait` option). Restore failures
instead remove the partial project's containers as described above. Transient
upload failures are retried four times with backoff; a retried conditional PUT that finds the blob already present fails as uncertain, so check
the container with a separate read credential. A hard kill, power loss or disk
failure cannot run cleanup: the next backup or restore removes root-owned
paperclip-hermes-recovery-* remnants under its work directory, under the lock.
Check services before resuming work.
Do not point staging or the local upload destination inside the kit or a service
volume. No plaintext aggregate backup is written during encryption; payload
files and restore plaintext exist only in private staging until cleanup.

### Restore on a prepared fresh host

Fence the original host first, including its restart policies and scheduled
starts. For a VM drill, verify it is deallocated and cannot be started by a
schedule. **--fenced is an operator assertion, not a remote fencing mechanism.**
It is required before decryption, volume creation or any scheduler start.
Keep the original host fenced until deciding which host is authoritative.

Prepare the new host's mounted disk, firewall, Docker roots and Tailscale using
this trusted kit, checked out at the commit that made the backup (the commit in
the pilot log, or the one a refused restore prints). Format and mount the new
data disk and add it to /etc/fstab as in step 1 of
[Prepare the dedicated host](#prepare-the-dedicated-host). The preparation mode
needs no application credentials, installs age, and starts no Compose service:

~~~sh
sudo bash bootstrap.sh --prepare-only /data
~~~

Do not run a normal bootstrap on the restore destination first: restore requires
an empty target directory and a Compose project with no existing containers,
networks or volumes. Use a different project name only for a deliberately
separate drill. Download the newest encrypted blob with an operator-supplied,
one-day Read/List user-delegation SAS on the operator device. The read SAS never
reaches the host, the scripts or any container mount.

Set RESTORE_HOSTNAME to the destination host's actual MagicDNS FQDN in your private shell.
Generate a one-use restore identity on the host. The command returns only its
public recipient and refuses to overwrite an existing restore key:

~~~sh
sudo ./sfctl restore-key-ephemeral
~~~

On the operator device, decrypt the selected backup using the long-term identity
and re-encrypt it to that ephemeral recipient. The selected blob's recorded kit
commit still determines the trusted restore checkout. Never supply the long-term
identity to the host or to `restore.sh`. In the operator's private Bash session,
set the identity file path, selected ciphertext path and public restore recipient:

~~~sh
set -o pipefail
age --decrypt --identity "$BACKUP_IDENTITY" "$SELECTED_BACKUP" | \
  age --encrypt --recipient "$RESTORE_RECIPIENT" --output paperclip-hermes-staged.tar.age
~~~

Copy only that staged ciphertext to the fresh host with `scp` over your Tailscale
SSH access (`tailscale file cp` does not work: a tagged node is not a file-sharing
target without a separate grant), and install it root-only. Substitute the staged
ciphertext's filename in the transfer command:

~~~sh
# On the operator device:
scp paperclip-hermes-<timestamp>-<id>.tar.age <admin user>@<restore host>:recovery.tar.age
# On the restore host:
sudo install -m 0600 -o root -g root ~/recovery.tar.age /var/tmp/recovery.tar.age
rm ~/recovery.tar.age
~~~

~~~sh
# The ciphertext below has been re-encrypted to the ephemeral recipient.
sudo bash restore.sh --fenced --archive /var/tmp/recovery.tar.age \
  --identity /run/paperclip-hermes/restore.key --staged \
  --target /opt/software-factory/restored --hostname "$RESTORE_HOSTNAME" \
  --work-dir /data
sudo rm -f -- /run/paperclip-hermes/restore.key
~~~

`--staged` requires the identity under `/run` and removes it on success or failure.
`--verify-only` decrypts and validates the manifest and every payload hash without
creating a Compose project or starting any service; it still requires `--fenced`
and the required target argument. A verification with `--staged` consumes the
identity too, so generate a new ephemeral identity and re-encrypt again before a
subsequent full restore. Local offline drills use throwaway identities and may
omit `--staged`.

The target must not exist yet (it is created root-only, mode 0700) or be an
empty root-owned 0700 directory, and every parent must be root-owned and not
group- or other-writable (so not under /var/tmp). Replace the example destination
hostname with the new host's actual MagicDNS FQDN. The hostname is the only Compose setting changed by the restore option
(restore also adds `KIT_MESSAGING_OFF=1`, below);
all signing secrets, agent JWT secret, master key, database password, gateway
key and dashboard authentication values remain original. The destination mount
guard comes from the preparation host, never from the backup. Reconfigure
Tailscale Serve to the new host as in the setup section, and redo sign-ins.

Restore decrypts and validates the complete bundle before creating application
volumes. It refuses an existing project or nonempty destination volume, restores
the database with stop-on-error checks, imports Hermes with --force --keep-host
--messaging-off and starts the stack only after the journal matches. Hermes's
best-effort post-import service revival is suppressed in its one-shot container.
Database restore commits statements separately to stay within PostgreSQL's
lock budget for Paperclip's schema. A failed restore leaves applications stopped;
partial data is never started.
The temporary rollback ZIP made by --keep-host is removed on success or failure.
The root config.yaml is rewritten from this kit's hermes-config.yaml at every
start, so no archived setting reaches it. That alone would not keep messaging
off: with no `enabled` switch in the config, Hermes starts a bot from any
credential it finds in the environment, such as a token in the restored home's
`.op.env`, and import's --messaging-off covers only bots whose token is in a
profile's `.env`. Restore therefore adds `KIT_MESSAGING_OFF=1` to
`.secrets/compose.env`. While it is set, every start writes
`platforms.<id>.enabled: false` for every messaging platform Hermes knows into
the root config.yaml and every named profile's config.yaml; a profile also gets
the platforms of plugins installed only in it (listed in its own plugin scope)
and every platform that already has a block in its config (an explicit disable
that environment credentials cannot override), so the restored agent answers on
no platform beside the source host, however often it restarts. Tokens and other
settings stay in place. There is no automatic messaging cutover: once the source
host's bots are off, remove the `KIT_MESSAGING_OFF=1` line from
`.secrets/compose.env`, run `docker compose --env-file .secrets/compose.env up -d
hermes`, and switch each named profile's bots back on on its Messaging page (the
root's bots start again from their credentials). A failure removes staging and
takes down the partial project's containers, one-off `run` containers
included, and their anonymous volumes without deleting its named volumes.
Inspect those volumes deliberately; a retry must use a fresh project and empty
kit directory.

Let the one-day read grant expire or revoke its user-delegation key when needed,
and remove any remaining ephemeral runtime identity after the drill,
including on failure. Leave the nightly timer disabled on the drill host; neither
its units nor the upload SAS are installed/restored automatically. On the chosen
replacement host, install the timer and supply a new host-only upload SAS through
the installation steps above. No stored SAS or private age identity is in the
recovery archive.

### What is not recovered

- OAuth logins must be redone, including actual Paperclip Runner/provider sessions.
  Saved credentials in the filesystem are not evidence of a valid external login.
- Unpushed worktrees in the SSH sandbox, external workspaces and changes made
  after the recovery point are not recovered. The captured Paperclip home may
  contain local checkouts, but does not guarantee their external paths or
  in-flight work can be resumed. Push work before the pause when practical.
- Browser profiles and browser credentials, excluded Hermes caches/checkpoints,
  downloaded runtimes and tools must be recreated. Hermes backup omits symlinks.
- Host OS/packages, Docker image layers, /etc/fstab, tailnet enrolment, cloud
  resources, external storage/services and provider-hosted sessions are outside
  this recovery set. Prepare a new host; never copy a tailnet node identity blindly.

For pilot check 8, record the recovery point and elapsed restore time. Verify the
known issue, attachment, pending approval signature, agent instruction, Hermes
memory/session, original secrets and CA. Reconcile both applications' in-flight
run IDs, branch head and PR before submitting anything. Then use the operator's
fixture to show one new task produces exactly one PR. Local verification below
covers durable recovery mechanics, subject to its stated profile limits; it
cannot establish those external effects or the pending-approval and attachment
application checks.

### Local verification without external credentials

On a disposable Linux Docker host with enough disk/memory, run from this kit:

~~~sh
shellcheck backup.sh restore.sh bootstrap.sh block-metadata.sh upgrade.sh health.sh join-tailnet.sh sfctl container/*.sh tests/e2e-backup-restore.sh
python3 -B -m unittest discover -s tests
# From the repository root, run the WP3 container acceptance suite:
(cd ../../.. && sudo env "PATH=$PATH" bash ci/kit-e2e.sh)
~~~

The WP3 acceptance suite runs a real upgrade with forced candidate health failure
and data rollback, a failing dump that must leave the old stack healthy, and a
backup under a previous fixture commit that is rejected by a different commit
and then restored through the backup's own commit. It also checks Tini signal
forwarding, fences a candidate-only writer sharing an original volume, and
upgrades and rolls back a restored install through a fresh previous checkout.
The weekly and PR `kit-e2e`
workflow runs these on a Docker-capable Ubuntu runner. The fixture releases use
the pinned Paperclip/PostgreSQL/Caddy stack without cloud, tailnet, GitHub or
model credentials. Hermes-only and combined profile sequencing are covered by
unit tests; live Hermes and cloud control probes remain deployment release gates
in the ADR. The suite requires root and a disposable host with no live
`paperclip-hermes` project; it creates and deletes that project and its volumes.

The legacy `tests/e2e-backup-restore.sh` combined-profile drill needs its fixture
migrated to WP3's separate application secret files before it can run again;
its former direct invocation is not a passing WP3 acceptance command. Its
remaining coverage must still be verified for the combined pilot: the drill
creates throwaway credentials and an age key, backs up to a private
local directory, removes the source project's containers to fence it, and
restores into a second Compose project using only the ciphertext. It checks a
known Paperclip issue, Hermes session/memory, agent instruction, original secrets,
CA and messaging switched off in the root and a named profile. It publishes no host ports and makes no
model call. Images/build dependencies still need registry/package network access.
The dedicated test dashboard subnet is 172.29.245.0/24; use a disposable host
where it is free. The test removes its two projects, volumes and temporary files
on exit; KEEP_DRILL=1 retains them for inspection. Never run it against a live
kit project or supply real credentials. These legacy checks describe the
combined recovery acceptance target; the current Paperclip-only WP3 suite does
not establish the Hermes memory/session or named-profile checks.

Pinned source contracts and limitations are recorded in
[recovery-sources.md](recovery-sources.md).

## Upgrade recovery

The deployment workflow must complete the fresh off-site backup and Terraform
apply before calling `upgrade.sh <approved-workflow-sha> --run-id <id>`.
`backup.sh`, `restore.sh`, `upgrade.sh` and all `sfctl` mutations share the root-only
`/run/paperclip-hermes/op.lock`; contention refuses the operation.
Run IDs must contain 1–255 ASCII letters, digits, underscores or hyphens. An
existing rollback path (including a symlink) is rejected before changing the
journal; use a fresh ID for every upgrade attempt, including after a rollback.

The upgrade refuses unless available space is at least 2.2 times the sum of
volume bytes and the database dump estimate. It then creates `/data/rollback/<run>`
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

For an approved manual recovery of an interrupted upgrade, the same protocol
is exposed by these entrypoints (the journal identifies the previous commit and
rollback point):

```sh
sudo ./sfctl upgrade "$APPROVED_KIT_SHA" --run-id "$RUN_ID"
# Equivalent upgrade entrypoint:
sudo ./upgrade.sh "$APPROVED_KIT_SHA" --run-id "$RUN_ID"
# Recovery uses the journal; --point can select its retained rollback point:
sudo ./upgrade.sh recover --point "/data/rollback/$RUN_ID"
# Equivalent journal-based rollback:
sudo ./sfctl rollback
sudo /opt/software-factory/current/health.sh
sudo /opt/software-factory/current/sfctl status
```

A local rollback point contains unencrypted database and volume state. Keep it
root-only and retain the independent off-site backup; it does not replace the
fenced fresh-host restore contract above.

## Sources

- Paperclip [tagged deployment environment](https://github.com/paperclipai/paperclip/blob/v2026.1117.0/docs/deploy/environment-variables.md),
  [Docker guide](https://github.com/paperclipai/paperclip/blob/v2026.1117.0/docs/deploy/docker.md),
  [storage](https://github.com/paperclipai/paperclip/blob/v2026.1117.0/docs/deploy/storage.md),
  [GitHub credential selection](https://github.com/paperclipai/paperclip/blob/v2026.1117.0/server/src/services/heartbeat/execution-configuration.ts),
  [broker Git environment](https://github.com/paperclipai/paperclip/blob/v2026.1117.0/packages/adapter-utils/src/github-launcher.ts),
  and [trusted-origin derivation](https://github.com/paperclipai/paperclip/blob/v2026.1117.0/server/src/auth/better-auth.ts).
- Hermes [v0.23.4 Docker guide](https://github.com/NousResearch/hermes-agent/blob/v0.23.4/website/docs/user-guide/docker.md),
  [SSH backend/configuration](https://github.com/NousResearch/hermes-agent/blob/v0.23.4/website/docs/user-guide/configuration.md)
  and [security guide](https://github.com/NousResearch/hermes-agent/blob/v0.23.4/website/docs/user-guide/security.md).
- Docker [Ubuntu installation](https://docs.docker.com/engine/install/ubuntu/),
  [daemon data directory](https://docs.docker.com/engine/daemon/#daemon-data-directory),
  [iptables user rules](https://docs.docker.com/engine/network/firewall-iptables/)
  and [local logging](https://docs.docker.com/engine/logging/drivers/local/).
- Caddy [internal TLS](https://caddyserver.com/docs/caddyfile/directives/tls#internal),
  Tailscale [Serve](https://tailscale.com/kb/1242/tailscale-serve) and
  [policy syntax](https://tailscale.com/kb/1337/policy-syntax).
