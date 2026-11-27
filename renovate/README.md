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
Renovate automerge is disabled throughout. Image digest/patch updates (Paperclip: digest only) and Terraform runtime patches
get that label. Action references (depType `action`) and instance production refs get it only for digest, pinDigest, patch and minor updates. Every major from any manager gets
`needs-operator`. Validator/release tooling updates always require the operator. Provider updates
(including patches) and AVM 0.x minor updates get
`needs-operator`; AVM minor changes carry Renovate's `isBreaking` flag and a major-risk PR note.
Other image minor updates carry neither merge label. All image and Terraform commits use
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
a [generated release index](https://raw.githubusercontent.com/agenticcodingops/software-factory/renovate-data/paperclip-releases.json)
on branch `renovate-data`, published by [the daily workflow](../.github/workflows/renovate-paperclip-index.yml).
The stdlib builder reads every page of stable [GitHub releases](https://docs.github.com/en/rest/releases/releases#list-releases)
and resolves bare and v-prefixed tags independently through anonymous registry HEAD requests.
It never lists package versions; the index is bounded by stable releases, and contains
version, digest and timestamp together in the [custom datasource format](https://docs.renovatebot.com/modules/datasource/custom/).
The GitHub token goes only to `api.github.com`, never a registry or the raw index host.

The timestamp records first observation of each `(version, digest)`, retaining it only
while the digest stays the same. A re-pushed tag restarts the clock, so the soak cannot be
shorter than the image's real age. A new entry soaks for three days from first observation;
the first run delays all candidates by three days. An unresolved tag form (404) is omitted;
any other registry/API/parse failure or an empty result aborts before publishing and leaves
the previous file in place. Contents/ref API publishing uses no git push
([contents](https://docs.github.com/en/rest/repos/contents#create-or-update-file-contents),
[refs](https://docs.github.com/en/rest/git/refs#create-a-reference)).
A missing index or timestamp cannot propose an unsoaked update. A stalled index cannot
introduce new releases; previously observed entries remain available with their existing
soak clocks. Until the operator first runs the workflow after merge, the URL returns 404
and no Paperclip update is proposed. No dependency dashboard is configured, so a failed
scheduled run is how a stalled index is noticed.

The instance regex customManager supplies `depType: workflow` for canonical
`uses: ...@<sha> # vX.Y.Z` caller lines. The native manager is
disabled for factory refs to avoid duplicate updates. Ten non-ops caller files are explicitly
allowed, including `ph-move.yml`, `ph-rotate-age.yml`, `ph-selftest.yml` and
`ph-host-monitor.yml`, and grouped into one PR per update stream (major and non-major stay separate). All `ph-ops*`, `ph-power*`, `ph-cost-guard*` and
`ph-backup-verify*` callers are disabled by file name, across both managers and every target suffix.
New caller filenames require review of the allowlist. Ops refs advance only in the approved handoff.

The [release-notes gate](../.github/workflows/release-notes-gate.yml) reads upstream Paperclip/Hermes
release bodies for every stable tag between the current default-branch version and PR head,
including skipped releases, regardless of PR author.
Case-insensitive `breaking`, `migrat*` or `rollback floor` text applies `needs-operator` and fails
the `release-notes-gate` status on the exact PR head. Deferred notes (notes that ship,
follow, land, come or publish with/in another version, or "undocumented here") independently
fail the gate. This is stricter than ADR 0001's literal hazard wording; the ADR is unchanged.
Most recent Hermes patches currently go to the operator while upstream defers patch notes
([v0.23.4 notes](https://github.com/NousResearch/hermes-agent/releases/tag/v0.23.4)).
Paperclip version bumps are operator-reviewed in practice because upstream bodies routinely
contain these words, including "No migrations" ([Paperclip releases](https://github.com/paperclipai/paperclip/releases)). Missing/empty notes and API failures also fail
closed. The gate reads `(tag, digest)` from strict physical image lines: canonical
`    image: repository:tag@sha256:...` under the named Compose service and
`FROM repository:tag@sha256:... [AS stage]` in the Dockerfile. Earlier Dockerfile
stage aliases and unqualified local-build Compose images on other services are allowed.
Comments cannot supply a pin; directives, interpolation on the tracked image, escapes,
unsupported image syntax, duplicate pins and non-ASCII/control characters fail closed.
These forms deliberately restrict [Dockerfile syntax](https://docs.docker.com/reference/dockerfile/),
[YAML escaped line breaks](https://yaml.org/spec/1.2.2/#double-quoted-style) and
[Compose interpolation](https://docs.docker.com/reference/compose-file/interpolation/).
Whenever either tag or digest changes, an anonymous registry token and mandatory manifest
HEAD check must prove that the new tag resolves to the pinned digest; unavailable or
mismatched digests fail closed ([registry authentication](https://docs.docker.com/reference/api/registry/auth/),
[OCI digest header contract](https://raw.githubusercontent.com/opencontainers/distribution-spec/main/spec.md)).
Digest-only updates with a matching digest need no release notes.
The Compose check accepts one canonical form and is not a YAML parser. It does not cover
a PR that points Compose at a different Dockerfile, adds `COPY --from=<image>`, or adds an override file. Protected changes are detected by comparing the captured default-branch SHA with the
captured head SHA using the [three-dot compare API](https://docs.github.com/en/rest/commits/commits#compare-two-commits),
including previous filenames on renames. The protected paths are `.github/workflows/`,
`ci/renovate/release-notes-gate`, `hosting/paperclip-hermes/rulesets/` and root `CODEOWNERS`.
The script prefix does not match `test_release_notes_gate.py`; that test remains unprotected.
A PR with 300 or more changed files fails closed because the compare list may be truncated.
Gate script, ruleset and CODEOWNERS changes always require the operator. Workflow files
have a verified pin-only exception: status must be `modified`, immutable merge-base and head
contents must decode as UTF-8 with equal line counts, and every changed line must be a
same-action `uses: owner/repo[/path]@<40 lowercase hex> # vX.Y.Z` bump preserving the key
and indentation. Local, Docker and same-repository references are excluded. The numeric
version must advance within the same major; unchanged versions with a different SHA,
downgrades and majors fail. The new SHA must equal the upstream tag's commit, following
at most five annotated-tag hops ([refs API](https://docs.github.com/en/rest/git/refs),
[tags API](https://docs.github.com/en/rest/git/tags)). Missing data, API/decode errors,
more than 20 workflow files or 50 changed lines fail closed. Compare patch text is never trusted.
This proves what the upstream tag pointed to when the gate ran; it does not review upstream
action code. Renovate's configured `minimumReleaseAge` and two current-head reviews remain
other controls. Actions majors, runtime inputs (`uses-with`), runners and workflow images,
and every other workflow edit need the operator. The allowed depTypes are checked against
[Renovate 45.1.0](https://github.com/renovatebot/renovate/blob/45.1.0/lib/modules/manager/github-actions/dep-types.ts).
The operator's 2026-11-27 decision keeps public Actions patch/minor merges with agents;
this explicit verified exception records the ADR interpretation without editing the ADR. The `pull_request_target` workflow is
restricted to `main` and checks out `github.sha`, the trusted default-branch commit. PR files are
read through the API as data. The script guards both event and live base refs and posts nothing
for other targets. It resolves current main through the Git ref API, and fails if main moves during
evaluation; a changing PR head receives no passing result.

The per-SHA status is informational. `org-release-notes-gate.json` requires the workflow itself at
a merged main commit resolved by the operator from the disabled template and checked against
`org-release-notes-gate.pin.json`, with only the operator's org-admin pull-request bypass. A forged status or another
workflow using the same job name cannot satisfy that rule. GitHub's required-workflow rules ignore
trigger filters, so the script's default-branch guard is necessary too. See the
[GitHub required-workflow documentation](https://docs.github.com/en/enterprise-cloud@latest/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets#supported-event-triggers).

The npm manager updates root validator devDependencies, including Renovate, Release Please and Nock.
Those three packages share a branch with `googleapis/release-please-action`; review the library and
action together. Their version numbers are independent: grouping does not synchronize the action's
bundled library. This repository currently tests Renovate 45.1.0 on Node 24.22.3, which supports
relative preset resolution (Renovate 42.0.0 did not).

npm copies upstream `deprecated` messages into `package-lock.json`, so an npm or
lock-maintenance PR can fail the deny-list when a message contains a maintainer address
([npm deprecation documentation](https://docs.npmjs.com/cli/v11/commands/npm-deprecate)).
The operator must verify the address is public upstream metadata and add the exact value
to `deny-list/allow.txt` with a comment naming the package; never add a path or field exclusion.
npm PRs carry no `agent-merge` label.

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
verify the immutable workflow binding and release-tag bypass contract. Native RE2 is not installed in CI because npm installs use `--ignore-scripts`.
The validator falls back to RegExp ([validator source](https://github.com/renovatebot/renovate/blob/45.1.0/lib/config-validator.ts));
the CLI fixture keeps `RENOVATE_X_IGNORE_RE2`. A separate Python syntax test walks every
regex-valued preset string and rejects lookarounds, backreferences, atomic groups and
possessive quantifiers while allowing named capture groups, enforcing the
[RE2 syntax contract](https://docs.renovatebot.com/string-pattern-matching/).
