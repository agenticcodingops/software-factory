# WP4 review and self-audit

Audited on 2026-11-26 against ADR 0001 and the GitHub source at **each pinned release**, including
every root `variables*.tf`, nested variable defaults, locals and resource definitions used by these
calls. Downloaded upstream sources were kept outside the repository. No module/provider pin changed.
Live Azure and Tailscale acceptance still requires the later instance/bootstrap/workflow packages.

## Pinned AVM default audit

| Module | Source inspected | Defaults and resolved ADR contract |
| --- | --- | --- |
| Resource group 0.5.0 | [variables](https://github.com/Azure/terraform-azurerm-avm-res-resources-resourcegroup/blob/v0.5.0/variables.tf), [resources](https://github.com/Azure/terraform-azurerm-avm-res-resources-resourcegroup/blob/v0.5.0/main.tf) | AzAPI RG, full-map tags (`ignore_tag_changes=false`), no implicit lock; telemetry explicitly off. Parent deletion cascades despite child `prevent_destroy`: fixed guard coverage. |
| Identity 0.6.0 | [variables](https://github.com/Azure/terraform-azurerm-avm-res-managedidentity-userassignedidentity/blob/v0.6.0/variables.tf), [resources](https://github.com/Azure/terraform-azurerm-avm-res-managedidentity-userassignedidentity/blob/v0.6.0/main.tf) | No implicit federations/roles, full-map tags, no lock, no implicit isolation scope. Exact workflow subjects, conditions and outbound scopes are explicit; role assignments wait for storage and workload RGs. |
| Storage 0.11.0 | [account variables](https://github.com/Azure/terraform-azurerm-avm-res-storage-storageaccount/blob/v0.11.0/variables.storageaccount.tf), [other variable files](https://github.com/Azure/terraform-azurerm-avm-res-storage-storageaccount/tree/v0.11.0), [account resource](https://github.com/Azure/terraform-azurerm-avm-res-storage-storageaccount/blob/v0.11.0/main.tf) | `Standard_ZRS` overrides deprecated replication input; public networking defaults false and network rules default Deny. Fixed all three calls to explicit `Standard_GRS`, public access and Allow rules. Shared Key/anonymous/local-user/SFTP/NFS access remain off, OAuth/TLS/versioning explicit. Private endpoints/identities/CMKs default absent. Containers default private. Blob-service properties settle before containers. |
| VNet 0.23.0 | [variables](https://github.com/Azure/terraform-azurerm-avm-res-network-virtualnetwork/blob/v0.23.0/variables.tf), [subnet variables](https://github.com/Azure/terraform-azurerm-avm-res-network-virtualnetwork/blob/v0.23.0/modules/subnet/variables.tf) | Address space/subnets explicit, no implicit peering or encryption policy, no DNS override. Explicit subnet default outbound=false and NSG; RG dependency already present and tested. |
| NSG 0.7.0 | [variables](https://github.com/Azure/terraform-azurerm-avm-res-network-networksecuritygroup/blob/v0.7.0/variables.tf), [rule variables](https://github.com/Azure/terraform-azurerm-avm-res-network-networksecuritygroup/blob/v0.7.0/variables.security_rule.tf) | No custom rules by default; explicit priority-100 inbound deny supersedes Azure default inbound allows. No additional inbound paths or intermediaries. |
| Public IP 0.3.0 | [variables](https://github.com/Azure/terraform-azurerm-avm-res-network-publicipaddress/blob/v0.3.0/variables.tf) | Static, IPv4, Standard, Regional, zone-redundant `[1,2,3]`, no DNS label, inherited DDoS policy. Static/IPv4/Standard are explicit and tested; exactly one IP provides egress. |
| VM 0.22.1 | [variables](https://github.com/Azure/terraform-azurerm-avm-res-compute-virtualmachine/blob/v0.22.1/variables.tf), [VM body](https://github.com/Azure/terraform-azurerm-avm-res-compute-virtualmachine/blob/v0.22.1/locals.vm_common.tf), [Linux resource](https://github.com/Azure/terraform-azurerm-avm-res-compute-virtualmachine/blob/v0.22.1/main.linux_vm.tf) | Windows/image/size, generated credentials, Premium OS disk and boot diagnostics=false defaults are overridden. Linux Ubuntu 24.04, B-series size, SSH-only/no generation, no identities, secure boot/vTPM/encryption at host, StandardSSD, boot diagnostics and daily shutdown are explicit. Existing data disks use `Detach`; added guard against changing that setting. Full-map VM tags caused maintenance removal: fixed pass-through. Cloud-init is write-only; fingerprint guards remain. |
| Action group 0.3.0 | [variables](https://github.com/Azure/terraform-azurerm-avm-res-insights-actiongroup/blob/v0.3.0/variables.tf), [receiver variables](https://github.com/Azure/terraform-azurerm-avm-res-insights-actiongroup/blob/v0.3.0/variables.receivers.tf) | Enabled=true, location explicitly global; empty receiver defaults are filled with separate email and Entra-authenticated webhook receivers. No signed callback URLs. Actual100 routing fixed in both budgets, with subscription verification retained. |
| Logic workflow 0.2.0 | [variables](https://github.com/Azure/terraform-azurerm-avm-res-logic-workflow/blob/v0.2.0/variables.tf), [resource](https://github.com/Azure/terraform-azurerm-avm-res-logic-workflow/blob/v0.2.0/main.tf) | Enabled workflow, empty identities/access/definition defaults are overridden with system identity, AAD sender restrictions and explicit HTTP actions. Added immediate tag reread before POST; no POST retries. Residual race documented, no lease claimed. |

Optional diagnostic sinks remain adopter infrastructure as before; telemetry is off in every call.
The disk fallback and Key Vault resources were also checked: literal disk protection, private disk
network access, platform encryption, RBAC, purge protection and 90-day Key Vault soft delete remain.

## Ownership, external writes and first apply

- **Bootstrap overlap:** the state account implies an existing parent vault RG. Added its import
  alongside the account, container and named lock. The bootstrap does not exist in WP4; the documented
  inventory is the contract checked here, not a claim of having executed a bootstrap.
- **Custom roles:** all four display names now include the prefix. References remain resource IDs;
  ABAC does not depend on display-name matching. The second-prefix Terraform run verifies actual names.
- **External tags:** audited all full-map resources against ADR writers. The VM's four approval/delivery
  tags and `sf-maintenance-until` are the external tag writes. Added maintenance to the ARM input allowlist,
  excluded it from ordinary tags, verified exact deadline preservation and new-host absence, and retained
  plan-guard checks for changed/removed/unknown protocol tags. The stable Terraform-owned expiry remains.
  No external identity, RG, network, storage or Key Vault tag writer is prescribed by the ADR.
- **External settings:** tailnet baseline still copies live policy/settings/DNS and performs no apply;
  enforcing hardening owns subsequent policy/settings. Break-glass requires a fresh baseline. Individual
  blob WORM locks do not conflict with Terraform's account/container default-policy ownership.
- **RG ordering:** checked both trust-owned and host-owned modes across subscription providers. Trust-owned
  modules already order RGs before grants; host-owned groups were not checked at all. Added an existing-RG
  read and readiness dependency before grants, with an Owner-only host-state RG initialization sequence.
  No second owner or subscription-wide Contributor grant was introduced.
- **Host ordering:** disk, NSG, VNet and public IP wait for the created/read RG; subnet waits for NSG;
  VM waits for the VNet/subnet and references its disk, NSG and public IP. Guardrails wait for host.
  Within guardrails, budgets now additionally wait for the deallocator role assignment so a first alert
  cannot arrive before its permission is assigned. RBAC propagation and webhook delivery remain live tests.
- **Provider registration:** checked the pinned [AzureRM 4.81.0 provider defaults](https://github.com/hashicorp/terraform-provider-azurerm/blob/v4.81.0/website/docs/index.html.markdown)
  and [AzAPI 2.14.0 provider defaults](https://github.com/Azure/terraform-provider-azapi/blob/v2.14.0/docs/index.md).
  AzureRM defaults to automatic `legacy` registration; AzAPI defaults to not skipping it. Disabled
  both on every root and generated subscription alias, matching the ADR's Owner bootstrap registration
  step. Plan/RG-scoped identities must not need subscription registration writes. Regression coverage
  checks every root and generated alias; live bootstrap registration remains a prerequisite.
- **New WORM policy:** upstream documents that a policy cannot be created Locked. Documented the two-apply
  initialization when a new account's intended pilot end is already past (uploads paused, initialize
  Unlocked with a temporary future date, restore intended date and lock). Plan-guard rejects the invalid
  new-account Locked plan. Existing account transitions remain Owner-reviewed protection changes.
- **Pilot-era versions:** a Locked account default is not retroactive. Added the operator closure tool
  and ADR procedure, including paused uploads, temporary operator Data Owner on the unlocked vault RG, paginated version
  inventory, unchanged expiries, verification, retry/receipt and privilege cleanup. No workflow gains
  permission to change immutability.
- **Temporary role cleanup:** account deletion locks also prevent removing child-scoped RBAC grants.
  The closure procedure places its temporary operator grant on the unlocked parent vault RG, with
  explicit temporary access to that RG's blob data, so cleanup preserves every account lock. The ADR
  bootstrap/migration contract now applies this rule to temporary operator grants generally; persistent
  workflow data permissions stay container-scoped. This follows Azure's documented
  [lock inheritance and RBAC constraints](https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/lock-resources).

## Policy and plan bypass audit

- Checked both sides of grants and ACLs, SSH, nested groups, host aliases, cycles, wildcard selectors,
  tags, member/role/self/internet/shared autogroups, IP sets, IPv4 ranges and IPv6/bracketed ACL destinations
  (including IPv4-mapped IPv6).
  Fixed IP-set references on either side, including indirect references. Added `autogroup:danger-all`
  coverage and case-insensitive tag handling. Unknown selector types fail closed, including service
  selectors until their effective destinations can be resolved. Host aliases cannot shadow reserved
  selectors; alias/group definitions cannot collide after case normalization. Omitting grants and ACLs
  fails instead of inheriting allow-all. SSH pilot sources are now checked as well as destinations.
  User-owned member/role/self/internet selectors still do not imply the tagged pilot; direct operator
  grants and operator-only groups remain valid. Existing CIDR overlap, alias-cycle and IPv6 tests remain.
- Report-only baseline plans now reject explicit `complete:false`, preserving complete no-diff baselines.
  Tests still reject apply/join, policy-byte changes, unrelated resources and join-generation markers.
- Audited AzureRM and AzAPI delete/replacement/forget actions, both replacement orders, no-op children,
  ARM type changes, unknown types and VM embedded disk settings. Added protected RG/subscription/blob
  types, checking the old ARM kind as well as the new one. RG deletion with a no-op data disk is rejected
  even with the VM-delete override. A VM-only delete cannot first set data disk `deleteOption=Delete`.
  Key Vault/secret deletion is also protected to preserve decryptability. Added rejection of nested ARM
  deployment mutations: Complete mode or deployment teardown can delete children without child plans;
  these stacks do not require nested deployments.
  New lifecycle policies also require Owner review: adding a policy can expire existing versions
  without a Terraform blob deletion. This is intentionally flagged during initial trust provisioning too.
  Existing lifecycle, immutability, cloud-init fingerprint and protocol-tag guards still reject bypasses.
- Audited both renderers: copied symlinks can escape through parents as well as files. Host rendering
  now resolves and checks template containment before any read/write. The same fix protects trust's
  generated composition files. Tests exercise absolute parent/file symlinks, unchanged outside files,
  cleanup, and ordinary copies that preserve the disk address and protection literals.
- Checked the other tools (`check-budget-alerts`, `check-mock-plans`, workflow/tree/deny-list checks and
  repository file enumeration) against their ADR responsibilities. Extended budget expression evaluation
  to the recheck body; actual resolved storage, budgets and roles are tested in Terraform plans. The
  repository checks and their security boundaries remain covered by their existing regression suites.

## Regression evidence

- `tests/trust.tftest.hcl`: all three actual account SKUs/network/auth settings, default/fallback WORM,
  lifecycle/storage isolation, second-prefix role names, host-owned RG reads, tenant/subscription scopes.
- `tests/host.tftest.hcl`: exact live maintenance/protocol values, new-host absence, four notifications
  per budget, distinct email/deallocator destinations and actual100 subscription email-only fallback.
- `tests/tailnet.tftest.hcl`: real non-operator source, operator acceptance unchanged, null-source omission,
  rejected operator/autogroup sources, complete no-diff baseline and no baseline join.
- `tools/tests/test_review_regressions.py`: review reproductions, indirect deletion and selector bypasses,
  missing/completed baseline plans, renderer escapes, immediate maintenance recheck, pilot closure with
  pagination/version IDs, preserved expiry, failure handling and idempotent reruns.
- Existing guard, repository, runtime, backup/restore, workflow and Terraform mock-plan suites remain
  part of `bash ci/check.sh`. This record is accompanied by the final check output in the task response.
