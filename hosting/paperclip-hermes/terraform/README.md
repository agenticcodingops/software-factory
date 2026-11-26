# Terraform deployment contracts (WP4)

These four modules and three root stacks implement the infrastructure in
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
The bootstrap-created state account, `tfstate` container and deletion lock are explicitly imported
by `stacks/trust/main.tf` using AzAPI, which does not persist account keys.
The `tfstate` container, soft delete, versioning, Shared Key prohibition and deletion lock are managed.
No SAS, password or SSH private key is created by these modules.

The trust root has default vault providers and explicit target aliases. `targets` includes every
subscription in the configured target tenant, including targets being added. If the target tenant
differs, target-facing identities are duplicated there and receive only target grants; vault identities
keep vault grants. Twins live in a separate owner-controlled `rg-<prefix>-trust`, outside the workload
Contributor boundary. More than one foreign tenant requires an operator-reviewed root composition
with an explicit alias per tenant; Terraform cannot select provider aliases dynamically. Do not apply
separate copies of the vault resources to independent states.

`rg_managed_by=trust` references the existing workload RG; `host` creates it for subscription scope.
Before the first host apply in subscription scope, the operator must establish the RG-scoped grants
and verify creation authorization through bootstrap. No workflow gets Owner or unrestricted RBAC.
Deploy can assign only `SF VM Deallocator` to service principals; its ABAC condition constrains both
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
The `reviewed_fragment` output is a patch for the next operator-reviewed hardening PR; Terraform
does not silently replace or narrow the imported policy. It renders tag owners, the operator grant,
SSH check mode, operator accept tests, pilot outbound denies and another-user denies.
`isolation_destinations` must sample every other tag/user/host:port in the actual imported policy.

Run `tools/policy-lint.py POLICY --operator LOGIN` before any hardening or later plan/apply.
It parses HuJSON, expands groups and host aliases, checks every ACL/grant and protects SSH check mode.
The sole report-only exception requires `--baseline-plan PLAN.json --live-policy LIVE.hujson` and
`--operation plan`: exact live policy bytes, no policy/settings resource diff, no unrelated changes.
It lists unsafe findings without authorizing apply or join. Live reads and the no-diff plan must come
from the trusted read-only workflow, not PR-supplied artifacts. A label/input alone is insufficient.
`hardening_applied` is an attestation from the approved workflow's applied-policy receipt, not an
operator bypass. Workflows must verify that receipt and enforcing lint before setting it. The module
rejects joining from a baseline or without this evidence. Explicit `join_generation` replaces keys;
VM replacements do not. Keys are single-use and expire after 3,600 seconds, and travel only encrypted.

## Upgrade and disk contracts

Pass the four current protocol tags from ARM and the original bootstrap `custom_data` from the receipt.
A new VM has no protocol tags. The `expires` tag uses `time_static` plus a 30-day `time_offset`.
Shutdown defaults to 22:00 and backup to 21:15; invalid times or a smaller gap fail planning.
`tools/plan-guard.py PLAN.json` rejects protected deletions/replacements and retention/lifecycle changes.
`--override-replace` never bypasses protocol tag changes or changed/unknown existing VM bootstrap data.
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
The optional container policy is enabled only by failed inheritance self-test 3. Both storage accounts
have Shared Key disabled, TLS 1.2, private containers and deletion locks. Data roles use container scopes;
user-delegation roles use the backup account scope. Deploy has no backup/key rights. Operator custody
grants no workflow any key access; vault custody grants only restore the read-only Key Vault Secrets User.

Azure lifecycle filters include the container name: `backups/backups/` means encrypted objects under
`backups/` in the `backups` container, excluding long-lived `receipts/`. Both base blobs and versions
expire after 45 days. Staging base blobs/versions use `restore-staging/` and one day. The imported kit's
upload path must be migrated to that object prefix by the runtime package before deployment is enabled.
Mock plan inspection verifies both actions and both prefix filters.

RG budgets are `max(5, min(cap, headroom))`, with the approved override using the cap. Alerts are
50% actual email, 80% actual deallocation, 100% forecast email. The credit budget uses 70/85/100;
`billing_month_verified=false` selects Monthly and **email only** until self-test 7 passes.
Email and deallocation action groups are separate, so email thresholds cannot stop a VM.
The Consumption Logic App and its SF VM Deallocator assignment are in the target RG and its POST
URI names only the pilot VM. It checks budget thresholds and the maintenance tag before deallocating;
100% is the emergency hard threshold. The webhook uses Entra authentication, not a SAS in state.
`alert_application` describes an operator-provisioned secure-webhook application and Monitor sender.
Its unsigned runtime invoke URL comes from Azure
`properties.accessEndpoint`; no `listCallbackUrl` call or signed URL enters state. Live webhook delivery, payloads, maintenance behavior and forecast notifications
must pass self-test before budgets authorize deallocation. The authoritative real-billing-period cost
workflow remains a later package. Code never changes the subscription's spending limit.

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
