# Terraform deployment contracts (WP4)

These reusable modules and three root stacks implement the infrastructure in
[ADR 0001](../../../docs/adr/0001-azure-deployment.md). They are checked with mock providers;
they do not enable deployment from this repository. Bootstrap, approved deployment workflows,
cloud-init preparation, live self-tests and the trusted ops controller belong to later packages.
The original bootstrap data must be supplied from the private deployment receipt on every host plan.
It must create the interactive `admin` account; Azure reserves that provisioning username,
so the VM's provisioning account is `sfadmin`.

| Stack | Ownership | Authentication |
| --- | --- | --- |
| `trust` | Vault/state/backup storage, WORM, Key Vault, roles, identities, workload RG and tenant twins | Operator's own Owner login for apply; plan identity reads drift |
| `host` | VM/network/data disk plus `guardrails`: budgets, action groups, target RG Logic App | Approval-gated deploy identity |
| `tailnet` | Imported whole policy and settings, MagicDNS, explicit-generation join key | Federated plan/production identities |

Root providers use exact pins. Child providers have bounded ranges. Local modules are versioned
with this repository commit; every registry module uses the ADR's exact release. The stacks commit
upstream-signed checksums for Linux amd64 and Windows amd64. CI regenerates both and rejects drift.
No credentials or instance identifiers belong here; pass private configuration and backend settings
from the instance repository. Never commit state or plans.

## Version decisions

| Provider | Exact version |
| --- | --- |
| Terraform | 1.17.2 |
| AzureRM | 4.81.0 |
| AzAPI | 2.14.0 |
| Tailscale | 0.30.0 |
| Random | 3.9.1 |
| Time | 0.14.2 |
| TLS | 4.4.2 |

