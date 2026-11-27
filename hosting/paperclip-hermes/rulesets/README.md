# Ruleset payloads

These are [repository ruleset request payloads](https://docs.github.com/en/rest/repos/rules#create-a-repository-ruleset) and [organization ruleset request payloads](https://docs.github.com/en/rest/orgs/rules#create-an-organization-repository-ruleset). Committing them does not change GitHub settings. Required workflows need organization support for the `workflows` rule; if unavailable, retain operator-only merges. See [supported required-workflow events](https://docs.github.com/en/enterprise-cloud@latest/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets#supported-event-triggers).

`public-repo.json` protects `main`: no deletion or force pushes, squash PRs only, an approving CODEOWNER review of the latest push, resolved conversations and the `checks` status from GitHub Actions (15368). CODEOWNERS retains `@vibecoddingops`; bot review is supplementary. Organization admins have a `pull_request` bypass so the operator can merge their own PRs without self-approval. Limit that role to trusted operators. The bypass requires a PR but can bypass review and checks; operators should still wait for checks and resolve conversations. A numeric review count cannot enforce two distinct reviewing bots.

`org-release-notes-gate.json` adds the public repository's required release-notes workflow. Its selector includes only `software-factory` on `main`; it is a disabled template with unresolved repository ID and commit SHA. `org-release-notes-gate.pin.json` records the reviewed workflow's Git blob SHA, independently of branch history. At apply time the operator resolves the newest merged main commit that touched the workflow, verifies its contents blob against the pin, and renders the active API body (commands below). A later workflow change requires a reviewed pin update and re-application. Do not substitute a mutable ref or a bare `release-notes-gate` status context: unrelated workflows use the same Actions integration and could forge that status. The status remains informational. The ruleset pins only the YAML; that YAML runs the script from the default-branch head
([pull_request_target semantics](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#pull_request_target)).
The logic is trusted because main is protected by CODEOWNER review of the latest push and
the gate fails PRs touching its own script prefix, gate workflow, rulesets or root CODEOWNERS, and workflow changes other than verified same-action, same-major pin bumps.
The captured main/head SHAs bind that [comparison](https://docs.github.com/en/rest/commits/commits#compare-two-commits),
including renames, and the 300-file cap fails closed. Only organization admins have a PR-only bypass of this workflow requirement.

The gate uses `pull_request_target` restricted to `main`, checks out `github.sha` (trusted default-branch code), and reads PR content only as API data. It compares tracked versions against current main, regardless of author. Required-workflow rules ignore event filters, so both the workflow job and script guard default-branch targets; the script posts no statuses or labels when the event targets another branch. A PR
retargeted during a run fails the gate; retargeting it to main afterwards requires a
reopen or new commit for re-evaluation. See the [required-workflow event rules](https://docs.github.com/en/enterprise-cloud@latest/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets#supported-event-triggers). Install the public branch and organization workflow payloads together using the by-hand section below. Agent merges stay disabled until `software-factory-main` reads back as active: the gate trust statement depends on these branch rules. Reopen existing PRs or push a new commit to trigger newly required workflows.

`public-release-tags.json` is a **disabled template** protecting `refs/tags/v*` against creation, update and deletion. Replace the Team `actor_id: 0` with a dedicated organization's release-actor team ID whose sole member is the machine user owning `RELEASE_PLEASE_TOKEN`, then set enforcement to `active`. Release Please creates tags with that token in `.github/workflows/release.yml`; the asset-upload `GITHUB_TOKEN` is not the tag actor. The only other bypass is the operator's OrganizationAdmin role, using `always` because tags have no PR path. Do not grant the shared Actions app (15368), Renovate, general write roles or agent accounts a bypass. Restrict release-token use to the reviewed release workflow. The release actor and operator must create version tags only at reviewed main commits; these rules restrict actors, not tag ancestry. Activate this tag protection before instances consume `github-tags` updates. Actions and instance-ref majors are labelled `needs-operator`. Action refs receive `agent-merge` only for digest, pinDigest, patch and minor updates; in this repository the gate permits only verified version-advancing pin bumps within one major. Runtime inputs, runners, workflow images and other workflow edits need the operator. The [Renovate README](../../../renovate/README.md) describes the exact conditions and limits.

`instance-repo.json` and `org-merge-gate.json` are also **disabled templates**. Bootstrap `hosting/paperclip-hermes/bootstrap/sf-bootstrap.sh rulesets [--apply]` resolves and applies these two templates. It keeps instance callers under operator CODEOWNERS until the separately reviewed gate ships, merges selectors across instances, excludes the source repo and adds no instance bypass actor. Keep these templates disabled until the operator configures the instance and gate. Do not activate the instance branch template by itself: ADR 0001 excludes `ph-*.yml` callers from instance CODEOWNERS, so the org-required gate must protect them, or the operator must merge every caller change.

Before enabling `org-merge-gate.json`, replace `repository_id: 0` with the public source repository's numeric ID, the all-zero SHA with a **tagged release commit containing the implemented instance gate**, and the `sf-instance-*` selector with the intended instance names. Keep the source repo outside that selector. The `workflows` rule binds workflow path, source repository and SHA; a same-named check in a PR cannot satisfy it. Never substitute a mutable ref or a status named `sf-merge-gate`.

`sf-bootstrap.sh rulesets [--apply]` defaults to dry-run and applies and reads back only
`public-repo.json`, `org-merge-gate.json` and `instance-repo.json`. It prints the payloads
it does not apply, but only after the release-SHA preconditions pass. Until a tagged
release containing `sf-merge-gate.yml` exists, the command aborts at its release-SHA
precondition, before printing NOT APPLIED and before applying anything, `public-repo.json`
included. That gate has not shipped yet. The `.pin.json` file is reviewed content metadata, not an API payload.

| Payload | Endpoint | Applied by |
| --- | --- | --- |
| `public-repo.json` | `repos/agenticcodingops/software-factory/rulesets` | bootstrap `rulesets` once the merge gate ships; operator, by hand until then |
| `instance-repo.json` | the instance's `repos/OWNER/REPO/rulesets` | bootstrap `rulesets` |
| `org-merge-gate.json` | `orgs/agenticcodingops/rulesets` | bootstrap `rulesets` |
| `org-release-notes-gate.json` | `orgs/agenticcodingops/rulesets` | operator, by hand |
| `public-release-tags.json` | `repos/agenticcodingops/software-factory/rulesets` | operator, by hand |

Do not POST organization payloads to a repository endpoint. An authenticated operator applies these settings; local CI does not apply or verify live enforcement.

For acceptance, list each endpoint, then fetch each managed ID at `ENDPOINT/ID`. Compare committed fields after removing server metadata (`id`, timestamps, source and links), sorting unordered rule/condition arrays and accounting for documented API defaults. A list response is only a summary and cannot establish payload equality. Verify the release team membership separately and test that an agent cannot create, replace or delete a `v*` tag, while the release actor can create one at a reviewed main commit. Verify a forged `release-notes-gate=success` does not satisfy the required workflow after the real gate fails. Retarget a PR away from main during the gate run and back; verify the required workflow is not green on that head until a reopen or push re-evaluates it. This apply/readback acceptance remains an operator step.

## Applying the public-repository payloads by hand

This is an operator action: an organization admin uses their own `gh auth login`,
from a checkout of the reviewed commit. CI and agents never run these commands.
The gate ruleset is named `software-factory-release-notes-gate`; resolve its reviewed
workflow content to the newest default-branch commit touching that path before rendering.
The [commits API](https://docs.github.com/en/rest/commits/commits#list-commits) accepts `sha` and `path`;
the [contents API](https://docs.github.com/en/rest/repos/contents#get-repository-content) returns a Git blob `sha`.
The [org rules API](https://docs.github.com/en/rest/orgs/rules) accepts workflow `repository_id`, `sha` and `path`.

The commands require standalone `jq`, `gh` and `diff` on PATH. Each JSON projection
is rendered to a temporary file with a checked exit status before comparison.
Run each Bash subshell below as a standalone command. Do not chain it with `&&`, `||` or `if`, which can disable errexit
([Bash conditional command rules](https://www.gnu.org/software/bash/manual/html_node/The-Set-Builtin.html)).
The first command shows the active account; use `gh auth switch` to the
organization-admin account if it shows a different login.

Apply `software-factory-main` first. It takes effect at once: squash only, CODEOWNER
approval of the latest push, and the `checks` status. Open PRs need that status on their
head. The later bootstrap `rulesets` run finds this ruleset by name and updates it in place.

```bash
(
set -euo pipefail
gh api user --jq .login
command -v jq >/dev/null
command -v gh >/dev/null
f=hosting/paperclip-hermes/rulesets/public-repo.json
jq -e . "$f" >/dev/null
id=$(gh api 'repos/agenticcodingops/software-factory/rulesets?per_page=100&includes_parents=false' --jq '.[] | select(.name=="software-factory-main") | .id') || exit 1
[[ -z $id || $id =~ ^[1-9][0-9]*$ ]] || { echo 'ruleset lookup is ambiguous or failed' >&2; exit 1; }
if [ -z "$id" ]; then id=$(gh api --method POST repos/agenticcodingops/software-factory/rulesets --input "$f" --jq .id) || exit 1
else gh api --method PUT "repos/agenticcodingops/software-factory/rulesets/$id" --input "$f" >/dev/null || exit 1; fi
[[ $id =~ ^[1-9][0-9]*$ ]] || { echo 'ruleset ID is unresolved' >&2; exit 1; }
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT
readback=$scratch/readback
gh api "repos/agenticcodingops/software-factory/rulesets/$id" > "$readback"
jq -S '{name,target,enforcement,bypass_actors,conditions,rules} | del(.bypass_actors[] | select(.actor_type == "OrganizationAdmin" and .actor_id == null) | .actor_id)' "$f" > "$scratch/expected"
jq -S --slurpfile want "$f" '{name,target,enforcement,bypass_actors,conditions,rules} | del(.bypass_actors[] | select(.actor_type == "OrganizationAdmin" and .actor_id == null) | .actor_id) | .rules |= map(. as $r | (first($want[0].rules[] | select(.type == $r.type)) // null) as $w | if $w != null and .parameters then .parameters |= with_entries(select(.key as $k | ($w.parameters // {}) | has($k))) else . end)' "$readback" > "$scratch/actual"
diff "$scratch/expected" "$scratch/actual"
)
```

```bash
(
set -euo pipefail
gh api user --jq .login
command -v jq >/dev/null
command -v gh >/dev/null
d=hosting/paperclip-hermes/rulesets; f=$d/org-release-notes-gate.json; p=$d/org-release-notes-gate.pin.json
jq -e . "$f" >/dev/null
path=$(jq -er .path "$p")
blob=$(jq -er .blob_sha "$p")
[[ -n $path && $blob =~ ^[0-9a-f]{40}$ ]] || { echo 'workflow pin is unresolved' >&2; exit 1; }
sha=$(gh api "repos/agenticcodingops/software-factory/commits?sha=main&path=$path&per_page=1" --jq '.[0].sha') || exit 1
[[ $sha =~ ^[0-9a-f]{40}$ ]] || { echo 'workflow commit is unresolved' >&2; exit 1; }
actual_blob=$(gh api "repos/agenticcodingops/software-factory/contents/$path?ref=$sha" --jq .sha) || exit 1
[ "$actual_blob" = "$blob" ] || { echo 'workflow at main differs from the reviewed pin' >&2; exit 1; }
repo_id=$(gh api repos/agenticcodingops/software-factory --jq .id) || exit 1
[[ $repo_id =~ ^[1-9][0-9]*$ ]] || { echo 'repository ID is unresolved' >&2; exit 1; }
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT
body=$scratch/body
readback=$scratch/readback
jq --arg sha "$sha" --argjson id "$repo_id" '.enforcement = "active" | .rules[0].parameters.workflows[0] |= (.sha = $sha | .repository_id = $id)' "$f" > "$body"
id=$(gh api 'orgs/agenticcodingops/rulesets?per_page=100' --jq '.[] | select(.name=="software-factory-release-notes-gate") | .id') || exit 1
[[ -z $id || $id =~ ^[1-9][0-9]*$ ]] || { echo 'ruleset lookup is ambiguous or failed' >&2; exit 1; }
if [ -z "$id" ]; then id=$(gh api --method POST orgs/agenticcodingops/rulesets --input "$body" --jq .id) || exit 1
else gh api --method PUT "orgs/agenticcodingops/rulesets/$id" --input "$body" >/dev/null || exit 1; fi
[[ $id =~ ^[1-9][0-9]*$ ]] || { echo 'ruleset ID is unresolved' >&2; exit 1; }
gh api "orgs/agenticcodingops/rulesets/$id" > "$readback" || exit 1
jq -S '{name,target,enforcement,bypass_actors,conditions,rules} | del(.bypass_actors[] | select(.actor_type == "OrganizationAdmin" and .actor_id == null) | .actor_id)' "$body" > "$scratch/expected"
jq -S '{name,target,enforcement,bypass_actors,conditions,rules} | del(.bypass_actors[] | select(.actor_type == "OrganizationAdmin" and .actor_id == null) | .actor_id)' "$readback" > "$scratch/actual"
diff "$scratch/expected" "$scratch/actual"
)
```

For `software-factory-release-tags`, wait until the dedicated release team exists.
Never apply the tag payload while `actor_id` is 0. Until the team exists, leave this
ruleset uninstalled and instances must not consume `github-tags` updates. Render the
team ID into a temporary body and never commit it:

```bash
(
set -euo pipefail
gh api user --jq .login
command -v jq >/dev/null
command -v gh >/dev/null
f=hosting/paperclip-hermes/rulesets/public-release-tags.json
jq -e . "$f" >/dev/null
team=$(gh api "orgs/agenticcodingops/teams/$RELEASE_TEAM_SLUG" --jq .id) || { echo 'release team lookup failed' >&2; exit 1; }
[[ $team =~ ^[1-9][0-9]*$ ]] || { echo 'release team is unresolved' >&2; exit 1; }
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT
body=$scratch/body
readback=$scratch/readback
jq --argjson team "$team" '(.bypass_actors[] | select(.actor_type=="Team") | .actor_id) = $team | .enforcement = "active"' "$f" > "$body"
id=$(gh api 'repos/agenticcodingops/software-factory/rulesets?per_page=100&includes_parents=false' --jq '.[] | select(.name=="software-factory-release-tags") | .id') || exit 1
[[ -z $id || $id =~ ^[1-9][0-9]*$ ]] || { echo 'ruleset lookup is ambiguous or failed' >&2; exit 1; }
if [ -z "$id" ]; then id=$(gh api --method POST repos/agenticcodingops/software-factory/rulesets --input "$body" --jq .id) || exit 1
else gh api --method PUT "repos/agenticcodingops/software-factory/rulesets/$id" --input "$body" >/dev/null || exit 1; fi
[[ $id =~ ^[1-9][0-9]*$ ]] || { echo 'ruleset ID is unresolved' >&2; exit 1; }
gh api "repos/agenticcodingops/software-factory/rulesets/$id" > "$readback" || exit 1
jq -S '{name,target,enforcement,bypass_actors,conditions,rules} | del(.bypass_actors[] | select(.actor_type == "OrganizationAdmin" and .actor_id == null) | .actor_id)' "$body" > "$scratch/expected"
jq -S '{name,target,enforcement,bypass_actors,conditions,rules} | del(.bypass_actors[] | select(.actor_type == "OrganizationAdmin" and .actor_id == null) | .actor_id)' "$readback" > "$scratch/actual"
diff "$scratch/expected" "$scratch/actual"
)
```

The expected result is an empty diff and exit status 0. Each filter drops
`"actor_id": null` returned for an OrganizationAdmin bypass actor; the
[API ignores actor_id for that type](https://docs.github.com/en/rest/repos/rules#create-a-repository-ruleset).
A Team actor's null or changed ID still appears in the diff.
For `software-factory-main`, the readback side also keeps only the rule parameters
set by the committed payload because GitHub returns extra parameters:
`dismissal_restriction`, `require_extra_approval_for_unattributed_changes`,
`required_reviewers` and `do_not_enforce_on_create`. These extra parameters were observed on
2026-11-27 in GitHub ruleset readbacks of existing repository rulesets in the same
organisation; the [repository ruleset response documentation](https://docs.github.com/en/rest/repos/rules#get-a-repository-ruleset)
lists all four.
Every committed field, and any rule type absent from the payload, is still compared.
Any remaining diff line means
the apply failed unless it is a documented API default, which the operator records in
this README in a reviewed PR. Do not normalize other fields without a real readback.
Check listings for duplicate names and pagination before applying: the
[repository rules API](https://docs.github.com/en/rest/repos/rules) supports `includes_parents`
and caps `per_page` at 100. Use the [teams API](https://docs.github.com/en/rest/teams/teams)
to verify membership. A later workflow change needs the blob pin updated in a reviewed
PR and the gate ruleset re-applied after merge. Renovate action bumps to the gate workflow
arrive as a separate `needs-operator` PR because its content is blob-pinned; update
`blob_sha` in `org-release-notes-gate.pin.json` in that same PR using
`git hash-object .github/workflows/release-notes-gate.yml`. Other workflow files
retain the verified pin-only exception.
