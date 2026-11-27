# Operator bootstrap (WP5)

Run `sf-bootstrap.sh` on the operator's Linux device using their own `az login`
and `gh auth login`. Install Bash, Python 3.11+ with PyYAML, Azure CLI, GitHub CLI, Terraform
at `.terraform-version`, curl and age. No cloud login is performed by tests.
`sf` delegates `age-key` and `restore-assist` to the same implementation.

All modes default to dry-run. Add `--apply` to mutate anything, including local
configuration or key generation. Calls are printed; bodies, CLI diagnostics and
secret responses are withheld. Python's standard library handles JSON and pipes
behind the Bash entry points; PyYAML locates caller job boundaries without rewriting other targets. Disable terminal/session recording when using
password-manager commands. The scripts disable tracing and core dumps. Existing
operator login caches belong to the operator's CLI sessions.

Use `--instance-dir /absolute/private/directory` or `SF_INSTANCE_DIR`. This must
resolve outside the public checkout. It holds only configuration, public claims,
receipts, an assignment ledger and a storage-lock recovery ledger. Rendered Terraform trees and temporary 0600 JSON
var-files are removed in every exit path, after role cleanup. Never put
credentials in configuration, CLI arguments, Terraform variables or backend
settings. No private key, SAS, bearer token, plan or pulled state is written by
bootstrap. Terraform itself can write `errored.tfstate` when a backend save fails.
Bootstrap detects that file, moves it to a private 0600 `recovery/ID/errored.tfstate`,
reports `terraform state push` recovery instructions and blocks further work until
the operator recovers and securely deletes it. `cleanup` remains available. This
explicit recovery exception preserves the only copy of unsaved state. Subprocess failures intentionally hide diagnostics that could contain
request bodies. Decrypted archives pass directly between the two age processes.
Ciphertext is held in memory during copying; provision memory for the largest blob.

## Configuration and modes

`bootstrap.json` supplies reusable CLI defaults: `org`, `instance_repo`,
`operator_login` (or `--operator-login LOGIN`), `source_repo`, `release_sha`, and, for custody operations, `subscription`,
`key_vault_id`, `key_vault_name`. These names and resource IDs belong only in the
private instance directory. `config/instance.tfvars.json` extends the instance
skeleton with WP4's `prefix`, `location`, `state_account_name`, `pilot_end_date`,
`monthly_credit`, immutable `role_home_target`, and Owner-authorized `workflow_refs`. Prefixes match the trust root: 1–17 lowercase alphanumeric characters. Trust and host initialization pass only variables
accepted by the corresponding root; backend authentication explicitly uses the
operator CLI, Entra authentication and `use_oidc=false`. Every subprocess receives an environment allowlist: `PATH`, `HOME`, `LANG`,
`LC_ALL`, `LC_CTYPE`, `TZ`, plus `XDG_CONFIG_HOME` and `AZURE_CONFIG_DIR` for CLIs.
Escrow/key adapters and age receive only the first six variables; configure adapters
through local files or non-secret arguments. Shell tokens, GitHub host overrides,
Terraform arguments and cloud credential overrides are never inherited. GitHub is
fixed to github.com and `gh api user` must identify the configured human operator
before the first write. Terraform receives only bootstrap-controlled ARM settings.

