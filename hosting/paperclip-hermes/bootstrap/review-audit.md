# WP5 independent security review

Reviewed against starting commit `71ac42d`. Locations below are the original review
locations, not the shifted lines after fixes. Every finding is valid. Tests use
isolated fake transports; no cloud or GitHub mutation was performed during review.

The cited external pages were fetched during this review. Relevant contracts:

- [Python signal handling](https://docs.python.org/3/library/signal.html): handlers can raise between bytecode instructions; SIGPIPE is ignored and pipe writes can fail as exceptions. Cleanup needs protection from additional signals.
- [Terraform variable files](https://developer.hashicorp.com/terraform/language/values/variables): JSON parsing depends on the `.json` suffix.
- [Terraform recovery report](https://discuss.hashicorp.com/t/failed-to-save-state-during-terraform-apply-lock-false/49686) and [upstream issue 15688](https://github.com/hashicorp/terraform/issues/15688): backend persistence errors can leave `errored.tfstate`; recovery uses `terraform state push`.
- [GitHub CLI environment](https://cli.github.com/manual/gh_help_environment): shell tokens override stored authentication and host variables change the destination.
- [Deployment protection](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments): admin bypass defaults on. The [GitHub provider implementation](https://github.com/integrations/terraform-provider-github/blob/main/github/resource_github_repository_environment.go) and its [SDK request structure](https://github.com/google/go-github/blob/master/github/repos_environments.go) support `can_admins_bypass`. GitHub's REST OpenAPI currently omits it, so absence or true in readback always refuses.
- [Rules requiring PRs](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets), [reference creation](https://docs.github.com/en/rest/git/refs#create-a-reference) and [PR creation](https://docs.github.com/en/rest/pulls/pulls#create-a-pull-request): bootstrap can publish branches and PRs without bypassing main protection.
- [Azure data access](https://learn.microsoft.com/en-us/azure/storage/blobs/assign-azure-role-data-access) and [azurerm backend authentication](https://developer.hashicorp.com/terraform/language/backend/azurerm): management-plane Owner does not provide blob DataActions.
- [Blob list include flags](https://learn.microsoft.com/en-us/cli/azure/storage/blob?view=azure-cli-latest#az-storage-blob-list): metadata and immutability policies require `m` and `i` respectively.
- [CODEOWNERS precedence](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-code-owners): the last matching pattern wins and an empty owner list removes ownership.

| Original bootstrap.py line | Resolution | Regression |
| --- | --- | --- |
| 123 | All progress is best effort; first output failure disables further output and redirects a broken descriptor for shutdown. Token and assignment deletes still run. | Python `test_closed_output_cannot_skip_role_or_token_cleanup`; Bats `closed stdout plus repeated signals still removes recorded grants` and `closed stdout plus HUP and repeated interruptions still revoke token and roles` |
| 1295 | Handler raises once; cleanup marks interruption handled before blocking INT/TERM/HUP, then ignores them. | Python `test_repeated_signals_cannot_unwind_cleanup` injects a second signal during handler replacement and checks the block mask. |
| 500 | Remove rendered trees after every operation, including failed cleanup. Detect fallback state after failed Terraform and during teardown; preserve only that state in an explicit private recovery location and refuse further work until recovered. | Python `test_errored_state_is_reported_and_tree_removed`, `test_main_always_removes_rendered_trees` |
| 127 | Child environments use documented allowlists, adapters receive fewer variables, gh targets github.com and verifies an expected human operator before writes. | Python `test_children_receive_only_documented_environment`, `test_gh_verifies_expected_operator_before_write` |
| 325 | Send admin bypass false for production/restore/ops and require explicit false readback in every gate check. | Python `test_admin_bypass_readback_is_required`; Bats `admin bypass true or missing refuses init and federation` |
| 395 | Publish template/CODEOWNERS/probe changes through PRs; retain pending nonce until reviewed content reaches main, then dispatch and bind caller SHA. Preserve previous gate receipt while awaiting a PR. No bypass added. | Python `test_probe_files_use_reviewed_pr_and_resume_nonce`; Bats `init stages owned CODEOWNERS in a PR and preserves the previous gate receipt` |
| 959 | Filter probe receipt claims through the current Owner allowlist. Delete withdrawn managed federations and variables, and verify managed key inventory. | Python `test_federation_filters_withdrawn_claims_for_all_consumers`; Bats `tailscale withdraws removed refs and replaces changed release claims` |
| 996 | Union existing org includes with the new instance, retain previous exclusions, exclude source and read back the merged selector. | Bats `org merge gate retains previously covered repositories and excludes source` |
| 667 | Write variables to a 0600 `.tfvars.json` in the private rendered directory; unlink in finally. No variable values enter argv. Fixture rejects other suffixes/permissions. | Python `test_var_files_are_private_json_and_unlinked`; Bats trust/host/revoke integration paths |
| 1233 | Grant temporary Blob Data Contributor at the removable vault RG boundary and wait for blob-list propagation before backend init. Fixture enforces backend authorization. | Python `test_revoke_grants_backend_data_access_before_init`; Bats `revoke targets only authorization leaves and verifies removal` |
| 800 | Insert the first job after the empty jobs key and reparse every staged caller before writing. | Python `test_new_static_callers_parse_with_jobs_after_mapping_key` exercises all four new files. |
| 1163 | Request `vmi`, require recipient fingerprint metadata, retain policies when comparing destination retention. Fixture now honors include flags. | Bats `backup migration requests metadata and policy and refuses missing recipient fingerprint`, `vault backups stream every version preserve metadata and verify hashes and retention` |
| 1134 | Pending migration inspects destination first; retry an empty destination only when source still matches recorded lineage/serial/inventory. Refuse nonempty mismatches. Fixture tracks state by backend. | Bats `pending state migration retries empty destination and verifies source receipt` |
| 1142 | Dry-run describes state checks without executing pull/list against uninitialized stacks. Fixture rejects state reads without init. | Python `test_state_dry_runs_never_pull_or_list_uninitialized_backend`; Bats `all remaining modes default to dry-run without key generation or remote mutations` |
| 866 | Separate generation/escrow from idempotent publication. Record a pending public fingerprint before escrow; exact-key recovery uses key adapter; retry publication without new key generation. | Bats `age publication resumes after variable failure without generating another key`, `age escrow pending fingerprint recovers the same key after a lost response` |
| 965 | Replace managed description matches whose claims/settings changed, verify removal and recreate/read back the authorized key. | Bats `tailscale withdraws removed refs and replaces changed release claims` |
| 322 | Give every generated pattern operator owners, including the final caller pattern. Update the ADR to defer its planned ref-only carve-out until a reviewed gate implementation exists. | Python `test_generated_codeowners_has_no_unowned_overrides`; Bats `init stages owned CODEOWNERS in a PR and preserves the previous gate receipt` |

The original ADR explicitly intended unowned callers behind a required workflow,
but this checkout does not contain the later merge-gate implementation. Retaining
operator ownership now also agrees with the bootstrap's promised CODEOWNER review
for static caller changes. The documentation records this conservative restriction.

Recovery state is intentionally retained rather than destroyed with the rendered
tree: deleting the only unsaved state would prevent safe recovery. It is an explicit,
permission-restricted exception to the old no-persisted-state claim, with blocking
readback on the next invocation. Crash recovery after SIGKILL still requires the
assignment ledger and inspection of any interrupted private working directory.
