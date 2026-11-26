# Renovate policy (WP7)

The root [renovate.json](../renovate.json) activates [default.json](default.json). An instance uses:

```json
{
  "extends": ["github>agenticcodingops/software-factory//renovate/instance.json#vX.Y.Z"]
}
```

Replace `vX.Y.Z` with a reviewed release tag. The instance preset inherits `./default` by
[relative reference](https://docs.renovatebot.com/config-presets/#relative-preset-references), so
both presets resolve at that same tag, then instance updates are limited to Actions and caller refs.
Install the Mend Renovate app and apply the branch, required-workflow and release-tag payloads
according to the [rulesets README](../hosting/paperclip-hermes/rulesets/README.md) before enabling
agent merges. These are operator settings changes; committing a payload does not apply it.

Agents may merge `agent-merge` PRs only after required checks and two reviews of the current head.
Renovate automerge is disabled throughout. Image digest/patch updates and Terraform runtime patches
get that label. Actions and instance production refs get it only for digest, pinDigest, patch and minor updates; their majors get
`needs-operator`. Validator/release tooling updates always require the operator. Provider updates
(including patches) and AVM 0.x minor updates get
`needs-operator`; AVM minor changes carry Renovate's `isBreaking` flag and a major-risk PR note.
Other image version updates have no agent-merge label. All image and Terraform commits use
`fix(deps)` so Release Please cuts releases; Actions and instance caller updates use `chore(deps)`.

Updates are scheduled weekly, Monday 00:00–06:00 UTC, consistent with ADR 0001.
Renovate must actually run during that window; the schedule limits branch creation, not the app's
execution. Monthly lock maintenance retains its own schedule.

The native Compose/Dockerfile managers update only `hosting/paperclip-hermes/kit/**`, and Terraform
only `hosting/paperclip-hermes/terraform/**`. Negative `matchFileNames` package rules disable
updates elsewhere: `managerFilePatterns` are additive and cannot remove Renovate's defaults.
PostgreSQL stays on 17. Terraform roots and AVM modules retain exact pins; reusable-module provider
compatibility ranges remain bounded. Tailscale's module accepts `>= 0.30.0, < 1.0.0`, so a
stack's operator-reviewed 0.x minor update remains satisfiable. Provider major PR notes require
raising module bounds in the same PR when needed. Provider updates refresh the discovered stack
lock files;
monthly lock-file maintenance uses `fix(deps)`, requires the operator and runs before 04:00 on the
first day. The existing CI lock regeneration
check still verifies both Linux amd64 and Windows amd64 hashes.

`.terraform-version` uses a regex customManager with `custom.hashicorp`, backed by HashiCorp's
`terraform/index.json`. Renovate has **hashicorp versioning**, but no built-in **hashicorp
datasource**. Exact stack `required_version` pins use the same datasource and branch as the runtime
file, so an upgrade does not leave CI and the stacks inconsistent.

Paperclip accepts stable `YYYY.MMDD.N` and `vYYYY.MMDD.N` tags; nightly, beta and cloud tags fail the
filter. Hermes accepts only `v0.N.N`, excluding `v2026.9.24`, canaries, release candidates, desktop
and architecture tags. Both require three days of release age (plus Renovate's default age buffer),
with missing timestamps held.

Renovate's [Docker datasource](https://docs.renovatebot.com/modules/datasource/docker/) supplies
timestamps for Docker Hub, but **not GHCR**. Paperclip therefore uses `custom.paperclip-ghcr`:
the [GitHub package versions API](https://docs.github.com/en/rest/packages/packages#list-package-versions-for-a-package-owned-by-an-organization)
returns image tags, the `sha256` digest, and GitHub's `created_at` timestamp. JSONata maps these
into releases together, preserving `tag@sha256` updates and enforcing the actual registry age.
Renovate reuses its GitHub app token for this API; nothing secret belongs in a preset. API failure,
an absent candidate or a missing timestamp cannot pass the age check. The custom datasource reads
the API's first page of 100 package versions; it does not search historical pages. A stable release
outside that window is not proposed, and an older pin may lack a digest refresh until a newer stable
candidate appears. This bounds requests independently of GHCR's thousands of historical tags.

The instance regex customManager supplies `depType: workflow` for canonical
`uses: ...@<sha> # vX.Y.Z` caller lines. The native manager is
disabled for factory refs to avoid duplicate updates. Six production/plan caller files are explicitly
allowed and grouped into one PR per update stream (major and non-major stay separate). All `ph-ops*`, `ph-power*`, `ph-cost-guard*` and
`ph-backup-verify*` callers are disabled by file name, across both managers and every target suffix.
New caller filenames require review of the allowlist. Ops refs advance only in the approved handoff.

The [release-notes gate](../.github/workflows/release-notes-gate.yml) reads upstream Paperclip/Hermes
release bodies for every stable tag between the current default-branch version and PR head,
including skipped releases, regardless of PR author.
Case-insensitive `breaking`, `migration` or `rollback floor` text applies `needs-operator` and fails
the `release-notes-gate` status on the exact PR head. Missing/empty notes and API failures also fail
closed. Digest-only updates need no release notes. Changes under `.github/workflows/**`, including
renames and deletions, also fail and require the operator. The `pull_request_target` workflow is
restricted to `main` and checks out `github.sha`, the trusted default-branch commit. PR files are
read through the API as data. The script guards both event and live base refs and posts nothing
for other targets. It resolves current main through the Git ref API, and fails if main moves during
evaluation; a changing PR head receives no passing result.

The per-SHA status is informational. `org-release-notes-gate.json` requires the workflow itself at
an immutable SHA, with only the operator's org-admin pull-request bypass. A forged status or another
workflow using the same job name cannot satisfy that rule. GitHub's required-workflow rules ignore
trigger filters, so the script's default-branch guard is necessary too. See the
[GitHub required-workflow documentation](https://docs.github.com/en/enterprise-cloud@latest/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets#supported-event-triggers).

The npm manager updates root validator devDependencies, including Renovate, Release Please and Nock.
Those three packages share a branch with `googleapis/release-please-action`; review the library and
action together. Their version numbers are independent: grouping does not synchronize the action's
bundled library. This repository currently tests Renovate 45.1.0 on Node 24.22.3, which supports
relative preset resolution (Renovate 42.0.0 did not).

## Acceptance fixture

Run `npm run validate:renovate -- renovate.json renovate/default.json renovate/instance.json` and
`npm run test:renovate`, or run the full `bash ci/check.sh` entry point.

The fixture invokes the installed **real** `renovate --platform=local --dry-run=lookup` CLI against
temporary copies of the kit, Terraform modules, stacks, npm devDependencies and committed lock files.
It serves the committed presets through mocked GitHub contents API responses at `#v1.2.3`, with
no preset rewriting; a fetch for an unpinned parent fails. This exercises actual tag inheritance.
Nock intercepts network responses; any unmatched network request or dependency lookup warning
fails. Renovate's extraction, versioning, JSONata, age checks, package rules, grouping and commit
message generation run unchanged. API timestamps are relative to the test clock, so the fixture
stays valid as the calendar advances. The CLI's documented local mode stops after lookup: it cannot
create branches, PRs, or update artifacts. The fixture checks the resulting real branch plans,
including `pendingChecks` before/after three days and with missing timestamps.

The generated digest PR commit is then supplied as a merged SCM fixture to **real Release Please**,
using this repository's manifest configuration and the npm-pinned release-please library
(currently **17.1.3**), independently of the release action's bundled library. Its
`createPullRequests()` path must request a
release PR with changelog, version file and manifest updates, both for initial `0.1.0` and a
subsequent `0.1.1` patch. The SCM adapter records writes locally; no GitHub PR is published.
Separate notes-gate tests cover risk words, skipped release pages, missing notes/API failures,
author-independent image checks against current main, workflow changes across diff pages,
non-default targets, label/status publication, digest-only updates, and moving main/PR heads.
The CLI fixture also covers Actions/caller majors, disabled out-of-scope files, grouped validator
updates, weekly scheduling and a Tailscale minor compatible with its module range. Ruleset tests
verify the immutable workflow binding and release-tag bypass contract. Native RE2 is skipped in the
CLI fixture because CI installs npm packages with `--ignore-scripts`; the regexes use RE2-compatible
syntax and the strict validator checks the committed presets.