1. `init --operator-login LOGIN --org ORG --instance-repo REPO --reviewer LOGIN[,LOGIN] --source-repo SOURCE --release-sha SHA --apply`
   requires a verifiable Enterprise organization and an Actions policy allowing
   the pinned reusable workflows. It creates a private repository, copies missing
   files from `templates/instance/`, writes CODEOWNERS, configures immutable OIDC
   claims and environment gates, then dispatches a harmless probe on `main`.
   Template, CODEOWNERS and probe changes are committed to a deterministic branch
   and opened as a PR. Merge it with the required reviews, then rerun the same
   command; bootstrap never adds a bypass actor or writes files directly to main.
   Once rulesets are active, another listed CODEOWNER must approve the operator's PR;
   configure enough reviewers for that approval to be possible.
   A probe nonce stays pending across this review and the dispatched request records
   the reviewed caller commit on main. Initial setup may require two such PRs.
   Existing instance settings are preserved. The initial invocation deliberately
   returns incomplete. Inspect the run ID and rerun with `--probe-run RUN` while
   **both** production and restore are waiting for review. This verifies approval
   enforcement without approving anything. Only then does bootstrap write its
   successful gate receipt. Awaiting a PR preserves the previous gate receipt;
   live environment/template readbacks still guard every federation. A successfully
   dispatched new reviewer probe marks setup incomplete until its gates are verified.
   One reviewer permits self-review; two distinct reviewers prevent it. `plan`
   has no branch restrictions; the other environments allow only `main` branches.
   Admin bypass is explicitly disabled and read back for production, restore and ops.
   CODEOWNERS assigns the operator to every generated pattern, including callers.
   The future automatic ref-bump carve-out remains disabled until separately reviewed.
   Organization workflow enforcement is separate: caller merges stay operator-only
   until `rulesets` successfully installs and verifies the pinned required workflow.
2. Approve the harmless run yourself. `oidc-probe --probe-run RUN --apply`
   collects the real claims from its GitHub logs. It checks the dispatch commit,
   referenced reusable workflows, nonce, issuer, audience, environment, immutable
   repository IDs and exact `job_workflow_ref`. Only these selected public claims
   are logged by `.github/workflows/sf-oidc-probe.yml`; tokens are never logged.
   For each actual workflow ref in `workflow_refs`, run `oidc-probe --apply` and
   collect that run after approval with `oidc-probe --probe-run RUN --apply`.
   Without `--apply`, `oidc-probe` describes the PR and dispatch steps without
   creating a branch, writing a nonce or reading a planned branch that does not exist.
   Workflows must implement the probe-only interface (`environment`, `nonce`,
   `probe_only`) with no cloud operations. Missing workflows or probes cannot be
   replaced by synthesized subjects or an environment-only federation.
3. `trust --vault-subscription VAULT --target NAME --subscription TARGET --resource-group RG --location REGION --apply`
   registers all nine namespaces documented by WP4, registers `Microsoft.Compute/EncryptionAtHost` in every target subscription, waits for `Registered` and re-registers Compute before Terraform. Pending/failed registration stops initialization. It checks the vault boundary and bootstraps hardened state storage (`Standard_GRS`, OAuth, public Enabled/Allow, Shared Key and anonymous access off, TLS 1.2, versioning, blob/container soft delete of seven days),
   imports the vault RG, state account, `tfstate` container and `CanNotDelete` lock named `state-cannot-delete` through WP4 trust, refreshes with a no-diff plan and checks federation
   outputs against the probe. Resource-group scope is the default. For
   `--scope subscription`, an absent host-owned RG is created by a targeted Owner
   apply of `module.host.module.workload_rg[0]`, using private
   `config/host-NAME.tfvars.json` and `bootstrap.json`'s `backends.host-NAME`. Before creating that RG, bootstrap targets only `module.trust.azurerm_role_definition.target` and records its `deallocator_role_id` in the private host inputs. Host inputs must match the final root variables (there is no `budget_start`); original `bootstrap_custom_data` and all five live `protocol_tags`, including `sf-maintenance-until`, pass through unchanged. Only the RG is applied here; later VM plans must read live tags under the target lock and run plan-guard.
   This establishes the RG before trust's RG-scoped grants. No host is created.
   Cost forecast input is in private `cost-query.json` (Azure Forecast API body).
   Usage and forecast authorization failures fail the operation. The command prints
   vCPU family/Total Regional and credit-minus-forecast headroom. Repeated targets
   extend the same trust state and retain existing entries. Every trust initialization uses `render-trust.py` with the full inventory and subscription aliases; removal retains old aliases for that apply. New trees exclude state, plans, tfvars and provider caches. Bootstrap refuses source symlinks that escape the source tree and private output parents that escape the instance directory. When all four ops-plane workflow refs exist, bootstrap stages static caller additions in the private directory for CODEOWNER review, preserving other targets’ current literal refs byte-for-byte. It never pushes these caller edits to main. More than one foreign
   target tenant requires a separately reviewed root composition.
   `--vault-mode same-subscription` requires identical subscriptions. With a
   spending limit On, it additionally requires `--accept-vault-risk`: exhausted
   credit can make backups and state unavailable with the VM.
   The first apply records its home target; `--role-home-target NAME` can explicitly select it. Adopting existing targets requires that explicit input (select the formerly first lexical target). Its key, subscription and region cannot change; decommissioned home targets remain registered. Omitted `--scope` preserves an existing target's RG ownership.
   `--remove-target` requires `config/receipts/destroy-NAME.json` with that target
   and `destroyed=true`, from the approved teardown package, and refuses the last
   target or the trust home. Caller entry removal still requires review.
