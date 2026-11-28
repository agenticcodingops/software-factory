# Fixed host bootstrap agent

Cloud-init installs `sf-host-agent` at `/usr/local/sbin/sf-host-agent`. It prepares
storage and generates the host delivery identity, without fetching a release.
The agent never creates or replaces the delivery key. It needs Python 3 and the
image's Git, Bash and cloud-init, plus the two pinned packages in the template.
There is no configuration file or repository input: the fixed source is
`https://github.com/agenticcodingops/software-factory.git`, shared with
`kit-control.py`. Changing either constant requires reviewing both fetch paths.

## Machine contract

[contract.json](contract.json) records the agent file's SHA-256, argv shapes,
stdout formats, lock behavior, exit codes, runtime outcomes and runtime bounds. The SHA argument
uses exactly `^[0-9a-f]{40}$`; matching uses fullmatch, including at end of input.
The agent uses only Python's standard library and runs with isolated Python.
Root ownership and non-writable-by-others checks cover the agent, its directory,
interpreter, tools and tool directories. Distribution interpreter symlinks have
their owner and resolved executable checked. Linux symlink mode bits have no
access-control meaning; tracked links must resolve within the checkout and their
targets and parent directories must pass ownership/mode checks.

| Command | Lock | Standard output |
| --- | --- | --- |
| `recipient` | Nonblocking exclusive | One native age recipient line |
| `fetch <sha> [--prepare-only]` | Nonblocking exclusive | JSON with `fetched`, `path`; `prepared: true` after preparation |
| `status` | None, read-only | One JSON object, at most 2048 bytes including newline |

| Exit | Meaning | Runtime outcome |
| --- | --- | --- |
| 0 | Success | success |
| 1 | Internal error, generic stderr only | agent-unavailable |
| 2 | Usage or invalid SHA | invalid-request |
| 3 | Missing or unsafe delivery key, or recipient conversion failure | agent-unavailable |
| 4 | Checkout, release or fetch verification failure | verification-failed |
| 6 | Data disk is not mounted | recovery-required |
| 7 | Preparation failed or timed out | prepare-failed |
| 8 | Lock busy | retry-later |
| 10 | Not root, or unsafe executable/directory | unsafe-host |

