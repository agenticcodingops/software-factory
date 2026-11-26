# ADR 0001: Deploying Paperclip and Hermes to Azure (revision 3)

- **Status:** Proposed, 2026-11-26. Revision 3 addresses the deployment, recovery and authorization review findings on revision 2.
- **Applies to:** the proposed `hosting/paperclip-hermes/` in `software-factory`; that directory and its implementation are absent at this revision.
- **Supersedes:** the portal steps of the original pilot runbook, and revisions 1–2 of this ADR.
- **Notation:** "[V]" marks a fact verified on 2026-11-26 against a primary source. "Open" marks something that still has to be tested; each Open item has a self-test or acceptance test, and either a documented fallback or, where stated, it blocks release.

## Context

Paperclip is an agent control plane and Hermes is an agent gateway. This ADR proposes running them together as a hardened Docker Compose kit on one Linux VM. Work package WP2 imports the kit into `hosting/paperclip-hermes/kit/` as one commit whose message records the private source repository and commit. That import and this repository's control-test CI are not present at this revision. All six are **Open, not implemented or verified in this repository**:

| Proposed control | Acceptance test before a deployment release |
|---|---|
| Pinned image digests | Reject every Compose image or Dockerfile base without a digest; inspect the running image IDs against the release manifest. |
| No public inbound ports | Terraform NSG assertions plus the external scan in self-test 9. |
| Containers blocked from IMDS and WireServer | Container probes to both endpoints fail and increment the host DROP counters. |
| No VM managed identity | Assert empty managed identities in both the plan and the deployed VM's ARM response. |
| Encrypted nightly backup with a write-only upload credential | Run the timer, decrypt and restore its blob in a drill, and prove the upload SAS cannot read, list or delete. |
| Fenced restore with messaging off | Restore a seeded backup and assert that no messaging service starts or sends until an explicit unfence. |

The repository layout, scripts, workflows and tests below are implementation requirements. WP2 imports the kit into `hosting/paperclip-hermes/kit/` as one commit whose message records the private source repository and commit. Evidence for every control above comes from this repository's own unit tests and `kit-e2e` CI, which must exist and pass before release. The controls remain Open until that CI exists and supplies passing evidence. Until then, bootstrap and deployment are unavailable; there is no deployable fallback.

This ADR specifies a reusable deployment for adopters whose private repositories can enforce the required approvals. An adopter connects GitHub to an Azure subscription, runs one bootstrap script once, and then runs workflows. They get Paperclip, Hermes or both. Day-2 work is automated: upgrades, backups, credential rotation, cost guards and subscription moves.

**Constraints that shape the design:**

- **Release pace.** Paperclip ships a stable CalVer release every 1–2 weeks. Hermes ships 1–2 semver releases a week, plus daily canaries [V]. Paperclip v2026.1117.0 is declared a "rollback floor": after its migrations, an older image cannot run against the database [V]. **So rollback must restore data, not only swap the image.**
- **Spending limit.** On a subscription with a spending limit, running out of credit stops and deallocates every VM and makes storage read-only until the next billing period [V]. **So backups and Terraform state must not live in that subscription by default.**
- **Budget data lag.** Cost data lags 8–24 hours and can lag up to about 72 hours. For Visual Studio offers it is a retail estimate with no credits applied [V]. **So a budget alert is a coarse guard, not a precise one.**
- **GitHub OIDC subjects.** Repositories created after 2026-07-15 use immutable OIDC subjects (`repo:<owner>@<id>/<repo>@<id>:…`) [V]. An Azure identity holds at most 20 federated credentials, and they accept no wildcards [V]. A reusable workflow's `sub` describes the caller, not the called workflow [V].
- **Tailscale policy file.** `tailscale_acl` overwrites the whole tailnet policy file. It must be imported first [V].
- **Run command.** It needs a running VM, and whether it echoes protected parameters is unverified (Open). **So no plaintext secret ever travels through it** (decision 9).

## Decisions

