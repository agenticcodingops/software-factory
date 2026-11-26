# Renovate policy (WP7)

The root [renovate.json](../renovate.json) activates [default.json](default.json). An instance uses:

```json
{
  "extends": ["github>agenticcodingops/software-factory//renovate/instance.json"]
}
```

Installing the Mend Renovate app and applying the updated public ruleset remain operator settings
changes. A deployed instance can select a reviewed preset ref using Renovate's `#<ref>` syntax.
The instance preset inherits the public policy, then limits updates to Actions and caller refs.

Agents may merge `agent-merge` PRs only after required checks and two reviews of the current head.
Renovate automerge is disabled throughout. Image digest/patch updates, Actions and Terraform runtime
patches get that label. Provider updates (including patches) and AVM 0.x minor updates get
`needs-operator`; AVM minor changes carry Renovate's `isBreaking` flag and a major-risk PR note.
Other image version updates have no agent-merge label. All image and Terraform commits use
`fix(deps)` so Release Please cuts releases; Actions and instance caller updates use `chore(deps)`.

The native Compose, Dockerfile and Terraform managers cover the kit and Terraform subtree.
PostgreSQL stays on 17. Terraform roots and AVM modules retain exact pins; reusable-module provider
compatibility ranges remain unchanged. Provider updates refresh the discovered stack lock files;
monthly lock-file maintenance uses `fix(deps)`, requires the operator and runs before 04:00 on the
first day. The existing CI lock regeneration
check still verifies both Linux amd64 and Windows amd64 hashes.

`.terraform-version` uses a regex customManager with `custom.hashicorp`, backed by HashiCorp's
`terraform/index.json`. Renovate 42 has **hashicorp versioning**, but no built-in **hashicorp
datasource**. Exact stack `required_version` pins use the same datasource and branch as the runtime
file, so an upgrade does not leave CI and the stacks inconsistent.

Paperclip accepts stable `YYYY.MMDD.N` and `vYYYY.MMDD.N` tags; nightly, beta and cloud tags fail the
filter. Hermes accepts only `v0.N.N`, excluding `v2026.9.24`, canaries, release candidates, desktop
and architecture tags. Both require three days of release age, with missing timestamps held.

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

Renovate 42 classifies reusable workflows as `action`, so an instance regex customManager supplies
`depType: workflow` for canonical `uses: ...@<sha> # vX.Y.Z` caller lines. The native manager is
disabled for factory refs to avoid duplicate updates. Six production/plan caller files are explicitly
allowed and grouped into one release PR. All `ph-ops*`, `ph-power*`, `ph-cost-guard*` and
`ph-backup-verify*` callers are disabled by file name, across both managers and every target suffix.
New caller filenames require review of the allowlist. Ops refs advance only in the approved handoff.

The [release-notes gate](../.github/workflows/release-notes-gate.yml) reads upstream Paperclip/Hermes
release bodies for every stable tag between the old and new versions, including skipped releases.
Case-insensitive `breaking`, `migration` or `rollback floor` text applies `needs-operator` and fails
the `release-notes-gate` status on the exact PR head. Missing/empty notes and API failures also fail
closed. Digest-only updates need no release notes. The workflow checks out the event's trusted base
SHA and never executes PR code; its token can read contents/PRs, label issues and write statuses.
The public ruleset payload requires that status from GitHub Actions. Operator bypass is the existing
org-admin pull-request bypass; agents must not bypass it. Applying that payload is not part of WP7.

## Acceptance fixture

Run `npm run validate:renovate -- renovate.json renovate/default.json renovate/instance.json` and
`npm run test:renovate`, or run the full `bash ci/check.sh` entry point.

The fixture invokes the installed **real** `renovate --platform=local --dry-run=lookup` CLI against
temporary copies of the kit, Terraform modules, stacks and committed lock files. It serves the
unmerged presets through an HTTP mirror (only the instance preset's parent URL is rewritten).
Nock intercepts network responses; any unmatched network request or dependency lookup warning
fails. Renovate's extraction, versioning, JSONata, age checks, package rules, grouping and commit
message generation run unchanged. API timestamps are relative to the test clock, so the fixture
stays valid as the calendar advances. The CLI's documented local mode stops after lookup: it cannot
create branches, PRs, or update artifacts. The fixture checks the resulting real branch plans,
including `pendingChecks` before/after three days and with missing timestamps.

The generated digest PR commit is then supplied as a merged SCM fixture to **real Release Please**,
using this repository's manifest configuration and release-please **17.1.3**, the version bundled
in the SHA-pinned release action. Its `createPullRequests()` path must request a
release PR with changelog, version file and manifest updates, both for initial `0.1.0` and a
subsequent `0.1.1` patch. The SCM adapter records writes locally; no GitHub PR is published.
Separate notes-gate tests cover risk words, skipped release pages, missing notes/API failures,
label/status publication, digest-only updates and a changing PR head. Native RE2 is skipped in the
CLI fixture because CI installs npm packages with `--ignore-scripts`; the regexes use RE2-compatible
syntax and the strict validator checks the committed presets.