4. `age-key --custody operator --escrow-cmd EXECUTABLE --apply` creates an in-memory
   key. The escrow adapter takes the key on stdin, stores it in a password manager,
   retrieves/derives it and returns **only its public recipient** on stdout as
   readback. Its arguments must contain no secrets. No screen-print fallback is
   provided, following the WP5 requirement that secrets never be printed.
   `--custody vault` also acquires and verifies temporary Key Vault Secrets Officer
   access on the instance vault, writes `age-FINGERPRINT` through `/dev/stdin`,
   verifies the enabled exact version and derived recipient, and removes the role.
   Public values are read back from GitHub variables. A failed publication resumes
   the existing recipient without generating a key. Before escrow, a pending public
   fingerprint is saved; after an uncertain escrow response, use `--key-cmd` to
   retrieve that exact key and resume (the adapter can use the pending fingerprint).
   `--rotate` retains every old
   map entry enabled and dispatches `ph-rotate-age`; no key retirement is offered.
   Retirement still needs the ADR's 45-day inventory/retention checks.
5. `tailscale --token-stdin --apply` consumes a short-lived API token through stdin,
   verifies HTTPS/MagicDNS and creates exact plan/production federations from probe
   claims. It reads back each subject, workflow claim rule, scope, tag and generated
   audience, then stores only non-secret IDs/audiences. Creation uses the upstream
   keys API with `keyType=federated`. Both inventory checks fetch each key's details
   before filtering descriptions: the [upstream list contract](https://github.com/tailscale/tailscale-client-go-v2/blob/v2.12.0/keys.go#L192-L209)
   supplies identifiers only. The token is revoked on success and failure;
   HTTP 401 is required as revocation readback. A cleanup failure explicitly requests
   console revocation. Only currently Owner-authorized refs are used, even when
   the probe receipt also contains older claims. Managed descriptions are reconciled:
   changed claims are deleted and recreated, withdrawn refs and their repository
   variables are deleted, and the remaining managed key inventory is read back. This mode never writes policy,
   settings or tag ownership and never joins a device. If creation needs reviewed
   tag ownership, finish the separate hardening PR and retry. `tailscale
   --print-subject` (or top-level `--print-subject`) prints claims, scopes and tags
   for the console path without reading an API token.
6. `rulesets --source-repo SOURCE --release-sha SHA --apply` resolves the committed
   public, instance and org payloads at their distinct endpoints. It requires a
   tagged release containing the gate file, fills the numeric source ID, full SHA
   and instance selector, unions existing org repository includes with this instance
   while retaining exclusions and excluding the source repo, then creates/updates
   by name and compares full
   readbacks, including rule parameters and all arrays. It never treats a list
   summary as proof. API errors fail closed; unsupported org rulesets retain the
   operator-only merge path. It does not apply `org-release-notes-gate.json` or
   `public-release-tags.json`, prints them as NOT APPLIED, and the operator applies
   them per [the rulesets README](../rulesets/README.md).
7. `sf restore-assist --backup-account ACCOUNT --staging-account STAGING --blob backups/NAME --version VERSION --staging-blob restore/UNIQUE --ephemeral-recipient RECIPIENT --key-cmd EXECUTABLE --apply`
   reads the recorded fingerprint before retrieving a key. The adapter returns
   the password-manager key on stdout into an anonymous pipe. Bootstrap verifies
   the recipient, then streams decryption directly into encryption for the host's
   ephemeral recipient. Upload uses Entra authentication, a unique create-only
   staging blob in the **separate** `<prefix>staging` account and ciphertext hash readback. Backup-account staging is refused, avoiding the backup account's WORM default; the staging container is `restore-staging` and expires after one day. No account key, SAS or plaintext
   archive is persisted. Unknown fingerprints and failed age processes stop upload.
8. `--revoke --apply` destroys only enumerated federated-credential and role-assignment
   leaf addresses in the existing private trust backend, then verifies removal.
   It first acquires temporary Blob Data Contributor access on the unlocked vault RG
   and waits for data-plane propagation before initializing that backend. Dry-run
   describes the state checks without reading an uninitialized backend.
   Azure [deletion locks also prevent RBAC assignment deletion](https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/lock-resources#considerations-before-applying-your-locks),
   including assignments under storage containers. Revocation inspects role scopes
   and their inherited locks, refuses locks other than account `CanNotDelete` locks,
   and records each affected lock's ID, level and notes in private `locks.json`
   before removing it. Cleanup restores and verifies those locks on success or failure.
   A failed restoration retains the recovery ledger and blocks further work until
   `cleanup --apply` succeeds. Storage resources and their Terraform lock addresses
   are never targeted for destruction.
   It never targets entire identity modules, resource groups, vaults or disks.
   Normal resources and data retention remain intact.
9. `cleanup --apply` restores unfinished storage-lock ledgers, then retries
   unfinished assignment ledgers before any new work.
   Each temporary assignment ID is durably recorded **before** creation. Success,
   failure, lost grant responses and INT/TERM/HUP all attempt removal and verify it.
   Output is best effort after terminal loss. The signal handler raises once;
   INT/TERM/HUP are blocked during token, lock, assignment and working-tree cleanup.
   Failed cleanup retains the ledger and blocks subsequent operations. SIGKILL or
   power loss cannot run handlers; the next invocation requires ledger cleanup.

## Vault migration

`vault-move --phase state|backups|keys --apply` uses private `migration.json`:
`paused=true`, the active `lock_id`, and `source`/`destination` objects with
`prefix`, `subscription`, `state_account`, `state_scope`, `backup_account`, `backups_scope`,
`key_vault_id`, `key_vault_name`, and `backends` for `trust`, `host` (or a separate `host-NAME` for each target), and `tailnet`. Multiple registered targets require every named host backend. Each backend runs in an isolated private Terraform directory.
Each backend supplies `storage_account_name`, `resource_group_name`,
`subscription_id`, `tenant_id`, `key`. Credential/backend override fields are refused.

Pause state-changing workflows and acquire an instance migration blob lease first.
Bootstrap requires the remote lease to be locked and renews that exact lease ID;
local processes also share an exclusive lock. Temporary source vault-RG Blob Data Contributor access maintains this lease in each phase. The container IDs in `state_scope` and `backups_scope` remain validated inventory boundaries; temporary storage grants use their unlocked parent vault RG because account deletion locks prevent cleanup of child-scoped assignments. Bootstrap reads subscription locks and refuses RG/inherited locks before granting. It never removes any lock. This temporarily widens operator data access across that RG; workflows keep their narrow persistent scopes. Further temporary roles are:

| Phase | Source | Destination |
| --- | --- | --- |
| State | Blob Data Contributor on unlocked vault RG | Blob Data Contributor on unlocked vault RG |
| Backups | Blob Data Reader on unlocked vault RG | `<destination-prefix> SF Backup Migration Writer` on unlocked vault RG (read/add/write, no delete) |
| Keys | Secrets User on each required age secret | Secrets Officer on the dedicated instance vault |

All custom display names include the instance prefix. The destination no-delete copy role is defined by the Owner's trust stack and is
never assigned to a workflow identity. Temporary roles grant no WORM or Shared Key
management. Trust's normal ops writer remains unchanged.

State migration uses `terraform init -migrate-state` with Entra authentication.
Because [`-force-copy` automatically confirms migration questions](https://developer.hashicorp.com/terraform/cli/commands/init#backend-initialization),
the first attempt initializes the destination with `-reconfigure` and inspects it
before copying. Azure's [state manager initializes an empty serial-zero snapshot](https://github.com/hashicorp/terraform/blob/v1.17.2/internal/backend/remote-state/azure/backend_state.go#L104-L138)
when first accessed; a snapshot is empty only with no resources, outputs or check results.
Only an empty destination can receive a copy; an identical state
is adopted without copying, and any nonempty mismatch is refused.
Before migration a pending per-stack receipt records lineage, serial and resource
inventory hash. Readback must match before that backend is verified. Retries first
inspect the recorded destination. An empty pending destination permits retry only
after the source still matches every recorded field. A nonempty mismatch or a
changed source keeps workflows paused. Verified destinations are never migrated
backwards. Dry-run describes these checks without running state pull/list.

Backups request versions, metadata and immutability policies together (`--include vmi`),
require every version to carry `recipient_fp`, stream only ciphertext, preserve metadata,
use unique destination names and compare both hashes and destination WORM expiry.
Source blobs are never overwritten or deleted. Repeat this phase for a final delta.
Keys copy every mapped exact historical version in memory, write enabled destination
versions through stdin and verify their recipients. The receipt retains both maps
for rollback. No source version is disabled. Receipts never claim a switch succeeded:
the later approval-gated `ph-vault-move verify/switch` must validate inventory, run a
historical-key restore/drill and prove a destination canary before cutover.

## Plan identities and pilot closure

Target receipts preserve `client_ids.vault` and `client_ids.target` for all four
identities, resolve twins in the immutable home's subscription and include
`role_home_target`. Their `plan_inputs.vault.client_id` selects the vault plan
identity for the default providers; `plan_inputs.target.target_client_id` feeds
the trust root's `target_client_id` and every generated target alias. Owner bootstrap
uses CLI authentication with `target_client_id=null`, without persisting a plan
twin selection into Owner inputs. WP4 grants plan Reader on vault, drill, workload
and twin trust RGs, plus subscription authorization-reader roles; these grants
remain managed by Terraform and are inspected in actual same/cross-tenant mock plans.

At `pilot_end_date`, pause uploads and state-changing workflows and hold the
instance `migration.lock` blob lease. Put `paused=true` and the actual `lock_id`
in private `pilot-closure.json`, then rerun the ordinary full `trust` command with
`--close-pilot --apply`. An early date, absent pause or lost lease fails closed.
The full trust apply locks account defaults and the optional fallback container
policy using WP4's ETag-bound POST action. Bootstrap then grants temporary Blob Data
Owner on the unlocked vault RG, renews/verifies the lease, and calls WP4's
`lock-backup-versions.py` implementation to enumerate all pages and versions, lock
unexpired Unlocked policies without changing expiry, and recheck the complete
inventory. It removes and verifies **all** temporary grants before publishing
`config/receipts/pilot-closure.json` (`account`, `pilot_end_date`, `versions_locked`,
`inventory_verified`, `temporary_roles_removed`, `lock_id`). Uploads remain paused
on any failure; cleanup and idempotent reruns are safe. The operator resumes only
after inspecting that completion receipt. A previous closure receipt is invalidated
before each new closure attempt.

For a new backup account after pilot end, keep uploads paused, initialize trust
with a temporary future end date (Azure creates only Unlocked account policies),
restore the intended date, then run `trust --close-pilot --apply`. No bootstrap
receipt substitutes for the later live deployment/restore acceptance gates.

## Acceptance evidence

Run `bash ci/check.sh`; it includes shellcheck of both entry points and every Bats
file, the WP5 stub suite, WP4 mock Terraform tests, actionlint, zizmor, gitleaks and
LF/identifier checks. Every external CLI in Bats is a fail-closed local fake,
including `az`, `gh`, `terraform`, `curl`, `tailscale`, age and escrow. Unexpected
commands fail. Fixture IDs are symbolic; secret sentinels are generated only at
runtime and assertions scan output, argv and all temporary files for leakage.

| Requirement | Bats test name (in `tests/bootstrap.bats`) |
| --- | --- |
| Payloads outside the rulesets command are announced, never silently skipped | `rulesets name every committed payload they do not apply` |
| Dry-run and idempotence | `dry-run is default…`, `trust applies WP4 variables…`, `rulesets resolve pinned gate…` |
| Actual immutable subjects and exact workflow refs | `environment-scoped probe collects real sub…`, `probe refuses wrong sub…`, `trust refuses changed subject template…`, `tailscale federates exact probe claims…` |
| Role scopes and WP4 names | `trust applies WP4 variables…`, `subscription scope sets host RG ownership`, all three `vault …` phase tests |
| RG grants, account delegation, ops cost reader/twins, ABAC write/delete constraint | WP4 `trust.tftest.hcl` mock assertions and `test_terraform_contract.py`, also run by `check.sh`; bootstrap passes these exact stacks and preserves all targets |
| Secrets never in argv/output/disk | `age-key vault custody…`, `operator custody…`, `restore-assist streams…`, `vault keys copy…`, `tailscale federates…` and injected-failure tests |
| One/two reviewers | `single reviewer disables…`, `two distinct reviewers enable…` |
| Unsupported/unknown capability and invalid Actions policy | `init refuses Free Pro Team…`, `init reports only the wrong…` |
| Protection-rule readback and reviewer wait | `init refuses failed protection…`, `single reviewer disables…` |
| Spending-limited same-subscription refusal | `spending-limited same-subscription vault…` |
| No baseline policy/settings/tag-owner writes or device joins | `tailscale federates exact probe claims…`, `tailscale creation failure…` |
| Temporary age write role and cleanup | `age-key vault custody…`, `age-key refuses missing escrow…`, `lost grant response and SIGTERM…`, `cleanup failures retain crash ledger…` |
| Migration verification and all cleanup paths | `every migration phase cleans…`, `vault migration refuses lost lease…`, all three successful phase tests |
| Historical key preservation/selection | `key rotation preserves enabled…`, `vault keys copy…`, `restore-assist refuses unknown…` |
| Static caller isolation | `static target caller additions preserve…` |
| Interrupted migration and retry | `state migration inventory failure…`, `backend migration init failure…`, `vault state migration isolates…` |
| Grant propagation and token cleanup failures | `age-key cleans after failed grant…`, `tailscale reports token cleanup…` |
| Target removal and authorization-only revocation | `target removal requires…`, `revoke targets only…` |

These are offline implementation checks, not live approval, token exchange, restore
or cloud authorization evidence. This repository still lacks the later deployment,
trusted teardown, migration-switch and rotation workflows and the implemented merge
gate. Bootstrap refuses unresolved workflow refs and ruleset releases; no stub,
fallback subject or bootstrap receipt waives the ADR's live release gates.

The API contracts are documented in [GitHub OIDC customization](https://docs.github.com/en/rest/actions/oidc),
[GitHub token claims](https://docs.github.com/en/actions/reference/security/oidc),
and the [Tailscale v2 keys client](https://pkg.go.dev/tailscale.com/client/tailscale/v2#KeysResource.CreateFederatedIdentity).

## WP4 port contract regressions

Ported from `b6b019e` against final WP4 (`574d9d3`), preserving its existing guards,
provider/module pins and seven prefixed role definitions. The WP5 migration role
is also prefixed and grants read/add/write blob data only.

| Bootstrap side ↔ final Terraform contract | Regression evidence |
| --- | --- |
| Vault RG, state account, `tfstate`, `state-cannot-delete` IDs ↔ all four root import blocks | Bats `bootstrap calls and receipts match parsed WP4…` invokes `verify_contracts.py`, parsing the real blocks and comparing executed resource/lock calls |
| State GRS/network/OAuth/TLS/shared-key settings and seven-day blob/container soft delete ↔ `module.state` | Same parsed-contract test, unsafe-storage tests and `trust_storage_and_authorization` Terraform mock |
| Complete target inventory ↔ rendered subscription providers, RG readiness and moved addresses | Parsed-contract Bats; `trust home survives…`; WP4 renderer and workload RG mocks |
| Shared trust/revoke input allowlist ↔ every declared and required root variable, including `role_home_target` and `target_client_id` | `test_allowlist_equals_actual_trust_root_variables`; parsed-contract Bats; `revoke targets only…` |
| Explicit stable home and twin lookup ↔ persisted home guard and home-bound target aliases | `trust home survives…`; `trust_home`/missing-home/earlier-target Terraform tests; target-removal Bats |
| Prefixed migration role lookup ↔ `${var.prefix} SF Backup Migration Writer`; other role names and ABAC remain WP4-owned | `vault backups stream…`; Terraform migration permission/name assertion and second-prefix role test |
| Vault/target plan client receipt ↔ `target_client_id` on default target and generated aliases; plan read grants | Cross-tenant/parsed-contract Bats; WP4 `trust_read_findings` on actual same/cross-tenant plans |
| Owner provider/feature registration ↔ registration-disabled providers and read-only deploy prerequisite gate | `Owner registers all final namespaces…`; WP4 preflight/provider tests |
| Owner role creation → recorded deallocator ID → host RG only → full trust ↔ host-owned RG data read before grants | `host-owned RG initialization…` parses host variables and verifies ordering |
| Original bootstrap and five protocol/maintenance inputs ↔ host pass-through; obsolete budget start omitted | Same host initialization Bats; WP4 host upgrade and plan-guard tests |
| Separate staging account, container and receipt ↔ WORM-free `module.staging` and one-day lifecycle | `restore staging account and receipt…`; WP4 staging storage/lifecycle mocks |
| Temporary storage roles on unlocked vault RG ↔ account deletion locks; ledger cleanup | `temporary storage roles refuse inherited RG locks…`; all migration/cleanup Bats |
| Pilot closure receipt ↔ Locked account/fallback defaults plus complete version locks | Two `pilot closure…` Bats, WP4 fallback-lock mocks and paginated/partial-failure/expiry-preservation tool tests |
| Fresh private rendered trees ↔ renderer containment and literal disk protection | Parsed-contract Bats; `test_renderers_reject_source_symlink_escapes_before_execution`, `test_private_parent_symlinks_cannot_write_outside_instance`, WP4 renderer regressions |
| No tailnet baseline mutations ↔ three-import no-diff baseline, real non-operator deny source and strict policy lint | Existing tailnet Bats; retained WP4 tailnet/policy suites (bootstrap owns no policy/deny-test inputs) |

All acceptance checks are offline. The probe workflow must still run in the private
instance and the operator must verify live approvals, token exchanges, RBAC, WORM,
restore and migration-switch evidence before release.