1. **Public repository plus private instance repository.**
   - The public repo holds the modules, root stacks with their lock files, the kit, the reusable workflows, the bootstrap script, a Renovate preset, the merge-gate workflow and the docs.
   - Each adopter creates a private instance repo from `templates/instance/`. It holds only settings, the tailnet policy, thin caller workflows and run history.
   - *Why:* strangers write no Terraform; personal IDs never reach the public repo; upgrading means bumping one ref.
   - Private instance repos require **GitHub Enterprise** with required environment reviewers. Free, Pro and Team are unsupported for this approval model [V]; see [GitHub's reviewer availability](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/review-deployments). Operator merges alone do not replace deployment approval gates.

2. **Three failure boundaries, with a single-subscription mode for small adopters.**
   - The *target* subscription runs the VM.
   - The *vault* holds backups, Terraform state, the Key Vault and the identities. It survives the loss of the target.
   - The *tailnet* is the third boundary.
   - `vault_mode = separate-subscription` is the default. `same-subscription` puts the vault in its own resource group in the target subscription. The bootstrap refuses that mode when the target's `subscriptionPolicies.spendingLimit` is `On`, unless `--accept-vault-risk` is passed, and the quickstart documents the risk of losing backups with the subscription.
   - *Why:* losing the target subscription, or having it disabled by its spending limit, leaves backups, state and the ability to move intact. Adopters with one subscription still have a path.

3. **Privileged resources are applied only with the operator's Owner login, through the reviewed bootstrap script.**
   - `sf-bootstrap.sh` creates the state storage account with `az`. This is the only resource made outside Terraform, because of the chicken-and-egg problem with the backend.
   - It then applies the **`trust` stack** under the operator's own login. That stack is Terraform in the public repo and holds:
     - identities, federated credentials, custom roles and role assignments;
     - the workload resource group (in resource-group scope mode);
     - the backup storage account, with its WORM policy, lifecycle rules and locks;
     - the Key Vault.
   - The state account is imported into the stack, so everything is IaC with drift detection.
   - No workflow identity can apply `trust`. `ph-plan` plans it read-only every night. A pending diff, such as an AVM bump in a new release, opens an `ops-alert` asking the operator to run `sf-bootstrap.sh trust --apply`.
   - *Why:* a deploy identity must never be able to widen its own rights or delete backups. The cost is an operator run only when the trust stack changes, which is rare.

4. **Resource-group-scoped trust by default.**
   - In resource-group scope mode, the `trust` stack owns the empty workload RG, and the `host` stack references it with a data source (`var.rg_managed_by = "trust"`). No `import` block is needed.
   - With `--scope subscription`, the `host` stack creates the RG (`rg_managed_by = "host"`).
   - The deploy identity gets Contributor on that RG only, plus Cost Management Contributor at subscription scope for the subscription budget.

5. **No stored cloud secret anywhere.**
   - Azure uses GitHub OIDC with user-assigned identities.
   - Tailscale uses workload identity federation (`oauth_client_id` plus `audience`) [V].
   - All IDs are non-secret configuration in the private instance repo.
   - *Why:* nothing in GitHub can be leaked and replayed.

6. **Terraform state holds no long-lived secret.**
   - No `random_password`, no `tls_private_key` and no SAS goes into state.
   - The VM module is called with `generate_admin_password_or_ssh_key = false` and a public key only.
   - The only secret in state is the Tailscale join key: single-use, preauthorized, and expiring after 3,600 s [V].
   - `terraform test` asserts that the plan contains no `tls_private_key` or `random_password` resource, and CI greps the mocked plan JSON for `private_key_pem`.

7. **The upload credential is a user-delegation SAS (amends the earlier stored-policy decision).**
   - Scope: `sr=c`, `sp=cw`, valid for at most 7 days, minted with OIDC [V].
   - Shared Key is off on every storage account.
   - The delegation-key action (`generateUserDelegationKey`) is granted **at storage-account scope**, because it is an account-level action. Data actions are granted at container scope. The SAS grants only the intersection, so it can neither read, list nor delete [V].
   - Stored-policy SAS credentials are unsupported. They require a service SAS, which [Azure rejects when Shared Key is off](https://learn.microsoft.com/en-us/azure/storage/common/shared-key-authorization-prevent); [stored policies cannot bind a user-delegation SAS](https://learn.microsoft.com/en-us/rest/api/storageservices/define-stored-access-policy) [V]. If delegation minting fails, keep the existing unexpired delegation SAS, alert and retry; once it expires, refuse operations requiring a fresh off-site backup. Never enable Shared Key as a fallback.

8. **Custody of the `age` key: the long-term private key never reaches the VM or a workflow by default.**
   - The key pair is generated on the operator's device by `sf-bootstrap.sh age-key`, and the private key is never written to disk.
   - `custody = operator` is the default and is recommended. The private key lives only in the operator's password manager.
     - Restores use **per-restore re-encryption**. The restore host generates an ephemeral age key in `/run` and returns only its public recipient. On the operator's device, `sf restore-assist` streams the chosen blob through `age -d` (with the long-term key) and `age -r <ephemeral>` into a `restore-staging` container.
     - The host restores from that blob and shreds its ephemeral key.
     - The long-term key never touches the VM, ARM or GitHub.
   - `custody = vault` (opt-in) stores the key as a Key Vault secret version that only the approval-gated restore identity can read. The same re-encryption then runs on a GitHub-hosted runner, so the plaintext key and the plaintext stream are briefly in runner memory. The run requires the input `accept_runner_exposure=true` on top of the `restore` approval, and the ADR states this deviation explicitly.
   - Every blob carries the recipient fingerprint as non-secret metadata.
     - Superseded keys are kept as password-manager entries or as **enabled** Key Vault secret versions readable only by the gated restore identity and the operator during authorized custody operations. Rotation stops using them for new backups; it does not disable or expire them while a retained backup needs them. [Disabled secret versions cannot be read, even by version ID](https://learn.microsoft.com/en-us/azure/key-vault/secrets/javascript-developer-guide-enable-disable-secret) [V].
     - Retire a key only after the last backup using it has passed both its retention and lifecycle period (45 days), and inventory confirms no retained blob, version or migration copy still needs it.
     - The restore picks the key by fingerprint, resolves the recorded exact secret version URI and verifies that its derived recipient matches. An unknown fingerprint or unreadable version stops the restore with an operator alert; the restore identity cannot change secret properties.
   - Rotation is no longer needed after a restore, because the long-term key never left custody. It is a scheduled or on-suspicion action (see "Backups and key custody", "Keys").

9. **Commands reach the VM through managed run command, and machine secrets travel only as envelope-encrypted ciphertext.**
   - On first boot, cloud-init generates a host **delivery key pair**: an age X25519 key in `/etc/paperclip-hermes/delivery.key`, root, mode 0600, never included in backups.
   - `ph-deploy` reads the public recipient through run command and records it as the VM tag `sf-delivery-recipient`. Only the deploy identity can write tags.
   - Workflows encrypt the SAS and the join key to that recipient and pass only ciphertext, as named parameters. `sfctl` decrypts on the host.
   - A recipient that no longer matches the tag raises an alert, and nothing is delivered.
   - Every workflow deletes its run-command resources when it finishes.
   - *Why:* CI never joins the tailnet, so "only the operator reaches the VM" stays literally true. If run command echoes parameters in GET, the Activity Log or `/var/lib/waagent`, only ciphertext is exposed. Self-test 1 becomes defence in depth instead of a blocker, and no weekly manual fallback exists.
   - Human secrets never use this path (decision 12).

10. **Upgrades are detected by Renovate and deployed in a fixed order: off-site backup → Terraform apply → kit upgrade with a local rollback point → health check → automatic rollback.**
    - Rollback restores the local plaintext rollback point (a database dump plus the volumes) after checking out the previous kit commit.
    - Terraform changes cannot be rolled back automatically. Destructive plans are therefore refused before apply (see "Upgrades").
    - Renovate rather than Dependabot, because it can share one preset across instance repos, update tag@digest pins and refresh lock files, and its Terraform support covers versions beyond 1.15 [V].

11. **Approval model and merge gate.**
    - **Environments.**
      - `production` and `restore` need the operator's approval and are limited to `main`.
      - `ops` is unattended and limited to `main`.
      - `plan` is unattended with **no branch restriction**, because its identities are read-only, so PR plans work.
    - **Self-review.** In single-operator mode, `init` leaves "prevent self-review" **off**, because GitHub would otherwise block the operator from approving any run they triggered. It is turned on only when `--reviewer` lists two or more people. The documented convention is that agents, not the operator, trigger the runs the operator approves.
    - **CODEOWNERS** in the instance repo:
      - The operator owns `.github/**`, `config/**` and `config/tailnet/**`.
      - The callers `.github/workflows/ph-*.yml` have **no code owner**. They are protected by `sf-merge-gate` instead.
    - **`sf-merge-gate`** is an **organisation ruleset "required workflow"**. It comes from a repo the PR cannot change, at a SHA pinned in the ruleset; for agenticcodingops that is `software-factory` at a release SHA. The gate:
      - passes a PR that touches only `ph-*.yml` callers, where each change is only a `uses: …@<sha> # vX.Y.Z` line pointing to a tagged `software-factory` release;
      - also requires, for an **ops-plane caller** (`ph-ops`, `ph-power`, `ph-cost-guard`, `ph-backup-verify`), that `<sha>` equals the VM tag `sf-approved-sha`, read with the plan identity;
      - for any other change to `ph-*.yml`, requires an approving review from a CODEOWNER.

      A PR that adds a job named like the gate does not satisfy it, because the ruleset binds the workflow file and its source repo, not a check name.
    - **Which code can reach root on the VM.** Renovate bumps only the production-plane callers. Terraform passes through all existing protocol tags unchanged (see "Terraform modules", "Details"); only the approved `ph-deploy` finish step publishes the new `job_workflow_sha`, after kit health and the port scan pass. It first records the previous approved SHA in `sf-recovery-sha` with `sf-recovery-until` (24 h maximum), then writes the new approved SHA and pushes the ops-caller bump. The previous SHA may run only the recovery commands defined under "Upgrades" during that handoff. An agent opens the PR and the gate verifies the new approved SHA. No candidate SHA gains unattended root before a successful approved deploy.
    - **Without org required workflows.** If the Enterprise organisation cannot enforce the required workflow, there is no agent self-merge for `ph-*.yml` changes: the operator merges them. `init` detects this separately from deployment reviewer protection, which remains mandatory.
    - **Two-bot rule in the instance repo.** Each adopter decides; the recommended default is that a gate-verified ref bump is a mechanical carve-out, because the referenced code passed the two-bot rule in the public repo. Every other instance PR needs two bots or the operator. The template ships an optional `claude.yml` for adopters who want bot review there.

12. **Human secrets go over Tailscale SSH in check mode.**
    - Interactive form: `tailscale ssh -t admin@<host> sudo sfctl secret set <name>`. The `-t` allocates a TTY so the prompt does not echo.
    - Piped form: `<pm-cli read …> | tailscale ssh admin@<host> sudo sfctl secret set <name> --stdin`. `sfctl` refuses a non-TTY stdin without `--stdin`.
    - `sfctl` writes atomic 0600 files into the kit's `.secrets/` directory under names listed in `SECRET_FILES`, so backups carry them, encrypted. It then regenerates `compose.env` or the `*_FILE` mounts so each secret reaches only its service.
    - For a GitHub token, `sfctl` calls `GET https://api.github.com/` from the host and records the `github-authentication-token-expiration` header as the real expiry.
    - *Why:* the secret goes straight from the operator's device to the file over WireGuard, and check mode forces a fresh sign-in.

13. **Cost guards are layered and sized to real headroom and to the real billing period.**
    - **Headroom** = the monthly credit minus the forecast spend of the *other* resources in the target subscription, over the billing period set by `billing_period_start_day`.
    - Deploy refuses when the headroom is less than one week of the chosen run hours, unless `override_cost` is set.
    - The other layers:
      - auto-shutdown;
      - an enforced daily run time;
      - a 14-hour uptime cap;
      - budget-driven deallocation;
      - `ph-cost-guard`, which is authoritative;
      - an approval-gated **park** tier for when deallocation is not enough.

14. **App selection.**
    - `apps = ["paperclip","hermes"]`, or either one alone, maps to Compose profiles.
    - The default size is `Standard_B4as_v2` for both apps and `Standard_B2as_v2` for one.
    - `vm_size`, `location` and the subscription are per-target settings.

15. **One host at a time, one operation at a time.**
    - All VM-touching operations share the GitHub concurrency group `sf-host-<target>` with `cancel-in-progress: false` and `queue: max`. [GitHub retains up to 100 pending entries and cancels additional arrivals](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency) [V]. Hold the group once for the whole operation; nested jobs must not acquire the same group and deadlock their parent. Recheck state and shutdown deadlines when execution starts, since dispatch order is not guaranteed.
    - A separate `workflow_run` completion monitor, outside the host group and with no VM credentials, alerts on cancelled host runs with a run link and the requested operation. The scheduled monitor also reconciles requested stops against completed stops, surfaces queue overflow in the Status issue and retries an outstanding stop once capacity is available. A cancelled stop must never be reported as completed.
    - Acceptance (Open): hold a restore, queue a stop, then queue ops; both pending operations remain and the stop eventually runs. Fill all 100 pending slots and assert the extra cancelled run produces an alert and an outstanding stop is retried. Until this passes, unattended host operations are not released.
    - On the host, `upgrade.sh`, `backup.sh` and `restore.sh` hold `/run/paperclip-hermes/op.lock` and write `last-op.json`.
    - While a maintenance tag (`sf-maintenance-until`, hard cap 2 h) is unexpired or an operation holds the lock, every deallocate path waits up to 20 minutes and skips. The exceptions are the Logic App, but only at its hard threshold, and the expiry of the 2 h cap.
    - The next `ph-ops` run detects an interrupted upgrade from `last-op.json` and runs the rollback branch.

## Architecture

```mermaid
flowchart LR
  subgraph GH[GitHub]
    PUB[software-factory public<br/>modules, stacks, kit,<br/>reusable workflows, preset, merge gate]
    INST[instance repo private<br/>config/targets/*.json, instance.tfvars.json,<br/>policy.hujson, caller workflows]
    INST -- "uses: …@sha" --> PUB
  end
  subgraph VAULT[Vault subscription or vault RG]
    ST[(tfstate<br/>Shared Key off)]
    BK[(backups<br/>versioning + WORM 30 d)]
    RS[(restore-staging<br/>1-day lifecycle)]
    KV[Key Vault<br/>age identities, vault custody only]
    ID[UAMIs plan, deploy, ops, restore]
    DR[rg-sf-drill]
  end
  subgraph TGT[Target subscription]
    RG[rg-&lt;prefix&gt;: VM no MI, NSG no inbound,<br/>outbound IPv4, data disk,<br/>shutdown schedule, budgets,<br/>action group + Logic App deallocator]
  end
  TS[Tailnet<br/>tag:pcpilot, SSH check]
  OP[Operator device]
  INST -- OIDC --> ID
  INST -- "OIDC WIF (plan, production)" --> TS
  ID -- "ARM + run command (ciphertext only)" --> RG
  RG -- "write-only SAS" --> BK
  OP -- "WireGuard, SSH check" --> TS --> RG
  OP -- "restore-assist (re-encrypt)" --> RS
```

If the vault and the target are in different tenants, the `trust` stack creates a twin of each target-facing identity in the target tenant, with the same GitHub subject. The per-target configuration file names which client ID to use in each tenant. Terraform's `azurerm` backend authenticates with the vault-tenant identity (`use_oidc = true`, `use_azuread_auth = true`).

**Identities.** Each identity has one federated credential per tenant, bound to its environment subject `<sub_claim_prefix>:environment:<env>`. That is 4 per tenant, well under the limit of 20.

| Identity | Env | Target-plane rights | Vault-plane rights | Worst case if its workflow is compromised |
|---|---|---|---|---|
| `id-sf-plan` | `plan` (any branch) | Reader on the workload RG(s); Cost Management Reader | Reader on `rg-sf-vault`; Blob Data Reader on `tfstate` | Reads configuration and state; the only state secret is an expired, single-use join key |
| `id-sf-deploy` | `production` (approval) | Contributor on the workload RG; Cost Management Contributor on the subscription; RBAC Administrator on the RG with an ABAC condition that allows only assigning `SF VM Deallocator` to service principals | Blob Data Contributor on `tfstate`; Contributor on `rg-sf-drill` | Changes the workload RG, after approval. **No rights on backups, the Key Vault or other identities** |
| `id-sf-ops` | `ops` | `SF Host Operator` on the workload RG (VM read, start, deallocate, `runCommands/*`; **no** VM write, **no** `tags/write`) | `SF Backup Writer` on the `backups` container (blob add, create, write; no delete); Blob Data Reader on `backups` (ciphertext and metadata, for verification); `SF Delegation Key` on the backup **account** (`generateUserDelegationKey` only) | Root on the VM through run command at `sf-approved-sha`; the previous approved SHA has a time-limited recovery path during handoff; mints a write-only SAS; reads ciphertext, never a key |
| `id-sf-restore` | `restore` (approval) | `SF Host Operator` | Blob Data Reader on `backups`; Blob Data Contributor on `restore-staging`; `SF Delegation Key` on the account; Key Vault Secrets User on the age secrets (vault custody only) | Under operator custody, reads ciphertext only. Under vault custody, can decrypt backups after approval plus `accept_runner_exposure` |

A separate verify identity was considered and rejected. It would trust the same `ops` subject, so splitting it adds no isolation, and the data it reads is ciphertext only.

The operator's own user gets Blob Data Reader on `backups` and Blob Data Contributor on `restore-staging` (for `restore-assist`), assigned by the `trust` stack.

**What a compromise reaches:**

- **A container:** the worker's GitHub token (one repository), the model key (with a hard limit), Paperclip's master key (accepted for the pilot), and the open internet. Not IMDS, not WireServer, no Azure credential, not the delivery key, not the backup settings file.
- **VM root:** all of the above, plus a write-only SAS (at most 7 days), the delivery private key (which decrypts only future deliveries to this host) and a node key for a tag that reaches nothing. Not the long-term `age` key.
- **The public repo:** nothing directly. A malicious release reaches a VM only after the operator approves a `production` run for that SHA (decision 11).

## Repository layout

**Proposed public `software-factory` layout** (MIT; absent paths below are work to implement):

```
LICENSE README.md SECURITY.md CODEOWNERS .terraform-version
hosting/paperclip-hermes/
  kit/                                         # WP2: one import commit; message records private source repository and commit
  terraform/modules/{trust,host,guardrails,tailnet}/
  terraform/stacks/{trust,host,tailnet}/       # exact pins, .terraform.lock.hcl (linux_amd64 + windows_amd64)
  terraform/tests/*.tftest.hcl                 # mock providers
  tools/{policy-lint.py,plan-guard.py,ref-bump-check.py,deny-list.py}
  bootstrap/sf-bootstrap.sh  bootstrap/sf      # sf = operator-device helper (restore-assist, age-key)
  bootstrap/tests/*.bats
  docs/{quickstart,architecture,threat-model,cost,upgrade,restore,move,vault-move,teardown,break-glass,runbooks/*}.md
  docs/adr/0001-azure-deployment.md  docs/adr/0002-pilot-security-model.md
  rulesets/{public-repo,instance-repo,org-merge-gate}.json
.github/workflows/ph-{plan,deploy,power,ops,host-monitor,cost-guard,backup-verify,restore,move,vault-move,teardown,tailnet-import,rotate-age,selftest}.yml
.github/workflows/{ci,kit-e2e,release,release-notes-gate,sf-merge-gate}.yml
renovate/{default,instance}.json
templates/instance/
deny-list/allow.txt
```

Reusable workflows must sit directly in `.github/workflows`, which is why they carry the `ph-` prefix.

**Instance template** (`templates/instance/`, copied by `sf-bootstrap.sh init`):

```
config/instance.tfvars.json        # prefix, apps, daily_run_hours, shutdown_time, timezone,
                                   # billing_period_start_day, budgets, not_before, custody, vault_mode
config/targets/<name>.json         # subscription, tenant, client IDs per plane, RG, location, vm_size,
                                   # status (active|fenced|decommissioning), written by the bootstrap
config/tailnet/policy.hujson       # the whole imported policy (contains logins, so private)
config/receipts/bootstrap.json     # non-secret receipt
.github/workflows/*.yml            # ~10 lines each: uses: <org>/software-factory/.github/workflows/ph-*.yml@<sha> # vX.Y.Z
.github/CODEOWNERS                 # operator owns .github/** and config/**; ph-*.yml have no owner (merge gate)
.github/workflows/claude.yml       # optional bot review
renovate.json                      # extends github>…/software-factory//renovate/instance#vX
```

There is no `kit_ref`. The kit commit on the VM is always the `job_workflow_sha` of the approved `ph-deploy` run.

## Terraform modules

- **Terraform:** 1.17.2, read by `setup-terraform` from `.terraform-version`. Renovate updates that file through a `customManager` using the hashicorp datasource. The floor is 1.16, set by the VM module [V].
- **Root stacks** pin exact versions of:
  - `azapi` 2.x (at least 2.12);
  - `azurerm` 4.x (at least 4.81 because of the Key Vault resource shape; at least 4.21.1 for the Logic App module);
  - `tailscale/tailscale` 0.30.0;
  - `random`, `time` and `tls` within the VM module's ranges.

  The exact `azapi` and `azurerm` patches are fixed when the lock file is first generated.
- **Lock files** carry hashes for `linux_amd64` and `windows_amd64`. CI runs `terraform providers lock -platform=linux_amd64 -platform=windows_amd64` and fails if the committed file differs.
- **Reusable modules** declare provider ranges with upper bounds.

| Need | Stack | Source | Version |
|---|---|---|---|
| Workload RG (resource-group scope mode) | trust | `Azure/avm-res-resources-resourcegroup/azurerm` | 0.5.0 |
| UAMIs, federated credentials, outbound role assignments | trust | `Azure/avm-res-managedidentity-userassignedidentity/azurerm` | 0.6.0 |
| Custom roles (`SF Host Operator`, `SF VM Deallocator`, `SF Backup Writer`, `SF Delegation Key`) | trust | `azurerm_role_definition` (no AVM module) | — |
| Backup account: Shared Key off, versioning, version-level WORM, account `immutability_policy` 30 d; containers `backups` and `restore-staging`; `CanNotDelete` lock | trust | `Azure/avm-res-storage-storageaccount/azurerm` | 0.11.0 |
| Lifecycle rules: `backups/` prefix `baseBlob.delete` and `version.delete` after 45 d; `restore-staging/` after 1 d | trust | the storage module's management policy input, or `azurerm_storage_management_policy` | — |
| Container time-based policy | trust | `azapi_resource` `immutabilityPolicies`, used only if self-test 3 fails | — |
| Key Vault (RBAC, purge protection) | trust | `azurerm_key_vault` shaped to the AVM interfaces (the AVM module is Orphaned, not Available) | — |
| VNet and subnet | host | `Azure/avm-res-network-virtualnetwork/azurerm` | 0.23.0 |
| NSG, no inbound allow rules | host | `Azure/avm-res-network-networksecuritygroup/azurerm` | 0.7.0 |
| Outbound-only static IPv4 | host | `Azure/avm-res-network-publicipaddress/azurerm` | 0.3.0 |
| VM: Ubuntu 24.04, `managed_identities` empty, `generate_admin_password_or_ssh_key = false`, the operator's SSH public key, `shutdown_schedules`, `data_disk_existing_disks`, boot diagnostics; tag `expires`, plus the protocol tags passed through unchanged (see Details) | host | `Azure/avm-res-compute-virtualmachine/azurerm` | 0.22.1 |
| Data disk that outlives VM rebuilds, `prevent_destroy` | host | the AVM disk module if the index lists it as Available at build time; otherwise `azurerm_managed_disk` shaped to the AVM interfaces | — |
| Action group | host (guardrails) | `Azure/avm-res-insights-actiongroup/azurerm` | 0.3.0 |
| Deallocator: Consumption Logic App in the **target RG**, whose system identity holds `SF VM Deallocator` on the RG | host (guardrails) | `Azure/avm-res-logic-workflow/azurerm` | 0.2.0 |
| Budgets (subscription and RG) | host (guardrails) | `azurerm_consumption_budget_*` (the AVM module is only Proposed) | — |
| Tailnet: `tailscale_acl` (imported), `tailscale_tailnet_settings` (imported), `tailscale_dns_preferences` (MagicDNS), `tailscale_tailnet_key` | tailnet | `tailscale/tailscale` | 0.30.0 |

**Details:**

- The `expires` tag is computed from `time_static` plus `time_offset` (30 days). That keeps it stable across applies, unlike `plantimestamp()`.
- Approval and delivery tags (`sf-approved-sha`, `sf-recovery-sha`, `sf-recovery-until`, `sf-delivery-recipient`) are written only by the deployment protocol. Because the AVM VM module manages `tags` in full and a caller cannot add `ignore_changes` inside a module, `ph-deploy` and `ph-restore` read the VM's current protocol tags with `az` before planning and pass them to the host stack unchanged (absent on a new VM). `plan-guard.py` fails any plan that changes or removes them, and a `terraform test` asserts that the pass-through preserves them.
- **The join key** is `reusable=false`, `ephemeral=false`, `preauthorized=true`, `expiry=3600`, `tags=["tag:pcpilot"]` and `recreate_if_invalid="never"`.
  - A `terraform_data` generation counter, not the VM, triggers its replacement.
  - It is delivered encrypted to the host delivery key through run command, never in `custom_data`.
- `custom_data` holds only cloud-init that does not depend on secrets. It mounts the disk, installs Docker and a pinned Tailscale, generates the delivery key, and checks out the kit at the release SHA passed in as a variable.
- **`terraform test` with mock providers asserts these invariants:**
  - The NSG has no inbound rule with `access = Allow` other than the Azure defaults.
  - The NIC has exactly one public IP, and no load balancer, NAT rule or Bastion exists.
  - `managed_identities` is empty, and `generate_admin_password_or_ssh_key = false`.
  - The plan contains no `tls_private_key` or `random_password` resource.
  - All four current VM protocol tags pass through unchanged; they are absent on a new VM.
  - A shutdown schedule exists, set at least 45 minutes after the backup timer.
  - The data disk has `prevent_destroy`.
  - Shared Key is off on the backup and state accounts.
  - Both lifecycle actions exist, with their prefix filters.
  - `id-sf-ops` and `id-sf-restore` hold the delegation role at account scope and their data roles at container scope.
  - The RG budget is at least £5.
- **The policy invariant** runs against the instance's real policy, not a mock. `tools/policy-lint.py` runs in `ph-plan` and `ph-deploy` before Terraform. It expands every `grants` and `acls` entry and fails when:
  - any `src` covers `tag:pcpilot` (the literal tag, `*`, `autogroup:tagged`, or a host alias resolving to it);
  - any `dst` covering `tag:pcpilot` has a `src` other than the operator login.

  The tailnet module also renders the `tests` and `sshTests` entries (see "Tailscale").

## Bootstrap script

`sf-bootstrap.sh` is bash plus `az`, `gh`, `terraform` and `age`. It is idempotent, `--dry-run` is the default and prints every call, and bats tests run it against stub `az`, `gh` and `curl`.

| Mode | What it does |
|---|---|
| `init --org O --instance-repo R --reviewer U[,U2]` | Requires GitHub Enterprise for private-repo reviewer gates; **refuses Free, Pro, Team and unknown/unverifiable capability**. Checks the org Actions policy (`gh api orgs/{org}/actions/permissions`) and prints the one setting to change only if it is wrong. Creates the private repo from `templates/instance/` and sets `use_immutable_subject`. Creates the environments: `plan` with no branch restriction; `ops`, `production` and `restore` limited to `main`, with reviewers on `production` and `restore`. Prevent-self-review is on only when there are two or more reviewers. Reads back each protection rule and branch restriction and runs a harmless probe that must wait for review. Does not create Azure federation or issue a successful bootstrap receipt until gates are verified; a failed capability check leaves setup incomplete. |
| `trust --vault-subscription V [--vault-mode separate-subscription\|same-subscription] --target NAME --subscription S --scope resource-group\|subscription --resource-group G --location L [--apply]` | Registers resource providers. Reads `spendingLimit` and enforces the vault-mode rule. Creates the state account and container with `az` if absent: Shared Key off, TLS 1.2, versioning, soft delete, `CanNotDelete` lock. Assigns the signed-in operator **temporary** Blob Data Contributor on `tfstate` and Key Vault Secrets Officer, waits for propagation, then runs `terraform apply` of the `trust` stack under the operator's login (`use_azuread_auth = true`), importing the state account. Removes the temporary assignments. Writes `config/targets/<NAME>.json`. Prints the vCPU headroom (for the VM family and Total Regional) and the cost headroom. Repeated for each new target; `--remove-target` removes one. |
| `age-key [--custody operator\|vault] [--escrow-cmd "…"] [--rotate]` | Generates the key pair in memory and writes no file. The private key goes to `--escrow-cmd` (a password-manager CLI) through stdin, or is shown once on screen. Under vault custody it is also written to Key Vault with `az keyvault secret set --file /dev/stdin` under secret name `age-<fingerprint>`, never as `--value`; records the returned version URI in the fingerprint map. Sets the public recipient and its fingerprint as instance variables. `--rotate` keeps the old key enabled and readable by the gated restore identity until decision 8's retirement checks pass, and triggers `ph-rotate-age`. |
| `vault-move --phase state\|backups\|keys [--apply]` | Operator-run data migration under the operator's own login; see "Moving the vault" for temporary data roles, verification and cleanup. The deploy and restore identities never receive destination backup or key-write permissions; `ops` gets only its normal `SF Backup Writer` role on the destination `backups` container from `trust`. |
| `tailscale --token-stdin` | Reads a short-lived Tailscale API access token from stdin. Adds the two `tagOwners` lines with `POST /acl` and `If-Match` on the current ETag. Creates two federated identities through the API: `plan` (read scopes) and `production` (full scopes). Checks that MagicDNS and HTTPS certificates are on (`GET /tailnet/-/settings`, DNS preferences) and refuses if not. Stores the client IDs and audiences as variables, then revokes the token. |
| `tailscale --print-subject` | Prints the subjects, scopes and tags for the console fallback. |
| `rulesets [--apply]` | Applies the committed `rulesets/*.json` to the public repo, the instance repo and the org (the merge-gate required workflow at a pinned SHA). The operator runs it, because these are GitHub settings changes. |
| `--revoke` | Deletes the federated credentials and role assignments, through a `trust` destroy limited by `-target`. |

**Federated credential subjects** are read from `GET /repos/{o}/{r}/actions/oidc/customization/sub` and take the form `<sub_claim_prefix>:environment:<env>`. The script never builds the name-only form. It finishes with a non-secret JSON receipt.

Bats tests assert:

- the dry-run output and idempotence;
- subjects read from the API;
- the role scopes (RG by default; the delegation role at account scope);
- the ABAC condition text;
- that no secret appears in argv or on disk;
- the self-review rule for one and for two reviewers;
- refusal on Free, Pro, Team, unknown capability and failed protection-rule readback; Enterprise acceptance only after the reviewer probe waits for approval;
- the vault-mode refusal on a spending-limited target.

## Workflows

Shared rules:

- Each job sets minimal `permissions`, for example `id-token: write, contents: read`.
- Callers pass no secrets.
- Each reusable workflow learns its own SHA by requesting an OIDC token (audience `sf-self`) and reading the `job_workflow_sha` claim. It checks out `software-factory` at that SHA and has no ref input.
- VM-touching operations share `sf-host-<target>` with `cancel-in-progress: false`, `queue: max` and the cancellation monitor in decision 15.
- Agents may dispatch `plan`, `power`, `ops`, `cost-guard`, `backup-verify` and the jobs the operator then approves. Agents never approve.

| Name | Trigger | Env | What it does |
|---|---|---|---|
| `ci` (public) | PR, push | none | fmt, validate, TFLint, `terraform test`, checkov; the lock-file platform check; kit pytest; shellcheck; bats; actionlint; zizmor; gitleaks; the deny-list; the Renovate config validator; every `uses:` pinned to a SHA; tests for `ref-bump-check.py`, `policy-lint.py` and `plan-guard.py` |
| `kit-e2e` (public) | PRs touching the kit or pins; weekly | none | Backup and restore e2e on the runner. **Upgrade-rollback e2e** (force a failed health check, assert rollback). **Cross-version restore** (a backup made under the previous release restores through its own kit commit) |
| `release` (public) | push to `main` | none | release-please tag `vX.Y.Z`; kit tarball with its SHA-256 |
| `release-notes-gate` (public) | PR by `renovate[bot]` | none | Fetches upstream release bodies between the old and new tags. If one says "breaking", "migration" or "rollback floor", it applies `needs-operator` and fails a required check that agents may not bypass |
| `sf-merge-gate` (org-required) | instance PRs | `plan` | Decision 11 rules, through `ref-bump-check.py` |
| `ph-plan` | instance PR on `config/**` or `.github/**`; nightly | `plan` | `policy-lint.py`. Plans `tailnet` and `host` for every active target, and `trust` for drift, all with `-lock=false`, passing through current VM protocol tags unchanged (see Details). Posts a redacted diff, image-pin changes with release notes, a Retail Prices estimate, the headroom and the projected spending-limit date. `plan-guard.py` flags any delete or replace of the VM, a disk, storage or `tailscale_acl`, and any change to the immutability or lifecycle rules; it fails any plan that changes or removes protocol tags. Nightly drift or a pending `trust` diff opens an `ops-alert` |
| `ph-deploy` | push to `main`; dispatch(`target`,`stacks`,`allow_stale_backup`,`override_replace`,`override_cost`,`finalize_handoff`) | `production` + `ops` (SAS minting only) | See "First deployment" and "Upgrades". Refuses before `not_before`, below the headroom threshold, within 150 minutes of the scheduled shutdown, or when `plan-guard` flags a delete or replace without `override_replace`. A plan that changes or removes protocol tags always fails. After health and port-scan success, publishes approval tags and pushes `sf/ops-ref-<sha>`. The mint-only ops job must `needs:` the approved `production` job of the same run before returning encrypted credentials; production delivers them to the host. `finalize_handoff` verifies merged callers and retires the recovery tags without another upgrade |
| `ph-power` | dispatch(`start`\|`stop`) | `ops` | Start: start the VM, re-mint the SAS if under 72 h remain, run the catch-up backup **after** the re-mint, run the health check, and schedule a stop at start + `daily_run_hours`. Stop: `backup.sh --reason pre-stop`, then deallocate |
| `ph-ops` | every 2 h, 07:00–23:00 | `ops` | Applies the shared ops SHA check below, including the bounded previous-SHA recovery path. If the VM is running: SAS re-mint under 72 h with a canary upload, health check, and pre-stop backup then deallocate when past 14 h or the scheduled stop. Dead-man check: the newest backup must be younger than the last VM stop. Interrupted-op recovery from `last-op.json`. Disk over 70 %. Worker-token expiry from the recorded header (alert at T-7 days). `expires` tag (alert at T-3, deallocate at expiry). WORM still Unlocked after the pilot end date. Retries pending decommissions. Maintains `ops-alert` issues and a pinned Status issue |
| `ph-host-monitor` | `workflow_run` completed; scheduled reconciliation | none | Outside the host concurrency group. Reads Actions run/job metadata, alerts on cancellations and reconciles pending stops under decision 15. Minimal Actions read/write and issues write permissions for inspection, retry dispatch and alerts; no OIDC or VM credentials. Never executes code or artifacts from the observed run |
| `ph-cost-guard` | daily; dispatch | `ops` | The **authoritative** cost trigger. Queries the Cost Management Query API with a custom time period from the last `billing_period_start_day` to now, plus the forecast to the end of the period. Runs a pre-stop backup and deallocates above the thresholds. Above 95 % of the subscription forecast, opens an `ops-alert` and dispatches `ph-teardown mode=park`, which waits for approval |
| `ph-backup-verify` | weekly | `ops` | Metadata and header only: one blob per day the VM ran; the `age` header; size within 20 % of the previous blob; the oldest blob younger than 47 days; metadata keys present (`kit_commit`, digests, `schema`, `recipient_fp`). Never decrypts |
| `ph-restore` | dispatch(`target`,`blob`,`rebuild`,`unfence`,`accept_runner_exposure`) | `production` (rebuild job only) + `restore` | 1. Read the blob metadata. 2. If `rebuild`, apply the host in restore mode as a `production` job using the deploy identity. 3. On the host: check out `kit-<kit_commit>` into a fresh directory, generate an ephemeral key, and return its recipient. 4. Re-encrypt: under operator custody, poll `restore-staging` for up to 60 minutes while the operator runs the `sf restore-assist` command printed in the summary; under vault custody, re-encrypt on the runner. 5. The host fetches the staged blob with a 1 h read SAS, runs `restore.sh --fenced`, shreds the ephemeral key, and runs the health check with messaging off. 6. Optionally `upgrade.sh` to `sf-approved-sha` |
| `ph-move` | dispatch(`from_target`,`to_target`,`phase`,`drill`,`drill_target`) | `production` + `restore` | See "Moving subscriptions" |
| `ph-vault-move` | dispatch(`phase=switch\|verify`,`drill`) | `production` + `restore` + `ops` (SAS minting only) | Switches and verifies only after validating the operator's migration receipt. The mint-only `ops` job must `needs:` the approved `production` job of the same run, so it starts only after the operator has approved that SHA; a mint job that could start before that approval is a release-blocking defect. State, backup and key copies run in `sf-bootstrap.sh vault-move`, never under these workflow identities. See "Moving the vault" |
| `ph-teardown` | dispatch(`target`,`confirm=<rg>`,`mode=destroy\|park`) | `production` | Final backup, then: `park` deletes the VM, OS disk and public IP and keeps the data disk; `destroy` also deletes the tailnet device and any outstanding keys, then destroys the host stack. Backups are kept |
| `ph-tailnet-import` | dispatch, once | `plan` | Reads the live policy and tailnet settings. Pushes the branch `sf/tailnet-import` with `policy.hujson`, `import {}` blocks for `tailscale_acl` and `tailscale_tailnet_settings`, and attributes copied from the imported values. Runs the plan itself and posts it to the job summary. An agent (the machine user) opens the PR, so `ph-plan` and the gate run on it |
| `ph-rotate-age` | dispatch after `sf-bootstrap.sh age-key --rotate` | `production` | Delivers the new recipient to `backup.env` through the envelope path, runs a canary backup, and records the fingerprint |
| `ph-selftest` | dispatch | `production` | Runs the gating tests listed below |

**What `ph-selftest` tests:**

1. A protected parameter carrying a dummy value is not echoed by GET, the Activity Log or `/var/lib/waagent`. This is now defence in depth, because the path carries only ciphertext.
2. A SAS with `sp=c` alone can upload with Put Block and Put Block List.
3. The 30-day retention is inherited by the container.
4. The production federated identity can mint a key for `tag:pcpilot`.
5. The plan identity's read-only Tailscale scopes are enough for a `tailscale_acl` plan with validation.
6. The scope needed for `tailscale_tailnet_settings`.
7. Budget `time_grain = BillingMonth` is accepted by the offer.
8. The ops identity mints a delegation SAS (key at account scope) and uploads a canary.
9. A TCP scan of the public IP from the runner (`nmap -Pn`, top 1,000 ports plus 22, 443 and 9443) finds nothing open. This scan also runs at the end of every `ph-deploy`.

Each failure flips its documented fallback in `instance.tfvars.json`.

The kit-control tests in "Context" and the deployment/recovery acceptance cases below are also release gates. Missing implementation or a failed mandatory gate blocks release; it is not converted into an enabled fallback.

**Shared ops SHA check.** Every ops-plane entry point checks its own `job_workflow_sha` against `sf-approved-sha` before run command. A matching, unexpired `sf-recovery-sha` permits only status/journal inspection, credential renewal with a canary, pre-stop backup, and the journal-directed rollback to the recorded previous kit. It cannot install a candidate release, unfence messaging or publish tags. Any other mismatch raises an alert; ARM deallocation remains allowed. The mint-only job called by an approved deployment has no host commands and does not require an existing host tag. It must `needs:` the approved `production` job of the same run, so it starts only after the operator has approved that SHA; a mint job that could start before that approval is a release-blocking defect.

**PRs proposed by workflows.** A workflow never opens a PR with `GITHUB_TOKEN`, because such PRs trigger no workflows. It pushes a branch and posts its own result to the job summary. In the operator's estate, an agent opens the PR as the machine user. For a stranger with no agent, the quickstart says to open the PR from the pushed branch by hand; a PR a human opens triggers `ph-plan`.

## First deployment

`trust` creates the workload RG in resource-group scope mode (the host stack creates it in subscription scope), never the VM. `ph-deploy` selects initial installation only for a fresh named target with a bootstrap receipt, no completed installation record and no existing application data. VM absence or a missing backup alone never selects this branch: a retained disk, prior installation receipt or ambiguous state requires approved restore/recovery.

1. Run the same policy, plan, cost and shutdown guards as an upgrade. Apply tailnet then host under `production`, with cloud-init in `--prepare-only` mode: create the VM, mount a fresh data disk, install the pinned kit and generate the delivery key. Keep services and the backup timer off. There is no pre-upgrade backup or local rollback requirement because no previous installation exists.
2. Wait for cloud-init and run-command readiness. Inspect the disk and initialization journal before initializing anything. Persist the initial-install run ID and phase on the host and in the deployment receipt. A retry resumes those phases without reformatting or overwriting existing data; disagreement between records stops for recovery.
3. Read and pin the delivery recipient. Mint the SAS in the mint-only `ops` job, which must `needs:` the approved `production` job of the same run under the shared SHA check, then deliver it and the tailnet join key as ciphertext using the approved production identity. Join the tailnet and run the canary. Only then initialize and start the selected apps.
4. Run health, the public-port scan and the first encrypted off-site backup. Publish `sf-approved-sha` only after all succeed, record installation completion and enable normal schedules. Push the ops-caller bump. A failure leaves an incomplete install with its data preserved, services stopped and an alert; rerun the approved deployment to resume. No ops run command is allowed before the first approval tag exists.

Acceptance (Open): from an empty RG and no backups, deployment reaches a healthy installation and a restorable first backup. Interrupt after VM creation and after initialization; both retries preserve data and resume. A missing VM with a retained application disk must choose restore/recovery and never initial installation.

## Upgrades

1. **Detection.** Renovate, through the free Mend hosted app, with the shared preset. It covers:
   - compose `tag@sha256` pins and the `Dockerfile.hermes` FROM line;
   - AVM modules and providers, with monthly `lockFileMaintenance` (plus the CI platform check);
   - `.terraform-version`;
   - SHA-pinned actions;
   - in the instance repo, the reusable-workflow SHA of the **production-plane callers only** (depType `workflow`), grouped into one PR per release. The ops-plane callers are excluded by `matchFileNames`; `ph-deploy` bumps them after approval.
2. **Version filters**, which also protect security:
   - Paperclip: `allowedVersions: /^\d{4}\.\d{3,4}\.\d+$/`. GHCR holds more than 8,000 tags, including nightly, beta, canary and `-cloud` [V].
   - Hermes: `/^v0\.\d+\.\d+$/`. This blocks the stale CalVer tag `v2026.9.24`, which sorts above `v0.23.4` and would look like an upgrade [V]. It also excludes `rc`, `canary`, `-desktop` and per-architecture tags.
   - PostgreSQL stays on 17.
   - `minimumReleaseAge: 3 days` for images, with the missing-timestamp behaviour set explicitly. WP7 tests that a GHCR Paperclip update passes after 3 days.
3. **Commit types.** The preset sets `semanticCommitType: fix` with scope `deps` for docker, dockerfile and terraform updates, so release-please cuts a release. Actions-only updates stay `chore`.
4. **Public repo merges.** CI, `kit-e2e` and `release-notes-gate` must be green, and two review bots must have reviewed the current head. Then agents merge:
   - digest pins, image patches, Actions and `.terraform-version` patch PRs.

   These stay with the operator (`needs-operator`, no automerge):
   - provider bumps;
   - **AVM 0.x minor bumps**, which the preset treats as major;
   - Paperclip and Hermes version PRs that the notes gate flags.
5. **Instance merges.** Renovate bumps the production-plane `@sha # vX.Y.Z`. The merge gate passes, an agent merges (subject to operator decision (e)), and `ph-deploy` waits for one operator approval. Then:
   1. **Guards.** Refuse within 150 minutes of the scheduled shutdown. Refuse when `plan-guard` flags a delete or replace and `override_replace` is not set. Always refuse a plan that changes or removes protocol tags. Refuse below the headroom threshold or before `not_before`.
   2. **Prepare.** Start the VM if it is deallocated and remember its previous power state. Set `sf-maintenance-until` = now + 2 h.
   3. **Re-mint.** A mint-only job in the `ops` environment re-mints the SAS and encrypts it to the pinned delivery recipient; the production job delivers it. It must `needs:` the approved `production` job of the same run, so it starts only after the operator has approved that SHA; a mint job that could start before that approval is a release-blocking defect. The mint job does not acquire the host concurrency group or run host commands.
   4. **Off-site backup.** `backup.sh --reason pre-upgrade`. If it fails, the run stops before changing anything, unless `allow_stale_backup` was passed (approval only). That input accepts an off-site blob younger than 24 hours, skips the health requirement for an unhealthy stack, and still takes the local rollback point.
   5. **Terraform apply,** tailnet then host. Read the current VM protocol tags with `az` before planning and pass them to the host stack unchanged (see Details); the candidate is not yet approved for unattended ops. If the apply fails: raise an alert, leave the kit untouched, restore the power state and stop. The documented recovery is re-running an approved `ph-deploy` at the previous release. The existing ops callers still pass the SHA check.
   6. **Kit upgrade** (`upgrade.sh` on the host, under `op.lock`):
      1. Check free space against the volumes plus the dump estimate. It must be at least 2× that, plus 10 %; otherwise refuse.
      2. Stop the stack and create the rollback point under `/data/rollback/<run>` (root, 0700): `pg_dump`, volume archives, the previous kit SHA and the digests.
      3. Check out the new kit SHA (`job_workflow_sha`), pull by digest, and run `compose up --wait`.
      4. Run `health.sh`. It prints one JSON line of at most 4 KB covering compose health, Paperclip `/api/health` through Caddy, Caddy→Hermes returning 401 with TLS verified, the IMDS and WireServer DROP counters, the migration journal and Tailscale Serve.
      5. On failure: **check out the previous kit SHA first**, restore the rollback point, then run the health check again. If that also fails, the stack stays stopped and an `ops-alert` names the off-site blob for an approved `ph-restore`.
   7. **Finish and handoff.** Run the port scan. On success, write a durable `healthy-awaiting-handoff` journal entry, then publish the previous SHA and a maximum 24 h expiry in the recovery tags **before** advancing `sf-approved-sha` to the new workflow SHA. Read back the tags and push `sf/ops-ref-<sha>`. Tag writes and branch creation are idempotent; a retry preserves the original recovery deadline. Clear the maintenance tag and restore the previous power state. A failed health check or rollback never advances approval.
   8. **Pruning.** The rollback point is pruned after 6 successful `ph-ops` health checks or 24 hours of VM uptime, not after wall-clock time.
6. **Disk snapshots are not used:** a snapshot is a plaintext copy that a Contributor can export with a SAS.

**Interrupted operations and the caller handoff.** Before publication, old ops callers retain normal access. If the journal says an upgrade was interrupted before health succeeded, they check out the recorded previous kit and restore its rollback point. Release compatibility tests must prove that the previous ops release can read the new journal and invoke that recovery path; a release failing this test cannot be deployed automatically.

After publication, old callers use only the shared recovery command set while the recovery tags are valid. A `healthy-awaiting-handoff` journal means the upgrade succeeded: report it and resume the branch handoff through an approved deployment, rather than rolling back a healthy stack. A production recovery that rolls back also restores `sf-approved-sha` to the previous SHA before clearing recovery tags. Keep the rollback point throughout an unresolved handoff, even if the normal pruning threshold is met. Block another upgrade until handoff or rollback is complete.

`ph-ops` alerts when handoff is pending for an hour and before its deadline. After the ops-caller PR merges and a run at the new SHA passes health, an approved `ph-deploy finalize_handoff=true` verifies those facts, marks the journal complete and removes the recovery tags. Ops never writes tags. Expiry closes the previous-SHA exception; if handoff is still incomplete, raise an urgent alert and require an approved deployment to resume it. There is no indefinite recovery authorization.

Acceptance (Open): interrupt after host apply, during upgrade, between recovery-tag and approved-tag writes, and after approved-tag publication but before branch creation. In each case the previous caller can inspect the journal and renew credentials; interrupted upgrades roll back, healthy upgrades resume handoff. Force a failed health check and assert the approved SHA stays unchanged. Verify recovery expiry, successful finalization, and rejection of new-install/unfence commands on the previous-SHA path. Until these pass, upgrades remain operator-run with an approved restore available.

## Backups and key custody

- **Storage.**
  - Container `backups` in the vault, with Shared Key off and versioning.
  - Version-level WORM with a 30-day time-based policy. It is **Unlocked** only while `pilot_end_date` is in the future. `ph-ops` alerts at that date, and the documented default afterwards is **Locked**.
  - The `trust` stack (Owner only) owns the account, the policy, the lifecycle and the `CanNotDelete` lock. No workflow identity can change them.
  - Lifecycle rules: `baseBlob.delete` and `version.delete` after 45 days on the `backups/` prefix. Every backup is a uniquely named base blob, so the base-blob rule is what actually ages backups out.
  - Teardown of the account waits for retention to expire.
- **Blob metadata** (non-secret, written at upload): `kit_commit`, the image digests, `schema`, `recipient_fp` and `reason` (`nightly`, `pre-stop`, `pre-upgrade`, `canary` or `final`).
- **Upload credential.**
  - `id-sf-ops` mints a 7-day user-delegation SAS (`sr=c`, `sp=cw`; `sp=c` if self-test 2 passes).
  - It is encrypted to the host delivery key and decrypted by `sfctl set-backup-credential` into `/etc/paperclip-hermes/backup.env` (root, 0600, mounted into no container).
  - It is re-minted by `ph-power start`, by `ph-ops` when less than 72 h remain, and by `ph-deploy` before its backup, then proved by `backup.sh --canary`.
  - Revocation: remove the role, or revoke the delegation keys [V].
- **When backups run.**
  - Every workflow-driven stop runs `backup.sh --reason pre-stop` first.
  - The nightly timer runs at `shutdown_time − 45 min` and is a safety net only.
  - `Persistent=true` is replaced by `ph-power start`, which runs the catch-up backup after the re-mint.
  - The dead-man rule is that the newest backup must be younger than the last VM stop.
- **Status.** `backup.sh` writes `last-backup.json` and `last-op.json`.
- **Keys.** See decision 8.
  - Deep verification (decrypt, check the manifest hashes, `pg_restore`) happens only inside a restore or drill.
  - Rotation (`age-key --rotate` then `ph-rotate-age`) is yearly or on suspicion, and keeps old keys until retention plus lifecycle has passed.
  - Acceptance (Open): back up under key A, rotate to B, then restore A's retained blob by fingerprint under the gated restore identity without any secret-property write permission. Both source and migrated vault copies must pass; a failure blocks rotation/retirement and leaves A enabled.

## Moving subscriptions

Targets are named, so a move addresses two targets: `config/targets/<name>.json` gives each target its subscription, tenant, client IDs, RG, location, size and status. `ph-ops` and `ph-cost-guard` iterate over every target whose status is not `decommissioned`.

1. The operator runs `sf-bootstrap.sh trust --target <new> --subscription <new> … --apply`.
2. **`phase=prepare`** applies the host stack for `to_target` under the state key `<instance>/<to_target>/host.tfstate`, with `restore_mode=true` and the backup timer off. The new host joins the tailnet with a fresh key as `<hostname>-next`.
3. **`phase=fence`** runs a final backup on the old host, deallocates it, removes its schedules and sets status `fenced`. If the old subscription is disabled or gone, this phase is skipped, `fence_skipped=true` is recorded, and the latest nightly backup is used.
4. **`phase=restore`** (approval) runs `ph-restore` of that exact blob, under the backup's own kit commit, with the **final** configured hostname baked into the configuration. Messaging stays off.
5. **`phase=cutover`** (approval):
   - deletes the old tailnet device (`devices:core`);
   - runs `tailscale set --hostname <hostname>` on the new host;
   - re-issues Serve certificates and runs `sfctl set-hostname`;
   - asserts that the MagicDNS name equals the configured name;
   - unfences;
   - if `fence_skipped`, first rotates the Paperclip secrets and the messaging and bot tokens, so the old copy's credentials are dead;
   - opens a checklist issue for re-sign-ins (Codex device code; bot tokens that need the operator);
   - pushes a branch making `to_target` the default.
6. **`phase=decommission`**, after N days (default 7), destroys the old host stack and runs `--remove-target`. If the old subscription is unreachable, the target becomes `decommissioning`, and `ph-ops` retries daily with an `ops-alert`. If the old VM ever starts again, it is deallocated on sight.

The secrets delivered with `sfctl` live in the kit's `.secrets/` under `SECRET_FILES`, so they travel with the backup. Only the Codex device code, plus bot tokens rotated because the fence was skipped, need new sign-ins.

**Drill.** `drill=true` runs prepare and restore into `rg-sf-drill` in `drill_target`. That defaults to the vault subscription at `Standard_B2as_v2`, with a 3-hour cap under the maintenance tag, and its cost is included in the headroom check. The drill records the recovery point, the time taken and the hostname assertion, then tears the drill RG down.

## Moving the vault

The data-copy phases run locally as **`sf-bootstrap.sh vault-move` under the operator's own login**. `ph-vault-move` only switches and verifies. The deploy and restore identities keep their existing rights; neither gets permission to write destination backups or keys. For destination backup writes, `ops` gets only its normal `SF Backup Writer` role on the destination `backups` container from `trust`.

1. The operator runs `sf-bootstrap.sh trust --vault-subscription <new> --apply` to create the destination vault, including normal workflow roles there. Pause state-changing workflows and acquire the instance migration lock; keep host backups writing to the source until cutover. Deallocation paths (`ph-cost-guard`, the uptime cap, scheduled stops) keep running, with pre-stop backups to the source until cutover.
2. The reviewed bootstrap assigns the operator temporary data roles in each tenant: Blob Data Contributor on source/destination `tfstate` containers (for state leases and migration), Blob Data Reader on source `backups`, a custom add/create/write/read/list role with no delete on destination `backups`, Key Vault Secrets User on the source age secrets and Key Vault Secrets Officer on the destination vault (vault custody only). The new vault contains only this instance's age secrets. No temporary role grants permission to alter WORM or Shared Key settings. Use an assignment ledger, wait for propagation and prove read/write access before copying.
3. **`phase=state`** migrates each stack backend with `terraform init -migrate-state`, using Entra authentication in both tenants. Verify state lineage, serial and resource inventory, and write the backend-switch receipt before workflows resume. A partially migrated instance stays paused; resume from the per-stack receipt rather than writing to both backends.
4. **`phase=backups`** streams ciphertext through the operator process with Entra-authenticated source reads and destination writes; no account keys or service SAS. Copy every retained backup/version to unique names in the new WORM container, preserve metadata and record a source-version-to-destination map. Compare hashes computed from both byte streams and verify destination retention before accepting each copy. Keep source backups intact and make a final delta copy at cutover.
5. **`phase=keys`** (vault custody) reads each required source secret version into memory and creates an enabled destination version over stdin, with no plaintext files, argv values or logs. Azure assigns new version IDs: record each fingerprint's new exact version URI, verify its derived recipient, and retain the source map for rollback. Historical keys remain enabled under decision 8.
6. The bootstrap writes a non-secret migration receipt containing the state checks, blob/hash map, key-version map and destination resource IDs. It removes its temporary assignments in a cleanup handler on success or failure, verifies their removal and reports any cleanup failure for operator action. After a crash, rerun cleanup from the assignment ledger before resuming migration.
7. **`ph-vault-move phase=verify`** validates the receipt against destination inventory and runs an approved fenced restore/drill using the copied blob and, for vault custody, a historical key. Pause host backup timers for cutover; the operator repeats `phase=backups` for the final delta, reacquiring its temporary roles, updating the receipt and removing those grants again. **`phase=switch`** under `production` validates that final receipt, updates configuration/backend references, uses the mint-only `ops` job to mint a destination SAS, delivers it encrypted and proves a host canary lands there. If verification fails, keep the source backup configuration and credential active; do not reverse already migrated state backends implicitly. Resume workflows against the verified per-stack backends and resume timers only after a consistent configuration is established.

After cutover, remove normal source writer grants through operator-run `trust`, revoke outstanding source delegation keys and keep source data read-only until retention and restore verification allow retirement. A scheduled `ops-alert` then asks the operator to run the old `trust` destroy. A failed switch resumes from the receipt while retaining the source; it never deletes source backups or keys.

Acceptance (Open): copies succeed with exactly the temporary roles above in each supported tenant layout; normal deploy/restore identities are denied destination backup/key writes. A restore from a copied historical-key blob succeeds, the canary lands in the new account, and temporary grants are absent after both a successful copy and an injected failure. Until these pass, vault moves are unavailable and the source remains authoritative.

## Tailscale

- **Credentials** are federated identities created by `sf-bootstrap.sh tailscale` through the API, so the operator does no console click-ops beyond creating one short-lived API token.

  | Identity | Subject | Scopes |
  |---|---|---|
  | `plan` | `<sub_claim_prefix>:environment:plan` | `policy_file:read`, `devices:core:read` (raised by self-test 5 if validation needs full `policy_file`) |
  | `production` | `<sub_claim_prefix>:environment:production` | `policy_file`, `devices:posture_attributes`, `devices:core:read`, `auth_keys`, `devices:core` (plus the settings scope if self-test 6 needs it) |

  - The `production` identity uses the tag `tag:pc-deployer`, which owns `tag:pcpilot`.
  - Every job that touches Tailscale runs in `plan` (read) or `production` (write). That includes `ph-move cutover`, which deletes a device in its `production` job.
  - Fallback: an OAuth client ID and secret as `production` environment secrets.
- **Ownership of the policy file and settings.**
  1. The `ph-tailnet-import` PR carries the live policy and settings with their `import {}` blocks. The plan must show **no diff** for both, which avoids resetting unrelated tailnet-wide settings.
  2. A second PR, reviewed by the operator under CODEOWNERS:
     - adds the `tagOwners`;
     - adds a grant from the operator's login to `tag:pcpilot`;
     - **removes or narrows every existing grant or ACL whose `src` or `dst` is `*`, `autogroup:member` or `autogroup:tagged` in a way that covers `tag:pcpilot`**;
     - adds no grant with `tag:pcpilot` as its source;
     - adds the SSH rule `{action:"check", src:[operator], dst:["tag:pcpilot"], users:["admin"]}`;
     - adds `tests`: the operator is accepted to `tag:pcpilot:22,443,9443`; `tag:pcpilot` is **denied** to a sample of every other tag, user and host:port in the file; a second user or an `autogroup:member` stand-in is denied to `tag:pcpilot`;
     - adds `sshTests` for the check rule.

     `policy-lint.py` enforces the same rules on every plan.
  3. `tailscale_tailnet_settings.acls_externally_managed_on = true` locks the console editor, and `reset_acl_on_destroy = false`.
  4. MagicDNS is managed by `tailscale_dns_preferences`. HTTPS certificates are managed by the settings resource if provider 0.30.0 exposes them; otherwise the bootstrap and `ph-deploy` verify them as a precondition.
- **Break-glass** (`docs/break-glass.md`): turn off "externally managed" in the console, edit, then re-run `ph-tailnet-import`.
- **Revocation.** Teardown deletes outstanding keys, because revoking a credential does not revoke them [V].

## Secrets and the manual sign-ins

| Item | Delivery path |
|---|---|
| Owner login for the bootstrap | The operator's own `az` and `gh` sessions; never stored |
| Tailscale API token (short-lived, for the bootstrap only) | Piped on stdin to `sf-bootstrap.sh tailscale --token-stdin`, then revoked by the script |
| `age` private key | Generated by `sf-bootstrap.sh age-key` into the password manager (and Key Vault under vault custody); never on disk, never on the VM |
| Paperclip claim URL | `tailscale ssh -t admin@<host> sudo sfctl claim-url`, opened in a browser over the tailnet |
| Codex device code | Paperclip's OpenAI connector in the browser, approved on the operator's phone |
| OpenAI API key (hard limit) | `sfctl secret set openai-api-key`, by TTY or `--stdin` pipe; only Hermes receives it |
| Worker GitHub token (fine-grained, one repository, 30 days) | `sfctl secret set github-token` (records the real expiry header), plus the Paperclip connector in the browser. A GitHub App with 1-hour tokens replaces it later (issue 149) |
| SAS, join key | Workflows only, as ciphertext encrypted to the host delivery key |

## Cost guardrails

These are UK retail estimates for `Standard_B4as_v2`: about £0.142 an hour, plus about £15.2 a month for the disks and IPv4. Benefit credits are not reflected in the cost data [V].

| Usage | Approx. £/month |
|---|---|
| 24/7 (also breaks dev/test credit terms such as a 120-hour continuous cap) | 118.6 |
| 12 h/day | 66.9 |
| 8 h/day | 49.7 |
| 6 h, weekdays (~130 h) | 33.7 |
| `B2as_v2` (one app), 12 h/day | ~41 |
| Deallocated | 15.2 |
| Parked (VM, OS disk and IPv4 deleted; data disk kept) | ~4–5 |
| Torn down (off-site backup only) | 0 in the target |
| Target guardrails (budgets, action group, Consumption Logic App, a few runs a month) | ~0 |
| Vault (storage, state, Key Vault) | under 3 |
| Restore drill in the vault subscription (`B2as_v2`, ≤ 3 h) | ~0.3 per drill |

**Defaults:**

- `daily_run_hours = 8`, with on-demand starts through `ph-power`.
- Auto-shutdown at 22:00 local time; the backup timer at 21:15. The test enforces a gap of at least 45 minutes.
- 14-hour uptime cap.
- `billing_period_start_day` = 17 for this operator.
- **RG budget:**
  - amount = max(£5, min(`pilot_monthly_cap` (£35), headroom)), or `pilot_monthly_cap` when `override_cost` is set;
  - e-mail at 50 % actual, deallocate at 80 % actual, e-mail at 100 % forecast.
- **Subscription budget** = the credit:
  - e-mail at 70 % actual; deallocate *only the pilot VM* at 85 % actual; e-mail at 100 % forecast;
  - `time_grain = BillingMonth` if self-test 7 passes; otherwise `Monthly`, with the budget used for e-mail only.
- `ph-cost-guard` is the authoritative trigger, on the real billing period. Its thresholds are lowered by 4 days × the daily run rate to cover data lag.
- Above 95 % of the forecast, the approval-gated park tier runs.
- `ph-plan` and `ph-deploy` print the projected date on which the subscription hits its spending limit.
- Code never changes the spending limit.

**On a subscription whose other resources already forecast the whole credit, the headroom is about zero, so deploy refuses.** The operator must cut other spend, set `override_cost` knowingly, or pick another target. Even with deallocation, the deallocated floor still accrues, which is why the park tier exists.

## Required kit implementation

These are proposed files and changes, not files present in this revision. WP2 imports the kit into `hosting/paperclip-hermes/kit/` as one commit whose message records the private source repository and commit. Control evidence comes from this repository's own unit tests and `kit-e2e` CI, which must exist and pass before release; the controls in "Context" remain Open until that evidence exists.

1. **`backup-restore.py`:**
   - The validator accepts only a user-delegation SAS: `skoid`, `sktid`, `ske` at most 7 days ahead, `sp` in {`c`,`cw`}, `sr=c`, no `si`. Reject stored-policy, service and account SAS forms; test rejection with Shared Key disabled.
   - Writes the blob metadata listed under "Backups and key custody".
   - Adds `rollback-point create|restore`, `--canary`, `--reason`, the status files, `--verify-only`, and restore from a re-encrypted staged blob with an identity file in `/run`.
   - Restore runs under the backup's own kit commit, so the byte-for-byte kit check and the image-pin check pass.
2. **New scripts:**
   - `upgrade.sh`, including the free-space check and `op.lock`;
   - `health.sh`, which prints JSON;
   - `sfctl`, with the commands `status`, `secret set`, `claim-url`, `set-backup-credential`, `delivery-recipient`, `restore-key-ephemeral`, `set-hostname`, `messaging on|off`, `upgrade` and `rollback`;
   - `join-tailnet.sh`, which decrypts the key, runs `--ssh --advertise-tags=tag:pcpilot` and configures Serve.
3. **`bootstrap.sh`:**
   - `--non-interactive` and `--prepare-only`;
   - generates missing random values on the host;
   - sets the time zone from config;
   - enables the backup timer only once `backup.env` exists;
   - schedules the timer from config, without `Persistent`.
4. **`compose.yaml`** gets the profiles `paperclip` and `hermes`, a Caddy route for Hermes alone, and per-service secret files. A pytest checks that a secret reaches only its intended service.
5. **Installation.** The kit installs as root-owned checkouts `/opt/software-factory/kit-<sha>`, with a `current` symlink.
6. **`tailscale-policy.hujson`** becomes a documented fragment that the tailnet module renders.
7. **README** portal steps are replaced by references to the workflows.

## What stays manual

Each item is allowed by R11 or reserved to the operator by R12 and the operator's rules.

1. Running the bootstrap: `init`, `trust`, `age-key`, `tailscale`, `rulesets` and vault-move data-copy phases. `trust` is re-run when a release changes the `trust` stack or when a target is added. Finalizing the ops-caller handoff also needs a production approval.
2. Creating one short-lived Tailscale API token for `sf-bootstrap.sh tailscale`. This replaces the OAuth client.
3. Installing the Renovate app, a GitHub-settings change reserved to the operator. `rulesets --apply` is the other.
4. Storing the `age` private key in the password manager, prompted by the bootstrap.
5. Approving `production` and `restore` runs, and running `sf restore-assist` during a restore under operator custody.
6. Claiming Paperclip.
7. Codex device-code sign-in.
8. Creating the OpenAI key with a hard limit and delivering it.
9. Creating the worker token and delivering it.

## Risks and open questions

1. **Protected parameters.** Only ciphertext travels this way; self-test 1 is defence in depth.
2. **Uploads with `sp=c` alone** are untested; `cw` is the default.
3. **Retention inheritance** is untested; the `azapi` fallback is ready.
4. **Tailscale.** Three items are unverified:
   - minting keys for an owned tag through a federated identity;
   - creating federated identities through the API with an access token;
   - the read-only scopes and the settings scope.

   Self-tests 4–6 cover them, and the console path (`--print-subject`) is the fallback.
5. **Budgets.** Whether forecast alerts drive action groups, and whether `BillingMonth` works for this offer, are unverified. `ph-cost-guard` is authoritative regardless.
6. **`ops` has unattended root through run command** at `sf-approved-sha`, plus the bounded previous-SHA recovery exception during handoff. The shared SHA check, merge gate and Renovate exclusion of the ops callers enforce this protocol. Flexible federated credentials that bind `job_workflow_ref` stay off until that preview reaches GA.
7. **Vault custody** puts the long-term key in runner memory during a restore. It is opt-in and gated twice.
8. **Paperclip image provenance** and tag immutability are unconfirmed. The mitigations are digest pins and `minimumReleaseAge`.
9. **Approval fatigue** at upstream cadence. Renovate groups releases on a weekly schedule; deployment and handoff finalization each need production approval.
10. **Open versions.** The exact patches of `azapi` and `azurerm`, and whether an AVM disk module is Available, are fixed at implementation time.
11. **Org required-workflow capability** is checked separately within the supported Enterprise setup. If unavailable, `init` requires operator merges; required deployment reviewers remain mandatory.
12. **The `trust` stack is applied only by the operator**, so AVM bumps in it are not automated. That is deliberate.
13. **Implementation is absent.** The six kit controls, initial-install path, recovery handoff and vault migration remain Open until the acceptance evidence specified above exists. This ADR alone supplies no operational protection.

---