These releases were resolved from the [Terraform registry provider API](https://developer.hashicorp.com/terraform/registry/api-docs/providers)
on 2026-11-26. AzureRM stays below 5, as the ADR and AVM ranges require. All nine AVM releases
match the ADR table; `test_every_adr_registry_module_and_root_provider_pin` verifies them.
The [AVM index](https://azure.github.io/Azure-Verified-Modules/indexes/terraform/tf-resource-modules/)
lists disk as Available. Inspection of disk 0.5.0 found no literal `prevent_destroy` interface,
so the permitted `azurerm_managed_disk` fallback preserves that contract.
Tailscale 0.30.0 exposes `https_enabled`; this is managed rather than left to a bootstrap fallback.

## Backend and target boundaries

Initialize each root with private `-backend-config` files. The backend always enables OIDC and
Entra data-plane authentication; trust apply uses the operator login with `-backend-config=use_oidc=false`.
The bootstrap-created vault RG, state account, `tfstate` container and `CanNotDelete` lock named `state-cannot-delete` are explicitly imported
by `stacks/trust/main.tf` using AzAPI, which does not persist account keys.
The `tfstate` container, soft delete, versioning, Shared Key prohibition and deletion lock are managed.
No SAS, password or SSH private key is created by these modules.

Before every trust init/plan/apply, compose a fresh isolated tree from private configuration:

```sh
python3 hosting/paperclip-hermes/tools/render-trust.py \
  hosting/paperclip-hermes/terraform /tmp/instance-trust \
  --config /private/instance.tfvars.json
terraform -chdir=/tmp/instance-trust/stacks/trust init -backend-config=/private/backend.hcl
terraform -chdir=/tmp/instance-trust/stacks/trust plan -var-file=/private/instance.tfvars.json
```

Use the same backend/state for every rendered tree. The renderer creates a stable provider alias and
workload RG module per subscription, using the ADR's pinned AVM resource-group module. Generated
`moved` blocks preserve addresses from the original trust module. Regenerate from the current full
target inventory before each run; missing RG composition fails before scoped grants are applied.
When removing a subscription's final target, keep that subscription in the renderer configuration
until its resources have left state; pass the reduced target map to Terraform so it can destroy
those resources using the retained provider alias.
If an earlier multi-subscription apply created an RG in the wrong subscription, the operator must
inspect that partial state and its resources before approving the corrective plan.

Set `role_home_target` once to the existing target that anchored the first trust apply (for an
upgrade, the formerly first lexical target). Its subscription and region anchor the custom roles,
trust RG and tenant twins. Keep this target registered even after its host is decommissioned.
A state-persisted `terraform_data.trust_home` with `prevent_destroy` refuses changes to the home key,
subscription or region; role definitions and the trust RG also prevent destruction. Run plan-guard
on trust plans to reject replacement/removal of target roles and every twin identity. Home migration
requires a separate operator-reviewed migration with preserved role/principal IDs.

The plan identity has Reader on the vault, drill and workload RGs, plus Reader on the twin trust RG
in the target tenant. Its custom authorization-reader roles grant only `Microsoft.Authorization/*/read`
at the vault and target subscriptions, allowing drift refresh of subscription roles and assignments.
For cross-tenant plans set `target_client_id` to the plan twin's client ID; the default vault providers
use the vault plan client. Generated subscription aliases also use `target_client_id`.

The trust root has default vault providers and explicit target aliases. `targets` includes every
subscription in the configured target tenant, including targets being added. If the target tenant
differs, target-facing identities are duplicated there and receive only target grants; vault identities
keep vault grants. Twins live in a separate owner-controlled `rg-<prefix>-trust`, outside the workload
Contributor boundary. More than one foreign tenant requires an operator-reviewed root composition
with an explicit alias per tenant; Terraform cannot select provider aliases dynamically. Do not apply
separate copies of the vault resources to independent states.
AzureRM's automatic registration is explicitly `none`, and AzAPI's registration is skipped on every
root/generated alias. The Owner bootstrap must register the required namespaces in each subscription
before trust/host initialization: `Microsoft.Compute`, `Microsoft.Network`, `Microsoft.Logic`,
`Microsoft.Insights`, `Microsoft.Consumption`, `Microsoft.DevTestLab`, `Microsoft.ManagedIdentity`,
`Microsoft.Storage` and `Microsoft.KeyVault` in each subscription where used.
Encryption at host is an **operator-registered, validated prerequisite**. In each target subscription,
the Owner runs `az feature register --namespace Microsoft.Compute --name EncryptionAtHost`, polls
`az feature show --namespace Microsoft.Compute --name EncryptionAtHost --query properties.state -o tsv`
until `Registered`, then runs `az provider register -n Microsoft.Compute` and waits for provider
registration. Before every `ph-deploy` host plan/apply the later deployment workflow must run
`bash hosting/paperclip-hermes/tools/check-azure-prerequisites.sh "$SUBSCRIPTION_ID"` under its target
identity, whose `SF Prerequisite Reader` role grants only `Microsoft.Features/providers/features/read`
and `Microsoft.Resources/subscriptions/providers/read` at each target subscription. The read-only gate validates the target namespaces (Compute, Network, Logic, Insights, Consumption,
DevTestLab and ManagedIdentity) and feature and fails with an Owner bootstrap instruction. Read-only plans and RG-scoped workflow identities cannot perform
subscription-level provider registration as a side effect of provider setup.

`rg_managed_by=trust` references the existing workload RG; `host` creates it for subscription scope.
For a new host-owned RG, the Owner must initialize its host-state RG before full trust apply:
first apply only `module.trust.azurerm_role_definition.target` in the rendered trust root, record its
`deallocator_role_id` in private host inputs, then apply only `module.host.module.workload_rg` in the
host root with the Owner login. Review both saved plans. Now run the full trust apply and the ordinary
approved host apply. These limited initialization applies create neither a VM nor a second RG owner.
The subscription-specific trust composition reads host-owned RGs and waits for them before RG-scoped
grants; a missing group fails the read. This avoids granting subscription-wide Contributor merely to
create the group. No workflow gets Owner or unrestricted RBAC.
Custom role display names include the instance prefix (for example, `<prefix> SF VM Deallocator`);
assignments and ABAC use their resource IDs. Deploy can assign only that deallocator role to service principals; its ABAC condition constrains both
assignment creation and deletion. Ops gets Cost Management Reader on every target subscription,
never cost writes. Live subscription usage/forecast and denied-write probes remain deployment gates.

## Ops identity boundary (release blocker)

Environment-only federation is **not created**, including for `id-sf-ops` and its tenant twins.
The operator configures GitHub's generally available subject customization with the ordered claims
`["repo", "context", "job_workflow_ref"]`, using immutable repository subjects. The resulting prefix
must be read back from GitHub, as required by the ADR. Each `workflow_refs` entry must be a complete
reusable workflow path at a full commit SHA. Empty maps create no federated credentials.
Each Azure credential matches the entire issuer, audience and subject, including that exact workflow.
A release adding its own `ops` job in `ph-deploy`, or a candidate `ph-ops` SHA, cannot match an
owner-authorized ops credential. There is no environment-only fallback and no preview Azure feature.

This narrows the environment-only subjects in ADR revision 3 to meet issue #5's review requirement.
See [GitHub's custom subject contract](https://docs.github.com/en/actions/reference/security/oidc#customizing-the-subject-claims-for-an-organization-or-repository)
and [OIDC with reusable workflows](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-with-reusable-workflows).
The trust mock plan and `check-mock-plans.py` exercise adversarial candidate subjects against the
actual generated credentials. This is offline authorization evidence, not a live Azure token exchange.

Until a separately trusted controller implements approval, target-tag/recovery checks and mint-only
job dependencies, the Owner must authorize each workflow SHA through trust. A release cannot widen
its own allowlist. Automatic ops handoff and candidate-release mint jobs remain unavailable.
WP4 cannot run the live adversarial-release acceptance without an instance, GitHub environments and
Azure credentials. That live test remains release-blocking; a passing local mock does not waive it.

## Policy baseline and hardening

Import the complete live policy into `module.tailnet.tailscale_acl.this`, settings into
`module.tailnet.tailscale_tailnet_settings.this`, and DNS into `module.tailnet.tailscale_dns_preferences.this`.
Provider `overwrite_existing_content` is never enabled. In `mode=baseline`, pass every live setting
and DNS preference unchanged, with `issue_join_key=false`. Only read-only no-diff planning is allowed.
The local `terraform_data.join_generation` resource exists only in hardening mode, so the initial
baseline needs only the three live imports and creates no local generation marker.
The `reviewed_fragment` output is a patch for the next operator-reviewed hardening PR; Terraform
does not silently replace or narrow the imported policy. It renders tag owners, the operator grant,
SSH check mode, operator accept tests, pilot outbound denies and another-user denies.
Set `non_operator_login` to a real second tailnet login distinct from the operator. If there is no
second user, leave it null and the module omits that deny test. An autogroup cannot be a test source.
`isolation_destinations` must sample every other tag/user/host:port in the actual imported policy.

Run `tools/policy-lint.py POLICY --operator LOGIN` before any hardening or later plan/apply.
It parses HuJSON, expands groups and host aliases, checks every ACL/grant and protects SSH check mode.
It normalizes tag case, covers IPv4/IPv6 tailnet ranges and `autogroup:danger-all`, and rejects IP-set
references and unknown selector types until they can be resolved safely. Reserved selectors cannot
be shadowed by host aliases. Omitting both grants and ACLs fails because it inherits allow-all.
The sole report-only exception requires `--baseline-plan PLAN.json --live-policy LIVE.hujson` and
`--operation plan`: exact live policy bytes, no policy/settings resource diff, no unrelated changes.
It lists unsafe findings without authorizing apply or join. Live reads and the no-diff plan must come
from the trusted read-only workflow, not PR-supplied artifacts. A label/input alone is insufficient.
`hardening_applied` is an attestation from the approved workflow's applied-policy receipt, not an
operator bypass. Workflows must verify that receipt and enforcing lint before setting it. The module
rejects joining from a baseline or without this evidence. Explicit `join_generation` replaces keys;
VM replacements do not. Keys are single-use and expire after 3,600 seconds, and travel only encrypted.

## Upgrade and disk contracts

Pass the four current protocol tags **and the live `sf-maintenance-until` deadline** from ARM in
`protocol_tags`, plus the original bootstrap `custom_data` from the receipt. Read ARM after setting
maintenance and before planning; preserve the exact existing deadline, without generating a new one
in Terraform. For example, select these five keys from `az vm show --ids "$vm_id" --query tags` into
the private `protocol_tags` map, omitting absent keys. Hold the target's workflow lock through read,
plan and apply so another protocol writer cannot invalidate that snapshot. A new VM uses an empty
map and receives no protocol or maintenance tags. Ordinary `tags` cannot supply these keys.
The `expires` tag uses `time_static` plus a 30-day `time_offset`.
Shutdown defaults to 22:00 and backup to 21:15; invalid times or a smaller gap fail planning.
`tools/plan-guard.py PLAN.json` rejects protected deletions/replacements and retention/lifecycle changes.
`--override-replace` authorizes delete-only VM changes, which have no successor to preserve.
Updates and replacements still reject protocol/maintenance tag changes and changed/unknown existing VM bootstrap data.
RG/subscription deletion, replacement or forgetting is also protected: deleting a parent can cascade
to a disk even when that disk's own plan is no-op. VM data-disk attachments must retain `Detach`;
changing them to `Delete` cannot bypass disk protection with a later VM-only deletion.
AVM 0.22.1 stores cloud-init in a write-only field; the guard also checks its first replacement
fingerprint, covering changes invisible in ordinary `custom_data` JSON.

The committed data disk always has literal `prevent_destroy=true`. `allow_disk_destroy=true` alone
fails its precondition. Parking removes compute/network resources and retains the same protected disk.
`tools/render-host.py SOURCE DESTINATION` copies only Terraform source to a new isolated directory.
The destroy form additionally requires `--allow-disk-destroy --authorization RECEIPT --target NAME --run-id RUN`.
It checks production approval, typed confirmation and a successful, verified, stopped-writer final
backup less than 24 hours old for that target/run, then changes only the two protection literals.
The disk address stays the same and the source tree stays protected. Pass `allow_disk_destroy=true`
to the rendered root for the preparatory apply and destroy.

The renderer is a local mechanism, not an approval authority: a later trusted teardown workflow must
verify GitHub approval and recompute the vault blob's hash, bind the receipt to the selected VM/disk,
revalidate freshness before each apply/destroy, reject unrelated preparatory plan changes, and restore
protection on surviving resources after failures without recreating deleted resources. Those end-to-end
gates and fault injections belong to teardown/move packages. Full destruction remains unavailable
until they pass; ordinary deploy/ops must never invoke the destroy renderer.

## Retention and cost controls

Backups use versioning and 30-day version WORM, Unlocked only before `pilot_end_date`, Locked afterwards.
The optional container policy is enabled only by failed inheritance self-test 3. After `pilot_end_date`,
the next approved trust apply explicitly locks that policy using Azure's `POST /lock` operation and
its ETag, matching the account policy's lock transition. Changing a default does not lock old versions;
complete the pilot-closure procedure below as well. All storage accounts use explicit `Standard_GRS`,
public network access and an Allow network rule so hosted runners and operator devices can reach their
Entra endpoints. Shared Key is disabled, anonymous blob access is disabled, HTTPS/TLS 1.2 is required,
and containers are private with account deletion locks. A private-endpoint deployment requires
network-connected runners and operator access and is outside this ADR's default. Data roles use container scopes;
user-delegation roles use the backup account for ops and the staging account for restore. Deploy has no backup/key rights. Operator custody
grants no workflow any key access; vault custody grants only restore the read-only Key Vault Secrets User.

Azure lifecycle filters include the container name: `backups/backups/` means encrypted objects under
`backups/` in the `backups` container, excluding long-lived `receipts/`. Both base blobs and versions
expire after 45 days. Restore staging lives in the separate vault account `<prefix>staging`, with
versioning but no WORM default or soft-delete delay. Its `restore-staging/` base blobs and versions
expire after one day. This isolates staging from the backup account's locked 30-day default while
preserving backup protection. Restore/operator data rights name the new container; restore's
delegation-key role names only the staging account. The old staging container and its cleanup rule
remain in the backup account so upgrades do not try to delete locked data; no restore/operator grants
point there. Its existing ciphertext remains subject to retention already inherited before this fix.
The imported kit's
upload path must be migrated to that object prefix by the runtime package before deployment is enabled.
Mock plan inspection verifies both actions and both prefix filters.

RG budgets are `max(5, min(cap, headroom))`, with the approved override using the cap. Alerts are
50% actual email, 80% and 100% actual deallocation, 100% forecast email. The credit budget uses
70% actual email, 85% and 100% actual deallocation, and 100% forecast email;
`billing_month_verified=false` selects Monthly and **email only** until self-test 7 passes.
Email and deallocation action groups are separate, so email thresholds cannot stop a VM.
The Consumption Logic App and its SF VM Deallocator assignment are in the target RG and its POST
URI names only the pilot VM. Budgets wait for its role assignment before alerting. It checks budget thresholds and the maintenance tag before deallocating;
100% is the emergency hard threshold. The
[monetary notification payload](https://learn.microsoft.com/en-us/azure/azure-monitor/alerts/alerts-non-common-schema-definitions#budget-alerts)
expresses `NotificationThresholdAmount` and `Budget` as amounts; all three comparisons use their
ratio, including the emergency override. CI evaluates actual mocked workflow expressions against
string/numeric monetary payloads with active, expired and absent maintenance windows.
The webhook uses Entra authentication, not a SAS in state.
`alert_application` describes an operator-provisioned secure-webhook application and Monitor sender.
Its unsigned runtime invoke URL comes from Azure
`properties.accessEndpoint`; no `listCallbackUrl` call or signed URL enters state. Live webhook delivery, payloads, maintenance behavior and forecast notifications
must pass self-test before budgets authorize deallocation. The authoritative real-billing-period cost
workflow remains a later package. Code never changes the subscription's spending limit.

The race mitigation is a **second ARM GET and maintenance check immediately before the deallocate
POST**, with no automatic POST retries. A failed recheck cannot reach the POST. The 100% actual
notification still overrides maintenance, with the same subscription billing-month gate. This is
not an atomic exclusion: maintenance written after the second GET and before Azure accepts the POST
can still lose the race. No shared lease is implemented. Live acceptance must exercise this residual
window and operators must account for it; this implementation does not claim race-free maintenance.

### Pilot closure (operator, at `pilot_end_date`)

1. Pause backup uploads and state-changing instance workflows under the instance lock. Keep uploads
   paused until all following steps pass. Schedule this Owner run at pilot end; `ph-ops` alerts on
   failure or an overdue closure, rather than treating a changed account default as success.
2. Run the reviewed full trust apply with the real pilot end date, setting the default to Locked and
   locking the fallback container policy if enabled. Azure only creates account policies in Unlocked
   or Disabled state. If initializing a **new** backup account after the intended end date, first
   initialize trust with a temporary future `pilot_end_date` while uploads remain paused, then restore
   the intended date and apply again. Plan-guard rejects new accounts with a Locked creation policy.
3. Temporarily grant the operator Storage Blob Data Owner at the **unlocked vault RG** using the
   bootstrap assignment ledger, wait for propagation, and run:

   ```sh
   python3 hosting/paperclip-hermes/tools/lock-backup-versions.py \
     --account "$backup_account" --pilot-end "$pilot_end_date"
   python3 hosting/paperclip-hermes/tools/lock-backup-versions.py \
     --account "$backup_account" --pilot-end "$pilot_end_date" --apply
   ```

   The first command counts candidates; the second uses the operator's `az` Entra session to enumerate
   every page and version in the container, including older versions of current names. It locks each
   unexpired Unlocked version with its exact version ID and unchanged retention-until date. Expired
   policies need no lock; already Locked versions are skipped. It rereads all pages and fails if any
   unexpired Unlocked version remains. It never extends expired backups or uses Shared Key/SAS.
   This temporary operator grant reaches blob data across that RG; it is not a workflow grant.
   Account-level `CanNotDelete` locks also block deletion of container-scoped RBAC assignments.
   An assignment on the unlocked parent RG can be removed while all storage account locks remain
   intact. Verify there is no inherited parent lock before creating the temporary assignment.
4. Record the successful count and complete inventory verification in the private closure receipt.
   Remove the temporary role on success or failure and verify removal. On partial failure, keep
   uploads paused, alert, reacquire the temporary role and rerun; locking is idempotent. Resume only
   after verified closure. Workflow identities receive no new immutability rights.

The data-plane operation is [Set Blob Immutability Policy](https://learn.microsoft.com/en-us/rest/api/storageservices/set-blob-immutability-policy),
using [version and immutability inventory](https://learn.microsoft.com/en-us/rest/api/storageservices/list-blobs).
Offline tests cover pagination, versions, expiry preservation, expired/Locked skips, authorization
failure, missing policy data, early invocation, partial closure and reruns. Live Azure acceptance
and the future bootstrap/scheduler integration remain deployment gates.

See the [review and self-audit record](review-audit.md) for pinned-source evidence and regression coverage.

## Local checks and static-analysis exceptions

Run `bash ci/check.sh` from the repository root using the README's installed tools. Terraform tests
are shared under `terraform/tests`, linked into each root's local `tests` directory. They use mocks
only; no cloud credentials are required. CI also inspects verbose test-plan JSON for forbidden secret
resources, PEM material, network intermediaries, inbound allows and lifecycle rules. Production plan
JSON and test logs must stay private in instance workflows.

Checkov scans committed code, excluding downloaded `.terraform` examples. Narrow inline exceptions
are attached to the exact affected resources: ADR-mandated exact registry releases instead of Git
sources, platform-managed encryption instead of extra disk/storage keys, public Entra service endpoints
for hosted runners, the ADR choice to leave optional diagnostic sinks to adopter infrastructure. None suppresses
the Shared Key, purge protection, retention, role scope or inbound-network invariants; mock tests check
those controls against the resolved resources.

## Budget creation and maintenance

Budget start dates use the current apply month's first day via `timestamp()`, with
`ignore_changes` preserving the existing start on ordinary refresh. Terraform evaluates the
configured start during apply for a new/replaced budget (including approval across a month boundary), so a later target or recreation needs no stale input.
There is no `budget_start` input. AzureRM 4.81.0 replaces the subscription budget when switching
`billing_month_verified` from Monthly to BillingMonth; review that temporary protection gap during
the Owner's self-test 7 rollout. RG budgets round down to whole pounds (minimum five).
Monetary alerts allow half a penny of rounding tolerance plus floating-point slack at 80%/85%; the 100% override remains exact.

The deallocator rereads VM tags before stopping. Only a valid future maintenance deadline within
two hours suppresses the lower thresholds. A farther-future or malformed tag deallocates; a failed
`ticks()` scope has an explicit failure handler. A separate outer 100% branch bypasses tag parsing.
All ARM HTTP actions use the managed identity with audience `https://management.azure.com/`.
