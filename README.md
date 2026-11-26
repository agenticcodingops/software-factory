# software-factory

Tooling and instructions for AI agents to plan, develop, test and release with extreme quality controls.

The first component under way is an automated, reusable deployment of Paperclip and Hermes to Azure:
Terraform with Azure Verified Modules, GitHub Actions over OIDC, automated upgrades, backups and
subscription moves. See [ADR 0001](docs/adr/0001-azure-deployment.md).

WP1 provides the repository scaffold, CI, release automation and ruleset payloads. The
[Compose kit](hosting/paperclip-hermes/kit/README.md) is already imported. Automated Azure deployment,
the instance merge gate and the bootstrap command remain future work; this scaffold is not deployable.

Repository paths follow the ADR: kit, Terraform, tools, bootstrap and rulesets live under
`hosting/paperclip-hermes/`; reusable workflows live directly in `.github/workflows/`.

## Local checks

On Linux amd64, install Python 3.11, Node 22.23.5, Terraform at the version in
[.terraform-version](.terraform-version), ShellCheck, Bats, curl, unzip and Git. Then:

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r ci/requirements.txt
npm ci --ignore-scripts --no-audit --no-fund
bash ci/install-tools.sh /tmp/software-factory-bin
export PATH="/tmp/software-factory-bin:$PATH"
bash ci/check.sh
```

The same entry point runs in CI on every push and PR, including fork PRs, with read-only
repository access and no secrets. It checks line endings and JSON, immutable action refs with
version comments, job permissions, [the deny-list](deny-list/README.md) and its planted-value tests,
the kit's unit tests, actionlint (including both existing Claude workflows), zizmor, gitleaks,
Markdown, every shell script, Bats, Terraform formatting, TFLint, Checkov and the Renovate validator.
The sole zizmor exception is documented in [zizmor.yml](zizmor.yml): the unchanged mention workflow
keeps checkout credentials but uploads no checkout artifacts. No workflow is excluded from actionlint.
Markdown line length is off; the config documents compatibility settings for the existing docs.
After regenerating the npm lock file, run `python3 ci/normalize-npm-lock.py` to remove optional
registry deprecation messages that include upstream maintainers' personal e-mail addresses.
Package versions, integrity hashes and dependency resolution are preserved.

Terraform stacks and Renovate presets are absent in WP1. Their checks pass on this empty scaffold,
and the Renovate validator runs against an empty fixture. Once a stack exists, CI requires a tracked
`.terraform.lock.hcl`, initializes without a backend or lock changes, validates and runs `terraform test`,
then regenerates provider checksums for **linux_amd64 and windows_amd64** and rejects drift.
Root providers and registry modules must use exact version pins; Git modules must pin a full commit.
Reusable modules follow the ADR's bounded provider-range convention. Mock provider tests, the policy,
plan and ref-bump tools and their tests join CI when their work packages add them.
Container e2e, Azure and GitHub settings checks require later packages and operator infrastructure.
The kit unit tests use sudo when needed to exercise root-only ownership protections.

## Releases and protection

Use conventional commits such as `feat(ci): add checks` and `fix(deps): update a provider`.
Release Please starts at `v0.1.0`, maintains a release PR and changelog, and creates `vX.Y.Z` tags
when a release PR merges to `main`. The release workflow attaches a kit tarball and SHA-256 checksum.
It requires an operator-provisioned **machine-user** `RELEASE_PLEASE_TOKEN` with repository contents
and pull-request write access. A machine-user token lets the release PR trigger CI, as required by
the ADR; no release PR is opened with `GITHUB_TOKEN`. Configure this secret before the first main push
or dispatch. The workflow fails visibly if it is missing. Never put token values in the repository.

Review the [ruleset payloads and apply/readback procedure](hosting/paperclip-hermes/rulesets/README.md).
Only the public payload is ready for operator application. The instance and org templates remain
disabled until the later bootstrap and trusted, release-pinned merge gate exist. WP5 supplies
`sf-bootstrap.sh rulesets [--apply]`; WP1 does not modify repository or organization settings.

See [SECURITY.md](SECURITY.md) for private vulnerability reporting and public-tree identifier rules.

Licensed under the MIT License (see `LICENSE`).