The agent never returns 127. The `runtime_outcome` map also defines 127 (absent
agent) and unknown exits as **agent-unavailable**. Consumers stop on
invalid-request, verification-failed and prepare-failed; they do not continue
to deliveries or activation. Recovery-required routes to approved restore or
operator recovery; unsafe-host stops and alerts. Retry-later permits a bounded
retry. A failed recipient conversion (exit 3) means agent-unavailable; no failed
invocation may pin a recipient.
A status `cloud_init` other than `done` means not ready. Compare
status `agent_sha256` with the contract before invoking the agent for deployment.
`cloud-init status --format json` is parsed even on exit 1 or 2: degraded, error
and disabled are errors; running and not started are running; invalid output or
a timeout is unknown. See the [cloud-init CLI reference](https://cloudinit.readthedocs.io/en/latest/reference/cli.html#status).

Set managed Run Command `timeoutInSeconds` above the contract maximum: recipient
60 seconds, fetch 4200 seconds and status 60 seconds. Each local subprocess has
a 30-second timeout, network Git 120 seconds and preparation 3600 seconds.
The [managed Run Command reference](https://learn.microsoft.com/en-us/azure/virtual-machines/linux/run-command-managed)
describes root execution, instanceView exitCode, configurable timeouts and the
last 4 KB of output. P20 must probe root execution, absent-file exit 127 and
umask on the actual image. The agent sets umask 022 during checkout and restores
it; each state file is 0600. No subprocess diagnostic or key content enters the
status or journal. Status reports at most eight sorted checkout SHAs and a
`truncated` marker, a validated projection of `last.json`, the mount and key
safety, cloud-init readiness and the agent's descriptor-read file digest.
Status reports `tools` as an executable-name map with `ok` or `missing` values.
Missing package tools do not prevent polling during boot or after failed package
installation: status exits 0, and missing cloud-init yields `unknown`. An existing
unsafe tool still causes exit 10. Recipient requires age-keygen; fetch requires
Git and adds Bash for preparation. All commands check the agent and interpreter.

Preparation runs in a new session. On timeout the agent signals the process
group with TERM, then KILL, and waits for Bash so children cannot retain the kit
lock. See [Python subprocess](https://docs.python.org/3/library/subprocess.html).
If preparation and activation succeeded but journaling fails, fetch preserves
exit 0 and the success JSON and adds `journal_error: true`. The runtime must
surface that diagnostic and use status to reconcile `current`; it must not
repeat successful preparation just to repair the journal. `last` can be stale.

## Runtime ordering and receipt preservation

1. Confirm through ARM that no VM exists and record that observation.
2. Plan and apply with `bootstrap_custom_data = null` and
   `bootstrap_render_for_new_vm = true`. Empty protocol tags alone cannot prove
   absence: an existing VM awaiting first health approval also has no tags.
3. Record `invariants.custom_data` and `invariants.agent_sha256` in the receipt.
4. Poll `status` until ready and verify the agent hash. Run `recipient`, pin its
   output through the runtime's recipient protocol, then
   `fetch <sha> --prepare-only`, then deliveries and `sfctl`.
5. Every plan for an existing VM passes the exact receipt bytes and sets
   `bootstrap_render_for_new_vm = false`. Plan-guard remains authoritative for
   unchanged and known existing custom_data; these preconditions supplement it.

[Azure custom data](https://learn.microsoft.com/en-us/azure/virtual-machines/custom-data)
is base64 encoded, limited to 64 KB and cannot be updated on a single VM. The
module checks the encoded size conservatively. To change preparation for future
VMs, review the template, agent, contract hash and Terraform render fixture;
existing VMs keep the old receipt bytes byte for byte. Cloud-init installs only
this fixed agent. It does not fetch Git, select a release or join a tailnet.
After a rebuild onto a new OS disk, cloud-init creates a fresh identity and the
workflows pin it again. A preexisting key or symlink is never overwritten. The
creation block uses a private temporary, cleanup traps and an exclusive hard
link so a racing target is preserved. It syncs the completed identity before
linking and the containing directory after linking, before reporting success.
See [GNU sync](https://www.gnu.org/software/coreutils/manual/html_node/sync-invocation.html).
An existing empty key is reported as unsafe and requires operator recovery;
it is never automatically replaced.

## Storage and package pins

The template uses `/dev/disk/azure/scsi1/lun0`, the documented SCSI path for the
default `B*as_v2` sizes with SCSI controllers. The `vm_size` override is not
validated for controller type. An NVMe-controller override has no matching SCSI
path; a new unlabeled disk then leaves `/data` unmounted and causes preparation
exit 6. See [Azure disk mapping](https://learn.microsoft.com/en-us/azure/virtual-machines/linux/azure-to-guest-disk-mapping),
[azure-vm-utils](https://learn.microsoft.com/en-us/azure/virtual-machines/linux/azure-virtual-machine-utilities)
and [Ubuntu's image notes](https://ubuntu.com/blog/ubuntu-azure-vm-utils-included).
P19 must confirm the exact link on Ubuntu 24.04 for the chosen `vm_size`,
including a SCSI default and an NVMe override, and test retained-disk rebuilds.
`disk_setup` and `fs_setup` use `overwrite: false` with `partition: any`.
This preserves only filesystems that the probes can detect, without relabeling
them. **A retained partition with a damaged or unreadable superblock, whose
filesystem blkid cannot detect, can be reformatted and lose recoverable data.**
`overwrite: false` does not protect that case. P19's retained-disk rebuild test
must include a disposable copy with a blkid-undetected ext4 superblock and check
recovery from a backup superblock before attempting rebuild. Do not rebuild a
suspect retained disk until operator recovery preserves its data. A future disk
initialization change must distinguish a proven-new disk from a retained disk;
the new-VM render flag alone proves nothing about data-disk provenance.
New disks get ext4 labeled `sf-data`, mounted by label at `/data` with `nofail`.
A retained disk without this label leaves `/data` unmounted: preparation fails
closed with exit 6. The kit requires a dedicated mount and places Docker and
containerd there. See the [cloud-init module reference](https://cloudinit.readthedocs.io/en/latest/reference/modules.html)
for disk setup, filesystem selection, mounts, pinned packages, `gz+b64` write_files,
apt sources and runcmd. In particular, do not use `partition: none`, which can
format even with overwrite false.

The age pin is `1.1.1-1ubuntu0.24.04.3` from noble-security (also in updates).
See [package metadata](https://packages.ubuntu.com/noble/age) and the
[Ubuntu changelog](https://changelogs.ubuntu.com/changelogs/pool/universe/a/age/age_1.1.1-1ubuntu0.24.04.3/changelog).
The [Ubuntu CVE review](https://ubuntu.com/security/CVE-2024-56327) and
[USN-8372-1](https://ubuntu.com/security/notices/USN-8372-1) report a plugin-name
validation issue, fixed for noble in `1.1.1-1ubuntu0.24.04.3+esm1` via ESM Apps.
That is outside the required public pockets and is not silently substituted.
This public pin retains that known issue. The root-only identity generated here
is native X25519; the agent invokes only `age-keygen -y` with descriptor-read
input and accepts only native recipient output. The wider kit age usage needs
its own security follow-up.

A superseded updates or security version can leave the archive. Installing a
new VM then fails closed until the pin is raised; existing VMs' creation data
remains fixed. The same review discipline applies to the exact Tailscale pin
`1.104.7` from the stable noble index. Only age and tailscale are explicitly
requested host packages; normal package dependencies remain apt-managed.

The embedded Tailscale armor has this SHA-256 checksum:
`53c6f7dfbd774839d9f37e6c5022ba952108aba9a0e556a56f292a9eb605d7cf`.
Its fingerprint is `2596 A99E AAB3 3821 893C 0A79 458C A832 957F 5868`.
The [stable package channel](https://pkgs.tailscale.com/stable/) publishes the
[noble source](https://pkgs.tailscale.com/stable/ubuntu/noble.tailscale-keyring.list)
and [armored key](https://pkgs.tailscale.com/stable/ubuntu/noble.asc). The
[Linux installation KB](https://tailscale.com/kb/1031/install-linux) links that
channel; its [installer](https://tailscale.com/install.sh) selects the same noble
repository and [binary key](https://pkgs.tailscale.com/stable/ubuntu/noble.noarmor.gpg).
The dearmored embedded key equals that binary key. These channels share a
publisher endpoint; the KB does not publish an independent fingerprint.
Cloud-init scopes the embedded key with `signed-by=$KEY_FILE`; boot fetches no
signing key. The review-time test hashes the armor against the constant above.
The KB does not establish that apt installs always disable auto-update, and
[Tailscale update docs](https://tailscale.com/kb/1067/update) describe tailnet
settings enabling updates for new devices. **The kit owns Tailscale upgrades**
and must enforce the desired update policy when joining. Cloud-init never runs
`tailscale up`.

## Release provenance and follow-ups

All Git invocations clear inherited environment/configuration, disable other
protocols, hooks, fsmonitor and recursive submodules, and enable object checks.
See [Git configuration](https://git-scm.com/docs/git-config) for protocol.allow,
core.hooksPath, transfer.fsckObjects and HTTP low-speed controls, and the
[Git environment reference](https://git-scm.com/docs/git#Documentation/git.txt-GITCONFIGNOSYSTEM)
for configuration isolation. Fetch uses depth one, no tags and no recursive
submodules. Release admission first lists `refs/tags/v*`, accepting only exact
`vX.Y.Z` names and their commit IDs; annotated tags require the peeled line.
Duplicate ref names fail closed. See [git ls-remote](https://git-scm.com/docs/git-ls-remote)
and [git fetch](https://git-scm.com/docs/git-fetch). Fetching a base-repository
SHA alone is insufficient evidence of release approval (P21). Existing checkouts
are never re-fetched and must retain their recorded `.git/config` digest before
Git can inspect them, plus their original commit, clean tree and safe ownership.

P22: `refs/tags/v*` must be protected by a tag ruleset restricting creation to
release automation and blocking update and delete, or by immutable releases
with equally restricted release creation. See [GitHub rulesets](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets)
and [immutable releases](https://docs.github.com/en/code-security/supply-chain-security/understanding-your-software-supply-chain/immutable-releases).
Residual trust is GitHub HTTPS and the actors able to create or move these tags.
This change does not add or verify a ruleset. P21 requires every delivered SHA,
job_workflow_sha, sf-approved-sha and kit_commit to be a release tag commit.

A flawed agent is fixed in custom_data for the VM's lifetime. A later released
kit step may replace `/usr/local/sbin/sf-host-agent` atomically; require_agent
must compare status `agent_sha256` against an allowlist. Until that step exists,
a fix requires a rebuild. A later **kit PR** owns that replacement step, alignment
of kit-control.py checkout with this tag check and Git hardening, and Tailscale
update policy. A later dependency-maintenance PR owns a Renovate regex manager
with the deb datasource for these `.tftpl` pins; renovate is outside this change.

Decision 13's **configurable source repository** follow-up is jointly owned by
the agent, kit and receipt maintainers. It must introduce a receipt source field
and keep both fetch paths aligned under the same reviewed provenance policy.
Tracking: [issue 22](https://github.com/agenticcodingops/software-factory/issues/22).
The source is set once per instance; a mismatch between the receipt and the
configured source must refuse fetching. Until that change lands, both source
constants remain fixed and identical.
