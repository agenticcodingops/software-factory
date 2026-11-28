#!/usr/bin/env python3
"""Operator-only bootstrap. No cloud credentials or key values are persisted.

Only non-secret configuration/receipts and crash-recovery assignment/lock ledgers
are written, in an explicitly selected private directory outside this checkout.
Terraform fallback state is isolated for explicit recovery; working trees are removed.
Subprocess diagnostics are captured: SDK errors can echo secret request bodies.
"""
import argparse
import base64
import contextlib
import fcntl
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import tempfile
import uuid
import zipfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
ENV = {"plan": "plan", "deploy": "production", "ops": "ops", "restore": "restore"}
ISSUER = "https://token.actions.githubusercontent.com"
AUD = "api://AzureADTokenExchange"
CLAIMS = ["repo", "context", "job_workflow_ref"]
REF = re.compile(r"[\w.-]+/[\w.-]+/\.github/workflows/[\w-]+\.ya?ml@[a-f0-9]{40}")
TRUST_VARIABLES = {"prefix", "location", "vault_subscription_id", "vault_tenant_id", "target_tenant_id", "targets", "role_home_target", "target_client_id", "operator_object_id", "state_account_name", "sub_claim_prefix", "workflow_refs", "custody", "pilot_end_date", "container_retention_fallback"}
HOST_VARIABLES = {"name", "resource_group_name", "subscription_resource_id", "location", "rg_managed_by", "ssh_public_key", "bootstrap_custom_data", "protocol_tags", "apps", "vm_size", "shutdown_time", "backup_time", "timezone", "allow_disk_destroy", "parked", "tags", "deallocator_role_id", "operator_email", "tenant_id", "alert_application", "pilot_monthly_cap", "headroom", "override_cost", "monthly_credit", "billing_month_verified"}
PROVIDERS = ("Microsoft.Compute", "Microsoft.Network", "Microsoft.Logic", "Microsoft.Insights", "Microsoft.Consumption", "Microsoft.DevTestLab", "Microsoft.ManagedIdentity", "Microsoft.Storage", "Microsoft.KeyVault")
INTERRUPTS = {signal.SIGINT, signal.SIGTERM, signal.SIGHUP}
CHILD_ENV = {"PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "XDG_CONFIG_HOME", "AZURE_CONFIG_DIR"}
OUTPUT_FAILED = False


def progress(*args, **kwargs):
    """Logging must never prevent a mutation or its cleanup after terminal loss."""
    global OUTPUT_FAILED
    if OUTPUT_FAILED:
        return
    stream = kwargs.get("file", sys.stdout)
    try:
        print(*args, **{**kwargs, "flush": True})
    except OSError:
        OUTPUT_FAILED = True
        # Interpreter shutdown also flushes stdout. Redirect its descriptor so
        # a broken pipe cannot fail that final flush after successful cleanup.
        try:
            with open(os.devnull, "w", encoding="utf-8") as sink:
                os.dup2(sink.fileno(), stream.fileno())
        except (OSError, ValueError):
            pass


def child_environment(adapter=False):
    allowed = {"PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TZ"} if adapter else CHILD_ENV
    return {k: v for k, v in os.environ.items() if k in allowed}


class Refusal(Exception):
    pass


def require(test, message):
    if not test:
        raise Refusal(message)


def pack(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def tool(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), HERE.parent / "tools" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def subset(expected, actual):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and subset(v, actual[k]) for k, v in expected.items())
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(expected) != len(actual):
            return False
        remaining = list(actual)
        for item in expected:
            found = next((i for i, candidate in enumerate(remaining) if subset(item, candidate)), None)
            if found is None:
                return False
            remaining.pop(found)
        return True
    return expected == actual


class Bootstrap:
    def __init__(self, args):
        self.a = args
        self.home = Path(args.instance_dir).resolve()
        require(not self.home.is_relative_to(ROOT), "Select a private --instance-dir outside the public checkout")
        self.config = self.load("bootstrap.json", {})
        self.ledger = self.load("assignments.json", [])
        self.lock_ledger = self.load("locks.json", [])
        self.token = None
        self.tf_session = None
        self.tf_trees = {}
        self.tf_run = None
        self.trust_inventory = None
        self.gh_verified = False
        self.pending_files = {}

    def load(self, name, default=None):
        path = self.home / name
        require(path.resolve().is_relative_to(self.home), "Private configuration path escapes instance directory")
        return json.loads(path.read_text()) if path.exists() else default

    def save(self, name, value, plain=False):
        require(self.a.apply, "Internal error: write in dry-run")
        path = self.home / name
        require(path.parent.resolve().is_relative_to(self.home), "Private configuration path escapes instance directory")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        require(not path.is_symlink(), "Refusing symlink destination")
        temporary = path.with_name(path.name + ".new-" + uuid.uuid4().hex)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(value.encode() if plain else pack(value) + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            require((path.read_text() if plain else json.loads(path.read_bytes())) == value, "Local configuration readback failed")
        finally:
            temporary.unlink(missing_ok=True)

    def run(self, *args, data=None, change=False, raw=False, pass_fds=(), env=None):
        progress(("APPLY " if change else "READ ") + shlex.join(map(str, args)) +
              (" < [in-memory stdin]" if data is not None else ""), flush=True)
        if change and not self.a.apply:
            return b"" if raw else {}
        environment = child_environment() if env is None else env.copy()
        if args[0] in ("az", "terraform"):
            environment.update(AZURE_LOGGING_ENABLE_LOG_FILE="false", AZURE_CORE_COLLECT_TELEMETRY="false")
        proc = subprocess.run(list(map(str, args)), input=data, stdin=subprocess.DEVNULL if data is None else None, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, check=False, pass_fds=pass_fds, env=environment)
        require(proc.returncode == 0, f"{args[0]} operation failed (diagnostics withheld; may contain secrets)")
        if raw:
            return proc.stdout
        return json.loads(proc.stdout) if proc.stdout.strip() else {}

    def az(self, *args, **kwargs):
        return self.run("az", *args, "--only-show-errors", "--output", "json", **kwargs)

    def gh(self, path, method="GET", body=None, raw=False):
        if method != "GET" and not self.gh_verified:
            expected = self.cfg("operator_login")
            user = self.run("gh", "api", "--hostname", "github.com", "user")
            require(user.get("type") == "User" and user.get("login", "").casefold() == expected.casefold(),
                    "GitHub operator login mismatch; use the operator's own gh auth login")
            self.gh_verified = True
        args = ["gh", "api", "--method", method, path]
        args += ["--hostname", "github.com"]
        if body is not None:
            args += ["--input", "/dev/stdin"]
        return self.run(*args, data=pack(body) if body is not None else None,
                        change=method != "GET", raw=raw)

    def setting(self, path, body):
        if "/actions/variables/" in path:
            parent, name = path.rsplit("/", 1)
            variables = self.gh(parent + "?per_page=100")
            require(variables.get("total_count", 0) < 100, "Variable inventory needs pagination")
            old = next((v for v in variables.get("variables", []) if v["name"] == name), None)
            if old != body:
                self.gh(path if old else parent, "PATCH" if old else "POST", body)
            if self.a.apply:
                require(subset(body, self.gh(path)), "Variable readback mismatch")
            return
        current = self.gh(path)
        if not subset(body, current):
            self.gh(path, "PUT", body)
        if self.a.apply:
            require(subset(body, self.gh(path)), "GitHub setting readback mismatch")

    def cfg(self, name):
        value = getattr(self.a, name, None) or self.config.get(name)
        require(value is not None and value != "", f"Missing --{name.replace('_', '-')} or bootstrap.json {name}")
        return value

    def repo(self):
        return f"{self.cfg('org')}/{self.cfg('instance_repo')}"

    def operator(self, subscription):
        account = self.az("account", "show", "--subscription", subscription)
        require(account.get("user", {}).get("type") == "user", "Operator's own az user login required")
        user = self.az("ad", "signed-in-user", "show", "--subscription", subscription)
        roles = self.az("role", "assignment", "list", "--assignee", user["id"],
                        "--scope", f"/subscriptions/{subscription}", "--include-inherited")
        require(any(r.get("roleDefinitionName") == "Owner" for r in roles), "Operator must be Owner")
        return user["id"], account["tenantId"]

    def retry(self, fn):
        for attempt in range(self.a.attempts):
            try:
                return fn()
            except Refusal:
                if attempt + 1 == self.a.attempts:
                    raise
                time.sleep(self.a.retry_delay)

    def grant(self, scope, role, subscription):
        principal, _ = self.operator(subscription)
        # Record the deterministic assignment ID BEFORE sending a mutation. Even
        # a lost HTTP response or SIGTERM leaves enough information for cleanup.
        name = str(uuid.uuid4())
        assignment = f"{scope}/providers/Microsoft.Authorization/roleAssignments/{name}"
        entry = {"id": assignment, "subscription": subscription}
        self.ledger.append(entry)
        if self.a.apply:
            self.save("assignments.json", self.ledger)
        self.az("role", "assignment", "create", "--name", name, "--assignee-object-id", principal,
                "--assignee-principal-type", "User", "--role", role, "--scope", scope,
                "--subscription", subscription, change=True)
        if self.a.apply:
            def verify():
                result = self.az("role", "assignment", "list", "--scope", scope, "--subscription", subscription)
                require(any(r.get("id", "").lower() == assignment.lower() and
                            r.get("principalId") == principal and r.get("roleDefinitionName") == role and
                            r.get("scope", "").lower() == scope.lower() for r in result), "Temporary grant has not propagated")
            self.retry(verify)

    def storage_grant(self, resource, role, subscription):
        # Account deletion locks inherit down to container RBAC assignments.
        # A temporary grant must be removable without touching those locks.
        match = re.fullmatch(r"(/subscriptions/[^/]+/resourceGroups/[^/]+)(?:/providers/Microsoft.Storage/storageAccounts/[^/]+(?:/blobServices/default/containers/[^/]+)?)?", resource)
        require(match and match[1].startswith(f"/subscriptions/{subscription}/"), "Invalid temporary storage grant boundary")
        rg = match[1]
        locks = self.az("lock", "list", "--subscription", subscription)
        require(not any(lock.get("id", "").lower().split("/providers/microsoft.authorization/locks/")[0]
                        in (f"/subscriptions/{subscription}".lower(), rg.lower()) for lock in locks),
                "Temporary storage grant requires an unlocked vault RG with no inherited lock")
        self.grant(rg, role, subscription)

    def restore_locks(self):
        failures = []
        for entry in list(reversed(self.lock_ledger)):
            try:
                self.az("rest", "--method", "put", "--url",
                        "https://management.azure.com" + entry["id"] + "?api-version=2016-09-01",
                        "--body", json.dumps({"properties": entry["properties"]}),
                        "--subscription", entry["subscription"], change=True)
                def restored():
                    locks = self.az("lock", "list", "--subscription", entry["subscription"])
                    require(any(lock.get("id", "").lower() == entry["id"].lower() and
                                subset(entry["properties"], lock) for lock in locks), "Storage lock restoration readback failed")
                self.retry(restored)
                self.lock_ledger.remove(entry)
                self.save("locks.json", self.lock_ledger)
            except (Refusal, ValueError, KeyError, OSError, subprocess.SubprocessError):
                failures.append(entry)
        require(not failures, "Storage lock restoration readback failed: rerun cleanup --apply using locks.json before continuing")

    def unlock_role_scopes(self, scopes):
        # CanNotDelete locks inherit to RBAC assignments. Persist recovery data
        # before unlocking, and restore in cleanup even after a failed destroy.
        affected = []
        for subscription in sorted({scope.split("/")[2] for scope in scopes}):
            locks = self.az("lock", "list", "--subscription", subscription)
            for lock in locks:
                lock_scope = lock["id"].lower().split("/providers/microsoft.authorization/locks/")[0]
                if not any(scope.lower() == lock_scope or scope.lower().startswith(lock_scope + "/") for scope in scopes):
                    continue
                require(lock.get("level") == "CanNotDelete" and re.fullmatch(
                    r"/subscriptions/[^/]+/resourcegroups/[^/]+/providers/microsoft.storage/storageaccounts/[^/]+", lock_scope),
                    "Revocation can temporarily remove only storage account CanNotDelete locks")
                affected.append({"id": lock["id"], "subscription": subscription,
                                 "properties": {k: lock[k] for k in ("level", "notes", "owners") if lock.get(k) is not None}})
        # Validate the entire inventory and operator access before any unlock.
        for subscription in sorted({entry["subscription"] for entry in affected}):
            self.operator(subscription)
        for entry in affected:
            self.lock_ledger.append(entry)
            self.save("locks.json", self.lock_ledger)
            self.az("lock", "delete", "--ids", entry["id"], "--subscription", entry["subscription"], change=True)
            def absent():
                locks = self.az("lock", "list", "--subscription", entry["subscription"])
                require(not any(lock.get("id", "").lower() == entry["id"].lower() for lock in locks),
                        "Storage lock removal readback failed")
            self.retry(absent)

    def cleanup(self):
        if not self.a.apply:
            if self.lock_ledger:
                progress("DRY-RUN: restore every storage lock in locks.json and verify its level/notes before clearing the ledger")
            for entry in reversed(self.ledger):
                self.az("role", "assignment", "delete", "--ids", entry["id"], "--subscription", entry["subscription"], change=True)
            if self.ledger:
                progress("DRY-RUN: read back removal of every temporary assignment; retain ledger if cleanup fails")
            return
        lock_failure = None
        try:
            self.restore_locks()
        except Refusal as exc:
            lock_failure = exc
        failures = []
        for entry in list(reversed(self.ledger)):
            try:
                self.az("role", "assignment", "delete", "--ids", entry["id"],
                        "--subscription", entry["subscription"], change=True)
                def absent():
                    remaining = self.az("role", "assignment", "list", "--scope", entry["id"].split("/providers/Microsoft.Authorization")[0],
                                        "--subscription", entry["subscription"])
                    require(not any(r.get("id", "").lower() == entry["id"].lower() for r in remaining),
                            "Temporary assignment still present")
                self.retry(absent)
                self.ledger.remove(entry)
                self.save("assignments.json", self.ledger)
            except (Refusal, ValueError, KeyError, OSError, subprocess.SubprocessError):
                failures.append(entry)
        if lock_failure:
            raise lock_failure
        require(not failures, "CLEANUP FAILED: rerun cleanup --apply using assignments.json before continuing")

    def gate_check(self):
        receipt = self.load("config/receipts/bootstrap.json", {})
        require(receipt.get("gates_verified") is True, "init reviewer probe must be verified first")
        self.check_environments(receipt["reviewers"])
        template = self.gh(f"repos/{self.repo()}/actions/oidc/customization/sub")
        require(template.get("include_claim_keys") == CLAIMS and template.get("use_immutable_subject") is True,
                "OIDC template/immutable subject readback mismatch")

    def check_environments(self, reviewers):
        for env in ENV.values():
            result = self.gh(f"repos/{self.repo()}/environments/{env}")
            policy = result.get("deployment_branch_policy")
            if env == "plan":
                require(policy is None, "plan must have no branch restriction")
            else:
                require(result.get("can_admins_bypass") is False, "Environment admin bypass must be disabled")
                require(policy == {"protected_branches": False, "custom_branch_policies": True}, "Branch protection readback mismatch")
                branches = self.gh(f"repos/{self.repo()}/environments/{env}/deployment-branch-policies")["branch_policies"]
                require([(b["name"], b.get("type", "branch")) for b in branches] == [("main", "branch")], "Only main may deploy")
            rules = result.get("protection_rules", [])
            review = [r for r in rules if r.get("type") == "required_reviewers"]
            if env in ("production", "restore"):
                require(len(review) == 1 and review[0].get("prevent_self_review") == (len(reviewers) >= 2)
                        and sorted(r["reviewer"]["id"] for r in review[0]["reviewers"]) == sorted(reviewers),
                        "Reviewer protection-rule readback mismatch")
            else:
                require(not review, "plan/ops must remain unattended")

    def init(self):
        org = self.cfg("org")
        organization = self.gh(f"orgs/{org}")
        require(organization.get("plan", {}).get("name") == "enterprise", "Verified GitHub Enterprise required; Free/Pro/Team/unknown refused")
        policy = self.gh(f"orgs/{org}/actions/permissions")
        require(policy.get("enabled_repositories") in ("all", "selected"), "Change org Actions enabled_repositories to selected or all")
        allowed = policy.get("allowed_actions") == "all"
        if policy.get("allowed_actions") == "selected":
            import fnmatch
            source = self.a.source_repo or self.config.get("source_repo")
            sha = self.a.release_sha or self.config.get("release_sha")
            if source and sha:
                patterns = self.gh(f"orgs/{org}/actions/permissions/selected-actions").get("patterns_allowed", [])
                allowed = any(fnmatch.fnmatchcase(f"{source}/.github/workflows/sf-oidc-probe.yml@{sha}", p) for p in patterns)
        require(allowed, "Change org Actions allowed_actions to explicitly allow the pinned source workflows")
        reviewers = [self.gh(f"users/{u}")["id"] for u in self.cfg("reviewer").split(",")]
        require(1 <= len(reviewers) <= 6 and len(reviewers) == len(set(reviewers)), "Select one to six distinct reviewers")
        existing = self.gh(f"orgs/{org}/repos?per_page=100")
        require(len(existing) < 100, "Paginate the organization inventory before bootstrap (ambiguous repo existence)")
        matches = [r for r in existing if r["name"] == self.cfg("instance_repo")]
        if not matches:
            self.gh(f"orgs/{org}/repos", "POST", {"name": self.cfg("instance_repo"), "private": True, "auto_init": True})
            if not self.a.apply:
                progress("DRY-RUN: create missing template files, CODEOWNERS, immutable OIDC template; configure plan/ops/production/restore; read back protections and dispatch reviewer probe")
                return
        if self.a.apply:
            repo = self.gh(f"repos/{self.repo()}")
            require(repo.get("private") is True, "Instance repository must be private")
            if policy["enabled_repositories"] == "selected":
                enabled = self.gh(f"orgs/{org}/actions/permissions/repositories?per_page=100")
                require(any(r["id"] == repo["id"] for r in enabled.get("repositories", [])), "Change org Actions selected repositories to enable this instance")
            if not matches and repo.get("default_branch") != "main":
                self.gh(f"repos/{self.repo()}/branches/{repo['default_branch']}/rename", "POST", {"new_name": "main"})
                repo = self.gh(f"repos/{self.repo()}")
            require(repo.get("default_branch") == "main", "Instance default branch must be main")
            self.config.update(org=org, instance_repo=self.cfg("instance_repo"), operator_login=self.cfg("operator_login"))
            for key in ("source_repo", "release_sha"):
                if getattr(self.a, key):
                    self.config[key] = getattr(self.a, key)
            self.save("bootstrap.json", self.config)
            for path in sorted((ROOT / "templates/instance").rglob("*")):
                if path.is_file():
                    self.put_file(str(path.relative_to(ROOT / "templates/instance")), path.read_text(), create_only=True)
            if self.load("config/instance.tfvars.json") is None:
                self.save("config/instance.tfvars.json", json.loads((ROOT / "templates/instance/config/instance.tfvars.json").read_text()))
            owners = " ".join("@" + u for u in self.cfg("reviewer").split(","))
            self.put_file(".github/CODEOWNERS", f".github/** {owners}\nconfig/** {owners}\n.github/workflows/ph-*.yml {owners}\n")
            self.publish_files()
        self.setting(f"repos/{self.repo()}/actions/oidc/customization/sub",
                     {"use_default": False, "include_claim_keys": CLAIMS, "use_immutable_subject": True})
        for env in ENV.values():
            body = {"deployment_branch_policy": None if env == "plan" else {"protected_branches": False, "custom_branch_policies": True}}
            if env != "plan":
                body["can_admins_bypass"] = False
            if env in ("production", "restore"):
                body.update(reviewers=[{"type": "User", "id": u} for u in reviewers],
                            prevent_self_review=len(reviewers) >= 2)
            self.gh(f"repos/{self.repo()}/environments/{env}", "PUT", body)
            if env != "plan":
                path = f"repos/{self.repo()}/environments/{env}/deployment-branch-policies"
                branches = self.gh(path).get("branch_policies", [])
                require(not any(b["name"] != "main" or b.get("type", "branch") != "branch" for b in branches), "Unexpected deployment branch policy")
                if not branches:
                    self.gh(path, "POST", {"name": "main", "type": "branch"})
        if not self.a.apply:
            progress("DRY-RUN: verify environment readbacks, reviewer wait probe, then completed OIDC probes; no receipt")
            return
        self.check_environments(reviewers)
        # Required workflows are a separate capability from environment reviewers.
        try:
            self.gh(f"orgs/{org}/rulesets")
            # Listing rulesets does not prove required-workflow capability. Only
            # a successful pinned workflow ruleset readback can enable it.
            verified = self.load("config/receipts/rulesets.json", {})
            merge_mode = "required-workflow" if verified.get("gate_sha") else "operator-only"
        except Refusal:
            merge_mode = "operator-only"
        self.save("config/receipts/setup.json", {"reviewers": reviewers, "merge_mode": merge_mode})
        if not self.a.probe_run:
            self.dispatch_probe(initial=True)
            self.save("config/receipts/bootstrap.json", {"gates_verified": False})
            raise Refusal("Setup incomplete: rerun init --probe-run RUN after both production and restore are waiting for review")
        run = self.probe_metadata(self.a.probe_run)
        pending = self.gh(f"repos/{self.repo()}/actions/runs/{run['id']}/pending_deployments")
        require({p["environment"]["name"] for p in pending} == {"production", "restore"}, "Harmless probe did not wait for both reviewer gates")
        require(all(p.get("reviewers") for p in pending), "Reviewer probe has no reviewers")
        self.save("config/receipts/bootstrap.json", {"gates_verified": True, "reviewers": reviewers,
                                                     "probe_run": run["id"], "merge_mode": merge_mode})
        progress("Reviewer gates verified. Approve the harmless probe, then run oidc-probe --probe-run RUN to collect claims.")

    def put_file(self, path, content, create_only=False):
        endpoint = f"repos/{self.repo()}/contents/{path}"
        # Listing parent directories avoids interpreting any API failure as 404.
        entries = self.gh(f"repos/{self.repo()}/git/trees/main?recursive=1")
        require(not entries.get("truncated"), "Repository tree is truncated")
        old = next((v for v in entries["tree"] if v["path"] == path), None)
        if old and create_only:
            return
        encoded = base64.b64encode(content.encode()).decode()
        if old:
            current = self.gh(endpoint + "?ref=main")
            if base64.b64decode(current["content"]) == content.encode():
                return
        self.pending_files[path] = encoded

    def publish_files(self):
        """Stage a deterministic PR and resume only once its contents are on main."""
        if not self.pending_files:
            return
        files = self.pending_files
        self.pending_files = {}
        branch = "sf-bootstrap-" + digest(pack(files))[:20]
        endpoint = f"repos/{self.repo()}"
        refs = self.gh(f"{endpoint}/git/matching-refs/heads/{branch}")
        exists = any(r["ref"] == "refs/heads/" + branch for r in refs)
        if not self.a.apply:
            progress(f"DRY-RUN: stage {len(files)} file(s) on {branch} ({'existing' if exists else 'new'} branch), open a bootstrap PR, then merge before dispatching the probe")
            return
        if not exists:
            main = self.gh(f"{endpoint}/git/ref/heads/main")["object"]["sha"]
            self.gh(f"{endpoint}/git/refs", "POST", {"ref": "refs/heads/" + branch, "sha": main})
        tree = self.gh(f"{endpoint}/git/trees/{branch}?recursive=1")
        require(not tree.get("truncated"), "Bootstrap PR tree is truncated")
        entries = {e["path"]: e for e in tree["tree"]}
        for path, encoded in files.items():
            body = {"message": "chore: configure operator bootstrap", "content": encoded, "branch": branch}
            if path in entries:
                current = self.gh(f"{endpoint}/contents/{path}?ref={branch}")
                if base64.b64decode(current["content"]) == base64.b64decode(encoded):
                    continue
                body["sha"] = entries[path]["sha"]
            self.gh(f"{endpoint}/contents/{path}", "PUT", body)
            if self.a.apply:
                require(base64.b64decode(self.gh(f"{endpoint}/contents/{path}?ref={branch}")["content"]) == base64.b64decode(encoded),
                        "Bootstrap PR file readback mismatch")
        pulls = self.gh(f"{endpoint}/pulls?state=open&head={self.cfg('org')}:{branch}&base=main")
        if not pulls:
            self.gh(f"{endpoint}/pulls", "POST", {"title": "chore: configure operator bootstrap", "head": branch, "base": "main",
                    "body": "Review the bootstrap configuration and pinned probe caller, then merge through the repository rules. Rerun bootstrap after merging."})
        raise Refusal(f"Merge bootstrap pull request from {branch} into main, then rerun the same command")

    def dispatch_probe(self, initial=False):
        refs = ({i: {"bootstrap": f"{self.cfg('source_repo')}/.github/workflows/sf-oidc-probe.yml@{self.cfg('release_sha')}"} for i in ENV}
                if initial else self.load("config/instance.tfvars.json", {}).get("workflow_refs", {}))
        require(refs, "Owner must supply workflow_refs before probing")
        pending = self.load("probe-pending.json", {})
        nonce = pending["nonce"] if pending.get("refs") == refs else str(uuid.uuid4())
        if self.a.apply:
            self.save("probe-pending.json", {"nonce": nonce, "refs": refs})
        jobs = []
        for identity, entries in refs.items():
            require(identity in ENV and len(entries) <= 20, "Unknown identity or too many federations")
            for key, ref in entries.items():
                require(REF.fullmatch(ref), "Probe requires full SHA reusable workflow refs")
                require(re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", key), "Invalid workflow entry name")
                jobs.append(f"  {identity}-{key}:\n    permissions:\n      contents: read\n      id-token: write\n    uses: {ref} # v1.0.0\n    with:\n      environment: {ENV[identity]}\n      probe_only: true\n      nonce: {nonce}\n")
        text = "name: Bootstrap OIDC probe\non: workflow_dispatch\npermissions: {}\njobs:\n" + "".join(jobs)
        self.put_file(".github/workflows/sf-bootstrap-probe.yml", text)
        self.publish_files()
        if not self.a.apply:
            progress("DRY-RUN: after the probe caller is merged, dispatch on main and collect the approved run's claims; no receipt")
            return
        if self.a.apply:
            caller = self.gh(f"repos/{self.repo()}/git/ref/heads/main")["object"]["sha"]
            self.save("probe-request.json", {"nonce": nonce, "refs": refs, "caller_sha": caller})
        self.gh(f"repos/{self.repo()}/actions/workflows/sf-bootstrap-probe.yml/dispatches", "POST", {"ref": "main"})
        if self.a.apply:
            (self.home / "probe-pending.json").unlink(missing_ok=True)
        progress("Probe dispatched on main. Inspect its run ID; no federation is enabled yet.")

    def probe_metadata(self, run_id):
        run = self.gh(f"repos/{self.repo()}/actions/runs/{run_id}")
        require(str(run.get("id")) == str(run_id) and run.get("event") == "workflow_dispatch" and
                run.get("head_branch") == "main" and run.get("path") == ".github/workflows/sf-bootstrap-probe.yml",
                "Untrusted probe run metadata")
        request = self.load("probe-request.json", {})
        require(request.get("caller_sha") == run.get("head_sha"), "Probe caller commit differs from dispatched request")
        expected = {r for entries in request["refs"].values() for r in entries.values()}
        actual = {r["path"] for r in run.get("referenced_workflows", [])}
        require(expected == actual, "Run does not reference exactly the Owner-authorized probe workflows")
        return run

    def collect_claims(self):
        request = self.load("probe-request.json", {})
        require(request, "Missing locally dispatched probe request")
        run = self.probe_metadata(self.cfg("probe_run"))
        require(run.get("conclusion") == "success", "OIDC probe must complete successfully after approval")
        archive = self.gh(f"repos/{self.repo()}/actions/runs/{run['id']}/logs", raw=True)
        records = []
        with zipfile.ZipFile(io.BytesIO(archive)) as logs:
            for name in logs.namelist():
                # GitHub includes both per-step and per-job logs; deduplicate records.
                for line in logs.read(name).decode().splitlines():
                    marker = "SF_OIDC_CLAIMS="
                    if marker in line and line.split(marker, 1)[1].startswith("{"):
                        value = json.loads(line.split(marker, 1)[1])
                        if value not in records:
                            records.append(value)
        require(records, "No OIDC claim records in probe logs")
        expected = [(i, key, ref) for i, entries in request["refs"].items() for key, ref in entries.items()]
        require(len(records) == len(expected), "Unexpected or missing probe claim records")
        repo = self.gh(f"repos/{self.repo()}")
        # The prefix is TAKEN FROM THE TOKEN; numeric IDs are checked against repo API.
        prefixes, resolved = set(), {}
        for identity, key, ref in expected:
            found = [c for c in records if c.get("environment") == ENV[identity] and c.get("job_workflow_ref") == ref]
            require(len(found) == 1, "Probe workflow/environment mismatch")
            claim = found[0]
            require(claim.get("nonce") == request["nonce"] and claim.get("iss") == ISSUER and claim.get("aud") == AUD,
                    "Probe nonce/issuer/audience mismatch")
            suffix = f":environment:{ENV[identity]}:job_workflow_ref:{ref}"
            require(claim.get("sub", "").endswith(suffix), "Probe sub does not bind exact workflow ref")
            prefix = claim["sub"][:-len(suffix)]
            require(prefix == f"repo:{repo['owner']['login']}@{repo['owner']['id']}/{repo['name']}@{repo['id']}",
                    "Probe must resolve immutable repository IDs")
            prefixes.add(prefix)
            resolved.setdefault(identity, {})[key] = {"sub": claim["sub"], "job_workflow_ref": ref}
        require(len(prefixes) == 1, "Inconsistent repository subjects")
        result = {"run_id": run["id"], "sub_claim_prefix": prefixes.pop(), "claims": resolved}
        if self.a.apply:
            self.save("config/receipts/oidc.json", result)
        return result

    def oidc_probe(self):
        self.gate_check()
        if self.a.probe_run:
            self.collect_claims()
        else:
            self.dispatch_probe()

    def federation(self):
        self.gate_check()
        receipt = self.load("config/receipts/oidc.json", {})
        require(receipt, "Run and collect environment-scoped oidc-probe first")
        variables = self.load("config/instance.tfvars.json", {})
        refs = variables.get("workflow_refs", {})
        require(refs, "Owner-authorized workflow_refs required")
        for identity, entries in refs.items():
            require(identity in ENV, "Unknown identity")
            for key, ref in entries.items():
                claim = receipt.get("claims", {}).get(identity, {}).get(key, {})
                require(REF.fullmatch(ref) and claim.get("job_workflow_ref") == ref and
                        claim.get("sub") == f"{receipt['sub_claim_prefix']}:environment:{ENV[identity]}:job_workflow_ref:{ref}",
                        "Owner allowlist differs from real probe claims; re-probe before federation")
        receipt["claims"] = {i: {k: receipt["claims"][i][k] for k in entries} for i, entries in refs.items()}
        return receipt, variables

    def print_subject(self):
        receipt, _ = self.federation()
        for identity in ("plan", "deploy"):
            for claim in receipt["claims"].get(identity, {}).values():
                progress(json.dumps(dict(claim, environment=ENV[identity], scopes=self.ts_scopes(identity),
                                      tags=[] if identity == "plan" else ["tag:pc-deployer"])))

    @staticmethod
    def ts_scopes(identity):
        return (["policy_file:read", "devices:core:read"] if identity == "plan" else
                ["policy_file", "devices:posture_attributes", "devices:core:read", "auth_keys", "devices:core"])

    def tf(self, stack, *args, data=None, variables=None, change=False):
        environment = child_environment()
        environment.update(ARM_USE_CLI="true", ARM_USE_OIDC="false", ARM_USE_MSI="false", ARM_STORAGE_USE_AZUREAD="true", TF_IN_AUTOMATION="true")
        session = self.tf_session or "operator"
        if self.a.apply:
            if session not in self.tf_trees:
                if self.tf_run is None:
                    self.tf_run = Path(tempfile.mkdtemp(prefix="terraform-run-", dir=self.home))
                base = self.tf_run / ("terraform" if session == "operator" else "terraform-sessions/" + session)
                source = HERE.parent / "terraform"
                for parent, directories, files in os.walk(source, followlinks=False):
                    directories[:] = [name for name in directories if name != ".terraform"]
                    require(all((Path(parent) / name).resolve().is_relative_to(source)
                                for name in directories + files if (Path(parent) / name).is_symlink()),
                            "Terraform source symlink escapes renderer source")
                inventory = self.trust_inventory or self.load("config/instance.tfvars.json", {}).get("targets")
                if stack == "trust":
                    require(inventory, "Trust rendering requires the complete registered target inventory")
                    tool("render-trust").render(source, base, inventory)
                else:
                    tool("render-host").render(source, base)
                self.tf_trees[session] = base
            directory = self.tf_trees[session] / "stacks" / stack
        else:
            directory = ROOT / "hosting/paperclip-hermes/terraform/stacks" / stack
            progress("DRY-RUN: render fresh contained Terraform tree with all target subscriptions; preserve disk protection")
        var_path = None
        try:
            if variables is not None:
                if not self.a.apply:
                    progress("DRY-RUN: supply variables using a private 0600 .tfvars.json file, then delete it")
                    return self.run("terraform", f"-chdir={directory}", *args, change=True, env=environment, raw=True)
                fd, name = tempfile.mkstemp(suffix=".tfvars.json", dir=self.tf_run)
                var_path = Path(name)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(pack(variables))
                args = (*args, "-var-file=" + name)
                data = None
            try:
                return self.run("terraform", f"-chdir={directory}", *args, data=data, change=change, env=environment, raw=True)
            except (Refusal, OSError, subprocess.SubprocessError):
                self.recover_errored_state(directory)
                raise
        finally:
            if var_path:
                var_path.unlink(missing_ok=True)

    def recover_errored_state(self, directory):
        state = directory / "errored.tfstate"
        if state.exists():
            recovery = self.home / "recovery" / uuid.uuid4().hex
            recovery.mkdir(parents=True, mode=0o700)
            os.chmod(recovery.parent, 0o700)
            destination = recovery / "errored.tfstate"
            state.rename(destination)
            os.chmod(destination, 0o600)
            raise Refusal(f"Terraform could not persist state. Recovery required: terraform state push {destination} against the intended backend, then securely delete that file before continuing")

    def remove_tf_tree(self):
        if self.tf_run is not None:
            recovery_failure = None
            try:
                # Catch fallback state even when a signal interrupted the call.
                for state in self.tf_run.rglob("errored.tfstate"):
                    try:
                        self.recover_errored_state(state.parent)
                    except Refusal as exc:
                        recovery_failure = exc
                        progress("REFUSED: " + str(exc), file=sys.stderr)
            finally:
                shutil.rmtree(self.tf_run)
                self.tf_run = None
                self.tf_trees.clear()
            if recovery_failure:
                raise recovery_failure

    def trust_vars(self, variables):
        return {k: v for k, v in variables.items() if k in TRUST_VARIABLES}

    def backend(self, stack, value, migrate=False):
        for key in ("storage_account_name", "resource_group_name", "subscription_id", "tenant_id", "key"):
            require(value.get(key), f"Missing backend {key}")
        # Backend config has only non-secret IDs, no access keys or SAS.
        require(set(value) <= {"storage_account_name", "resource_group_name", "subscription_id", "tenant_id", "key", "container_name"}, "Credential/backend option not permitted")
        require(value.get("container_name", "tfstate") == "tfstate", "State backend must use tfstate container")
        hcl = "\n".join(f"{key} = {json.dumps(val)}" for key, val in value.items() if key != "container_name")
        hcl += '\ncontainer_name = "tfstate"\nuse_oidc = false\nuse_azuread_auth = true\n'
        return self.tf(stack, "init", "-migrate-state" if migrate else "-reconfigure", "-force-copy" if migrate else "-input=false",
                       "-lockfile=readonly", "-backend-config=/dev/stdin", data=hcl.encode(), change=True)

    def trust(self):
        oidc, variables = self.federation()
        target = self.cfg("target")
        require(re.fullmatch(r"[a-z][a-z0-9-]*", target), "Invalid target name")
        vault, sub = self.cfg("vault_subscription"), self.cfg("subscription")
        operator, vault_tenant = self.operator(vault)
        _, target_tenant = self.operator(sub)
        account = self.az("account", "show", "--subscription", sub)
        mode = self.a.vault_mode or variables.get("vault_mode", self.config.get("vault_mode", "separate-subscription"))
        require((mode == "same-subscription") == (vault == sub), "vault_mode and selected subscriptions disagree")
        require(mode != "same-subscription" or account.get("subscriptionPolicies", {}).get("spendingLimit") in ("Off", "CurrentPeriodOff") or self.a.accept_vault_risk,
                "Spending-limited same-subscription vault refused; --accept-vault-risk required")
        targets = variables.setdefault("targets", {})
        variables["custody"] = self.a.custody or variables.get("custody", "operator")
        previous = json.loads(json.dumps(targets))
        home = variables.get("role_home_target") or self.a.role_home_target
        require(not variables.get("role_home_target") or not self.a.role_home_target or variables["role_home_target"] == self.a.role_home_target,
                "role_home_target is immutable")
        require(not previous or home, "Existing targets require explicit --role-home-target selecting the original trust home")
        variables["role_home_target"] = home or target
        if self.a.remove_target:
            require(target != variables["role_home_target"], "The trust home must remain registered after host decommission")
            destroyed = self.load(f"config/receipts/destroy-{target}.json", {})
            require(destroyed.get("destroyed") is True and destroyed.get("target") == target, "Verified destruction receipt required before --remove-target")
            require(target in targets and len(targets) > 1, "Cannot remove unknown or last target")
            del targets[target]
        else:
            owner = ("host" if self.a.scope == "subscription" else "trust") if self.a.scope else previous.get(target, {}).get("rg_managed_by", "trust")
            targets[target] = {"subscription_resource_id": f"/subscriptions/{sub}", "resource_group_name": self.cfg("resource_group"),
                               "location": self.cfg("location"), "rg_managed_by": owner}
        require(variables.get("target_tenant_id", target_tenant) == target_tenant, "Multiple foreign target tenants require an Owner-reviewed root composition")
        require(variables["role_home_target"] in targets, "Trust home must be a registered target")
        if home in previous:
            require(all(previous[home][k] == targets[home][k] for k in ("subscription_resource_id", "location")), "Trust home subscription and region are immutable")
        self.trust_inventory = {**previous, **targets}
        # Owner CLI authentication must never select an OIDC plan twin's client.
        variables["target_client_id"] = None
        if self.a.close_pilot:
            self.pilot_config(variables)
            if self.a.apply:
                self.save("config/receipts/pilot-closure.json", {"inventory_verified": False, "temporary_roles_removed": False})

        prefix = variables.get("prefix")
        require(prefix and re.fullmatch(r"[a-z0-9]{1,17}", prefix), "Supply a 1-17 lowercase alphanumeric prefix in instance.tfvars.json")
        state = variables.get("state_account_name")
        require(state and re.fullmatch(r"[a-z0-9]{3,24}", state), "Supply state_account_name")
        rg = f"rg-{prefix}-vault"
        scope = f"/subscriptions/{vault}/resourceGroups/{rg}/providers/Microsoft.Storage/storageAccounts/{state}"
        for subscription in sorted({vault, *(t["subscription_resource_id"].split("/")[2] for t in targets.values())}):
            self.operator(subscription)
            for provider in PROVIDERS:
                result = self.az("provider", "show", "--namespace", provider, "--subscription", subscription)
                if result.get("registrationState") != "Registered":
                    self.az("provider", "register", "--namespace", provider, "--subscription", subscription, "--wait", change=True)
                    if self.a.apply:
                        self.retry(lambda: require(self.az("provider", "show", "--namespace", provider, "--subscription", subscription).get("registrationState") == "Registered", "Provider registration readback failed"))
        for subscription in sorted({t["subscription_resource_id"].split("/")[2] for t in targets.values()}):
            feature = self.az("feature", "show", "--namespace", "Microsoft.Compute", "--name", "EncryptionAtHost", "--subscription", subscription)
            if feature.get("properties", {}).get("state") != "Registered":
                self.az("feature", "register", "--namespace", "Microsoft.Compute", "--name", "EncryptionAtHost", "--subscription", subscription, change=True)
                if self.a.apply:
                    self.retry(lambda: require(self.az("feature", "show", "--namespace", "Microsoft.Compute", "--name", "EncryptionAtHost", "--subscription", subscription).get("properties", {}).get("state") == "Registered", "EncryptionAtHost registration has not completed; rerun Owner bootstrap"))
            # Azure requires re-registration of Compute after feature registration.
            self.az("provider", "register", "--namespace", "Microsoft.Compute", "--subscription", subscription, "--wait", change=True)
            if self.a.apply:
                self.retry(lambda: require(self.az("provider", "show", "--namespace", "Microsoft.Compute", "--subscription", subscription).get("registrationState") == "Registered", "Compute registration has not completed"))
        groups = self.az("group", "list", "--subscription", vault)
        if not any(g["name"] == rg for g in groups):
            self.az("group", "create", "--name", rg, "--location", variables["location"], "--subscription", vault, change=True)
            if self.a.apply:
                require(self.az("group", "show", "--name", rg, "--subscription", vault).get("name") == rg, "Vault RG readback failed")
        accounts = self.az("storage", "account", "list", "--resource-group", rg, "--subscription", vault)
        if not any(a["name"] == state for a in accounts):
            self.az("storage", "account", "create", "--name", state, "--resource-group", rg, "--subscription", vault,
                    "--location", variables["location"], "--sku", "Standard_GRS", "--kind", "StorageV2",
                    "--public-network-access", "Enabled", "--default-action", "Allow", "--default-to-oauth-authentication", "true",
                    "--bypass", "None",
                    "--allow-shared-key-access", "false", "--allow-blob-public-access", "false", "--https-only", "true", "--min-tls-version", "TLS1_2", change=True)
        if self.a.apply:
            actual = self.az("storage", "account", "show", "--name", state, "--resource-group", rg, "--subscription", vault)
            require(subset({"allowSharedKeyAccess": False, "allowBlobPublicAccess": False, "enableHttpsTrafficOnly": True, "minimumTlsVersion": "TLS1_2", "sku": {"name": "Standard_GRS"}, "publicNetworkAccess": "Enabled", "networkRuleSet": {"defaultAction": "Allow"}, "defaultToOAuthAuthentication": True}, actual), "Unsafe state storage readback")
        self.az("storage", "account", "blob-service-properties", "update", "--account-name", state, "--resource-group", rg,
                "--subscription", vault, "--enable-versioning", "true", "--enable-delete-retention", "true", "--delete-retention-days", "7",
                "--enable-container-delete-retention", "true", "--container-delete-retention-days", "7", change=True)
        if self.a.apply:
            require(subset({"isVersioningEnabled": True, "deleteRetentionPolicy": {"enabled": True, "days": 7}, "containerDeleteRetentionPolicy": {"enabled": True, "days": 7}},
                           self.az("storage", "account", "blob-service-properties", "show", "--account-name", state, "--resource-group", rg, "--subscription", vault)), "State retention readback mismatch")
        container_scope = scope + "/blobServices/default/containers/tfstate"
        # Management-plane container create avoids an account-wide data grant.
        self.az("resource", "create", "--id", container_scope, "--api-version", "2025-01-01", "--properties", '{"publicAccess":"None"}', change=True)
        if self.a.apply:
            require(self.az("resource", "show", "--ids", container_scope, "--subscription", vault).get("properties", {}).get("publicAccess") == "None", "State container readback failed")
        self.az("lock", "create", "--name", "state-cannot-delete", "--lock-type", "CanNotDelete", "--resource", scope, "--subscription", vault, change=True)
        if self.a.apply:
            require(any(x.get("name") == "state-cannot-delete" and x.get("level") == "CanNotDelete" for x in self.az("lock", "list", "--resource", scope, "--subscription", vault)), "State lock readback failed")
        self.storage_grant(scope, "Storage Blob Data Contributor", vault)
        # KV may not exist on first apply. Its RG scope includes only this instance.
        self.grant(f"/subscriptions/{vault}/resourceGroups/{rg}", "Key Vault Secrets Officer", vault)
        if self.a.apply:
            self.retry(lambda: self.az("storage", "blob", "list", "--account-name", state, "--container-name", "tfstate", "--auth-mode", "login", "--subscription", vault))
        if self.a.close_pilot:
            self.pilot_lock(variables, vault)
        variables.update(vault_subscription_id=vault, vault_tenant_id=vault_tenant, target_tenant_id=target_tenant,
                         operator_object_id=operator, sub_claim_prefix=oidc["sub_claim_prefix"])
        backend = {"storage_account_name": state, "resource_group_name": rg, "subscription_id": vault,
                   "tenant_id": vault_tenant, "key": "trust.tfstate"}
        self.backend("trust", backend)
        tfvars = self.trust_vars(variables)
        if not self.a.remove_target and targets[target]["rg_managed_by"] == "host":
            groups = self.az("group", "list", "--subscription", sub)
            if not any(g["name"] == self.cfg("resource_group") for g in groups):
                host_vars = self.load(f"config/host-{target}.tfvars.json", {})
                host_vars = {k: v for k, v in host_vars.items() if k in HOST_VARIABLES}
                host_backend = self.config.get("backends", {}).get("host-" + target)
                require(host_vars and host_backend, "Subscription scope needs private host variables/backend to create the host-owned RG before scoped grants")
                require(host_vars.get("rg_managed_by") == "host" and host_vars.get("resource_group_name") == self.cfg("resource_group")
                        and host_vars.get("subscription_resource_id") == f"/subscriptions/{sub}", "Host RG bootstrap configuration mismatch")
                self.tf("trust", "apply", "-input=false", "-auto-approve", "-target=module.trust.azurerm_role_definition.target",
                        variables=tfvars, change=True)
                if self.a.apply:
                    role_id = json.loads(self.tf("trust", "output", "-json", "deallocator_role_id"))
                    require(isinstance(role_id, str) and role_id.startswith("/subscriptions/"), "Invalid deallocator role ID")
                    host_vars["deallocator_role_id"] = role_id
                    self.save(f"config/host-{target}.tfvars.json", host_vars)
                else:
                    progress("DRY-RUN: record trust deallocator_role_id in private host inputs before host RG initialization")
                self.backend("host", host_backend)
                self.tf("host", "apply", "-input=false", "-auto-approve", "-target=module.host.module.workload_rg[0]",
                        variables=host_vars, change=True)
                if self.a.apply:
                    require(self.az("group", "show", "--name", self.cfg("resource_group"), "--subscription", sub).get("name") == self.cfg("resource_group"),
                            "Host RG creation readback failed")
        self.tf("trust", "apply", "-input=false", "-auto-approve", variables=tfvars, change=True)
        if self.a.apply:
            # A no-diff refresh plan reads all settings back through the providers.
            self.tf("trust", "plan", "-input=false", "-detailed-exitcode", variables=tfvars)
            auth = json.loads(self.tf("trust", "output", "-json", "authorization"))
            for identity, entries in variables["workflow_refs"].items():
                for key in entries:
                    credential = auth["federations"][identity][key]
                    require(credential["subject"] == oidc["claims"][identity][key]["sub"] and
                            credential["issuer"] == ISSUER and credential["audience"] == [AUD], "Azure federation differs from probe")
            clients = {"vault": {}, "target": {}}
            for identity in ENV:
                vault_identity = self.az("identity", "show", "--name", "id-sf-" + identity, "--resource-group", rg, "--subscription", vault)
                target_provider_subscription = targets[variables["role_home_target"]]["subscription_resource_id"].split("/")[2]
                target_identity = (vault_identity if vault_tenant == target_tenant else
                                   self.az("identity", "show", "--name", "id-sf-" + identity, "--resource-group", f"rg-{prefix}-trust", "--subscription", target_provider_subscription))
                clients["vault"][identity] = vault_identity["clientId"]
                clients["target"][identity] = target_identity["clientId"]
            self.retry(lambda: self.az("keyvault", "secret", "list", "--vault-name", "kv-" + prefix, "--subscription", vault))
            self.config.setdefault("backends", {})["trust"] = backend
            self.config.update(vault_subscription=vault, subscription=vault, backup_account=prefix + "backups", staging_account=prefix + "staging", key_vault_name="kv-" + prefix,
                               key_vault_id=f"/subscriptions/{vault}/resourceGroups/{rg}/providers/Microsoft.KeyVault/vaults/kv-{prefix}")
            self.save("bootstrap.json", self.config)
            self.save("config/instance.tfvars.json", variables)
            ops_refs = {}
            for ref in variables.get("workflow_refs", {}).get("ops", {}).values():
                for workflow in ("ph-ops", "ph-power", "ph-cost-guard", "ph-backup-verify"):
                    if "/" + workflow + ".yml@" in ref:
                        ops_refs[workflow] = ref
            require(not ops_refs or len(ops_refs) == 4, "Register all four exact ops-plane caller refs together")
            self.save(f"config/targets/{target}.json", dict(targets.get(target, {}), status="removed" if self.a.remove_target else "active",
                                                         tenant=target_tenant, client_ids=clients, role_home_target=variables["role_home_target"],
                                                         plan_inputs={"vault": {"client_id": clients["vault"]["plan"]}, "target": {"target_client_id": clients["target"]["plan"]}}, authorization=auth,
                                                         ops_workflow_refs=ops_refs))
            if ops_refs or self.a.remove_target:
                self.ops_callers(target, ops_refs, self.a.remove_target)
        if self.a.close_pilot:
            self.close_pilot(variables, vault)
        usage = self.az("vm", "list-usage", "--location", self.cfg("location"), "--subscription", sub)
        progress("vCPU headroom: " + json.dumps([{ "family": x["name"]["value"], "available": x["limit"] - x["currentValue"]} for x in usage]))
        # Fail closed on absent cost data; never report zero for auth errors.
        cost = self.az("rest", "--method", "post", "--url", f"https://management.azure.com/subscriptions/{sub}/providers/Microsoft.CostManagement/forecast?api-version=2025-03-01",
                       "--body", "@/dev/stdin", data=pack(self.load("cost-query.json", {})))
        rows = cost.get("properties", {}).get("rows", [])
        require(rows and variables.get("monthly_credit") is not None, "Verified cost forecast and monthly_credit required for cost headroom")
        columns = cost["properties"].get("columns", [])
        amounts = [i for i, c in enumerate(columns) if c.get("name", "").lower() in ("cost", "totalcost", "pretaxcost")]
        require(len(amounts) == 1, "Forecast must identify exactly one cost amount column")
        progress(f"Cost headroom: {variables['monthly_credit'] - sum(float(row[amounts[0]]) for row in rows):.2f}")

    def pilot_config(self, variables):
        from datetime import datetime, timezone
        end = tool("lock-backup-versions").timestamp(variables["pilot_end_date"])
        require(end <= datetime.now(timezone.utc), "Pilot has not ended")
        config = self.load("pilot-closure.json", {})
        require(config.get("paused") is True and config.get("lock_id"), "Pilot closure requires paused uploads/workflows and the instance lease")
        return config

    def pilot_lock(self, variables, vault):
        config = self.pilot_config(variables)
        args = ("--account-name", variables["state_account_name"], "--container-name", "tfstate", "--auth-mode", "login", "--subscription", vault)
        lock = self.az("storage", "blob", "show", *args, "--name", "migration.lock")
        require(lock.get("properties", {}).get("lease", {}).get("status") == "locked", "Remote instance lock is not held; keep uploads paused")
        self.az("storage", "blob", "lease", "renew", *args, "--blob-name", "migration.lock", "--lease-id", config["lock_id"], change=True)

    def close_pilot(self, variables, vault):
        # Terraform locks account/container defaults. Existing version policies
        # must be locked separately without extending their original expiries.
        scope = f"/subscriptions/{vault}/resourceGroups/rg-{variables['prefix']}-vault"
        self.storage_grant(scope, "Storage Blob Data Owner", vault)
        if not self.a.apply:
            progress("DRY-RUN: enumerate all retained versions, lock unexpired Unlocked policies preserving expiry, verify complete inventory; keep uploads paused until cleanup")
            return
        account = variables["prefix"] + "backups"
        def request(method, path, headers):
            self.pilot_lock(variables, vault)
            return self.retry(lambda: self.http(method, f"https://{account}.blob.core.windows.net" + path, self.blob_token(vault),
                              headers={"x-ms-version": "2023-11-03", **headers}, change=method != "GET", response_headers=True))
        try:
            count = tool("lock-backup-versions").lock_versions(request, variables["pilot_end_date"], apply=True)
        except (ValueError, ET.ParseError) as exc:
            raise Refusal("Pilot version locking incomplete; keep uploads paused and rerun after investigation") from exc
        # Publish completion only after every temporary grant is removed.
        self.cleanup()
        self.save("config/receipts/pilot-closure.json", {"account": account, "pilot_end_date": variables["pilot_end_date"],
                  "versions_locked": count, "inventory_verified": True, "temporary_roles_removed": True,
                  "lock_id": self.pilot_config(variables)["lock_id"]})

    def ops_callers(self, target, refs, removing):
        # Stage a reviewable local change. Never overwrite other targets' current
        # approved refs with stale local copies, and never push callers to main.
        try:
            import yaml
        except ImportError as exc:
            raise Refusal("PyYAML is required to preserve existing static caller entries") from exc
        tree = self.gh(f"repos/{self.repo()}/git/trees/main?recursive=1")
        require(not tree.get("truncated"), "Caller inventory is truncated")
        paths = {entry["path"] for entry in tree["tree"]}
        for workflow in ("ph-ops", "ph-power", "ph-cost-guard", "ph-backup-verify"):
            path = f".github/workflows/{workflow}.yml"
            if path in paths:
                text = base64.b64decode(self.gh(f"repos/{self.repo()}/contents/{path}?ref=main")["content"]).decode()
            else:
                text = (f"name: {workflow}\non:\n  workflow_dispatch:\n    inputs:\n      target:\n        required: true\n        type: string\npermissions: {{}}\njobs:\n")
            try:
                parsed = yaml.compose(text, Loader=yaml.BaseLoader)
            except yaml.YAMLError as exc:
                raise Refusal("Existing caller YAML is invalid") from exc
            jobs = next((v for k, v in parsed.value if k.value == "jobs"), None)
            require(jobs is not None, "Existing caller has no jobs mapping")
            entries = jobs.value if isinstance(jobs, yaml.MappingNode) else []
            key = "target-" + target
            current = next(((k, v) for k, v in entries if k.value == key), None)
            if current and not removing:
                existing_ref = next((v.value for k, v in current[1].value if k.value == "uses"), None)
                require(existing_ref == refs[workflow], "Existing target caller differs from Owner-authorized ref; use reviewed deployment handoff")
                continue
            if removing:
                if not current:
                    continue
                require(len(entries) > 1, "Review deletion of the final caller file manually")
                lines = text.splitlines(keepends=True)
                del lines[current[0].start_mark.line:current[1].end_mark.line]
                text = "".join(lines)
            else:
                ref = refs[workflow]
                source, sha = ref.split("/.github/", 1)[0], ref.rsplit("@", 1)[1]
                tags = self.gh(f"repos/{source}/tags?per_page=100")
                releases = [t["name"] for t in tags if t["commit"]["sha"] == sha and re.fullmatch(r"v\d+\.\d+\.\d+", t["name"])]
                require(releases, "Static ops caller must reference a tagged Owner-authorized release")
                stanza = (f"  {key}:\n    if: inputs.target == '{target}'\n    permissions:\n      contents: read\n      id-token: write\n    uses: {ref} # {releases[0]}\n    with:\n      target: {target}\n")
                # Insert before the mapping ends, preserving every other byte,
                # including comments and each other target's literal workflow SHA.
                line = jobs.end_mark.line if isinstance(jobs, yaml.MappingNode) else jobs.start_mark.line + 1
                lines = text.splitlines(keepends=True)
                lines.insert(line, stanza)
                text = "".join(lines)
            try:
                result = yaml.safe_load(text)
            except yaml.YAMLError as exc:
                raise Refusal("Staged caller YAML is invalid") from exc
            require(isinstance(result.get("jobs"), dict) and
                    (key not in result["jobs"] if removing else result["jobs"].get(key, {}).get("uses") == refs[workflow]),
                    "Staged caller job readback mismatch")
            self.save(path, text, plain=True)
        progress("Static caller changes staged in the private instance directory; submit them for CODEOWNER review.")

    def vault_info(self, side=None):
        value = self.config if side is None else self.load("migration.json", {}).get(side, {})
        for key in ("subscription", "key_vault_id", "key_vault_name"):
            require(value.get(key), f"Missing {side or 'bootstrap'} {key}")
        self.validate_vault(value)
        self.operator(value["subscription"])
        return value

    @staticmethod
    def validate_vault(value):
        require(re.fullmatch(r"/subscriptions/[^/]+/resourceGroups/[^/]+/providers/Microsoft.KeyVault/vaults/[^/]+", value["key_vault_id"]) and
                value["key_vault_id"].startswith(f"/subscriptions/{value['subscription']}/") and
                value["key_vault_id"].endswith("/vaults/" + value["key_vault_name"]), "Key Vault role scope must be the exact instance vault")

    def age_recipient(self, key):
        value = self.run("age-keygen", "-y", data=key, raw=True).decode().strip()
        require(re.fullmatch(r"age1[0-9a-z]+", value), "Invalid derived age recipient")
        return value

    def key_write(self, vault, key, fp, expected):
        result = self.az("keyvault", "secret", "set", "--vault-name", vault["key_vault_name"], "--name", "age-" + fp,
                         "--subscription", vault["subscription"], "--file", "/dev/stdin", "--encoding", "utf-8",
                         "--query", "{id:id, enabled:attributes.enabled}", data=key, change=True)
        if not self.a.apply:
            return None
        require(result.get("enabled") is True and result.get("id", "").startswith(f"https://{vault['key_vault_name']}.vault.azure.net/secrets/age-{fp}/"), "Missing enabled exact secret version URI")
        readback = self.az("keyvault", "secret", "show", "--id", result["id"], "--subscription", vault["subscription"])
        require(readback.get("attributes", {}).get("enabled") is True and
                self.age_recipient(readback["value"].encode()) == expected, "Key Vault secret readback/recipient mismatch")
        return result["id"]

    def age_key(self):
        custody = self.a.custody or self.load("config/instance.tfvars.json", {}).get("custody", "operator")
        require(self.a.escrow_cmd, "--escrow-cmd required: private keys must never be printed")
        variables = self.load("config/instance.tfvars.json", {})
        pending = self.load("config/age-pending.json", {})
        keys = self.load("config/age-keys.json", {})
        if (pending and pending.get("status") == "escrow" and pending.get("operation") and
                keys.get(pending["fingerprint"], {}).get("operation") == pending["operation"]):
            variables.update(age_recipient=pending["recipient"], age_fingerprint=pending["fingerprint"], custody=pending["custody"])
            pending["status"] = "publish"
            if self.a.apply:
                self.save("config/instance.tfvars.json", variables)
                self.save("config/age-pending.json", pending)
        resuming = pending and pending.get("status") == "escrow"
        if pending and pending.get("status") != "publish":
            require(self.a.key_cmd, "Pending age escrow: retrieve the recorded fingerprint using --key-cmd to resume the same key; no new key was generated")
            custody = pending["custody"]
        if not resuming and (pending or (variables.get("age_recipient") and not self.a.rotate)):
            recipient = pending.get("recipient", variables.get("age_recipient"))
            fingerprint = digest(recipient.encode())
            require(keys.get(fingerprint, {}).get("recipient") == recipient and keys[fingerprint].get("enabled") is True,
                    "Existing age recipient has no enabled fingerprint inventory")
            self.publish_age(recipient, fingerprint, pending.get("rotate", False))
            return
        vault = self.vault_info() if custody == "vault" else None
        if vault:
            self.grant(vault["key_vault_id"], "Key Vault Secrets Officer", vault["subscription"])
            if self.a.apply:
                # Verify data-plane propagation without exposing any key values.
                self.retry(lambda: self.az("keyvault", "secret", "list", "--vault-name", vault["key_vault_name"], "--subscription", vault["subscription"]))
        if not self.a.apply:
            progress("DRY-RUN: age-keygen in memory -> escrow stdin -> verify escrow-derived recipient; publish public recipient/fingerprint")
            if vault:
                self.az("keyvault", "secret", "set", "--vault-name", vault["key_vault_name"], "--file", "/dev/stdin", change=True)
            return
        key = (self.run(*shlex.split(self.a.key_cmd), raw=True, env=child_environment(adapter=True)) if resuming else
               self.run("age-keygen", raw=True))
        recipient = self.age_recipient(key)
        fingerprint = digest(recipient.encode())
        require(not resuming or (pending["recipient"] == recipient and pending["fingerprint"] == fingerprint), "Recovered escrow key differs from pending fingerprint")
        rotate = pending.get("rotate", self.a.rotate)
        operation = pending.get("operation", uuid.uuid4().hex)
        self.save("config/age-pending.json", {"status": "escrow", "recipient": recipient, "fingerprint": fingerprint, "custody": custody, "rotate": rotate, "operation": operation})
        # The escrow adapter must store stdin and return ONLY the public recipient
        # after retrieving/deriving it. This provides readback without disclosure.
        returned = self.run(*shlex.split(self.a.escrow_cmd), data=key, raw=True, change=True, env=child_environment(adapter=True))
        require(returned.decode().strip() == recipient, "Escrow readback must return the stored key's public recipient")
        uri = self.key_write(vault, key, fingerprint, recipient) if vault else None
        keys = self.load("config/age-keys.json", {})
        keys[fingerprint] = {"recipient": recipient, "version_uri": uri, "enabled": True, "operation": operation}
        self.save("config/age-keys.json", keys)
        variables.update(age_recipient=recipient, age_fingerprint=fingerprint, custody=custody)
        self.save("config/instance.tfvars.json", variables)
        self.save("config/age-pending.json", {"status": "publish", "recipient": recipient, "rotate": rotate})
        self.publish_age(recipient, fingerprint, rotate)

    def publish_age(self, recipient, fingerprint, rotate):
        for name, value in (("AGE_RECIPIENT", recipient), ("AGE_FINGERPRINT", fingerprint)):
            self.setting(f"repos/{self.repo()}/actions/variables/{name}", {"name": name, "value": value})
        if rotate:
            self.gh(f"repos/{self.repo()}/actions/workflows/ph-rotate-age.yml/dispatches", "POST", {"ref": "main"})
        if self.a.apply:
            (self.home / "config/age-pending.json").unlink(missing_ok=True)
        progress("Public recipient: " + recipient + "; fingerprint: " + fingerprint)

    def http(self, method, url, token, body=None, headers=None, change=False, binary=False, status_only=False, response_headers=False):
        # curl receives BOTH authentication and request data via anonymous pipes.
        # No headers, signed URLs or response diagnostics are echoed to the user.
        import threading
        require(url.startswith("https://") and not any(c in url for c in '\r\n"'), "Invalid HTTPS endpoint")
        require(not any(c in token for c in '\r\n"'), "Invalid bearer token encoding")
        progress(f"{'APPLY' if change else 'READ'} HTTPS {method} " + url.split("?", 1)[0], flush=True)
        if change and not self.a.apply:
            return b"" if binary else {}
        settings = [f'url = "{url}"', f'request = "{method}"', f'header = "Authorization: Bearer {token}"']
        if ".blob.core.windows.net/" in url:
            from email.utils import formatdate
            headers = {"x-ms-date": formatdate(usegmt=True), **(headers or {})}
        for key, value in (headers or {}).items():
            require(not any(c in str(value) for c in '\r\n"'), "Invalid HTTP header")
            settings.append(f'header = "{key}: {value}"')
        descriptors = ()
        writer = None
        if body is not None:
            read_fd, write_fd = os.pipe()
            descriptors = (read_fd,)
            settings.append(f'data-binary = "@/dev/fd/{read_fd}"')
            def send():
                with os.fdopen(write_fd, "wb") as pipe:
                    try:
                        pipe.write(body)
                    except BrokenPipeError:
                        pass
            writer = threading.Thread(target=send, daemon=True)
            writer.start()
        try:
            flags = ["--silent", "--show-error", "--max-time", "120"]
            flags += ["--output", "/dev/null", "--write-out", "%{http_code}"] if status_only else ["--fail"]
            if response_headers:
                flags.append("--include")
            output = self.run("curl", "--disable", *flags, "--config", "/dev/stdin",
                              data=("\n".join(settings) + "\n").encode(), raw=True, pass_fds=descriptors)
        finally:
            for fd in descriptors:
                os.close(fd)
            if writer:
                writer.join(timeout=1)
        if status_only:
            return int(output)
        if response_headers:
            # Skip any interim HTTP responses before the final headers/body.
            require(output.startswith(b"HTTP/"), "Missing WORM service response headers")
            while output.startswith(b"HTTP/"):
                header, output = output.split(b"\r\n\r\n", 1)
                if not output.startswith(b"HTTP/"):
                    break
            require(header.splitlines()[0].split()[1] in (b"200", b"201"), "Unexpected WORM service response")
            return {k.decode().lower(): v.decode().strip() for k, v in
                    (line.split(b":", 1) for line in header.split(b"\r\n")[1:] if b":" in line)}, output
        return output if binary else (json.loads(output) if output.strip() else {})

    def ts(self, path, method="GET", body=None):
        return self.http(method, "https://api.tailscale.com/api/v2/" + path, self.token,
                         pack(body) if body is not None else None, {"Content-Type": "application/json"}, change=method != "GET")

    def revoke_token(self):
        if self.token and self.a.apply:
            # Access token IDs are non-secret; never put the full token in a URL.
            found = re.fullmatch(r"tskey-api-([A-Za-z0-9]+)-[A-Za-z0-9]+", self.token)
            require(found, "Cannot resolve API token ID for revocation")
            key_id = found[1]
            self.ts(f"tailnet/-/keys/{key_id}", "DELETE")
            status = self.http("GET", "https://api.tailscale.com/api/v2/tailnet/-/keys", self.token, status_only=True)
            require(status == 401, "API token revocation must read back as HTTP 401")
            self.token = None

    def tailscale(self):
        if self.a.print_subject:
            self.print_subject()
            return
        receipt, _ = self.federation()
        require(self.a.token_stdin, "tailscale requires --token-stdin")
        if not self.a.apply:
            progress("DRY-RUN: read short-lived API token from stdin; GET settings/DNS; create/read exact federations; revoke/read token")
            return
        self.token = sys.stdin.read().strip()
        require(re.fullmatch(r"tskey-api-[A-Za-z0-9]+-[A-Za-z0-9]+", self.token), "Expected short-lived Tailscale API access token")
        require(self.ts("tailnet/-/settings").get("httpsEnabled") is True, "HTTPS certificates must be enabled")
        require(self.ts("tailnet/-/dns/preferences").get("magicDNS") is True, "MagicDNS must be enabled")
        existing = [self.ts(f"tailnet/-/keys/{entry['id']}") for entry in self.ts("tailnet/-/keys").get("keys", [])]
        authorized = {"sf-" + ENV[i] + "-" + key for i in ("plan", "deploy") for key in receipt["claims"].get(i, {})}
        for key in existing:
            description = key.get("description", "")
            if description.startswith(("sf-plan-", "sf-production-")) and description not in authorized:
                self.ts(f"tailnet/-/keys/{key['id']}", "DELETE")
                require(not any(k["id"] == key["id"] for k in self.ts("tailnet/-/keys").get("keys", [])), "Withdrawn federation deletion readback mismatch")
                variable = "TAILSCALE_" + re.sub(r"\W", "_", description).upper()
                variables_path = f"repos/{self.repo()}/actions/variables"
                inventory = self.gh(variables_path + "?per_page=100")
                require(inventory.get("total_count", 0) < 100, "Variable inventory needs pagination")
                if any(v["name"] == variable for v in inventory.get("variables", [])):
                    self.gh(f"{variables_path}/{variable}", "DELETE")
                require(not any(v["name"] == variable for v in self.gh(variables_path + "?per_page=100").get("variables", [])), "Withdrawn federation variable deletion readback mismatch")
        result = {}
        for identity in ("plan", "deploy"):
            for key, claim in receipt["claims"].get(identity, {}).items():
                description = "sf-" + ENV[identity] + "-" + key
                body = {"keyType": "federated", "description": description, "issuer": ISSUER, "subject": claim["sub"],
                        "scopes": self.ts_scopes(identity), "tags": [] if identity == "plan" else ["tag:pc-deployer"],
                        "customClaimRules": {"job_workflow_ref": claim["job_workflow_ref"]}}
                matches = [k for k in existing if k.get("description") == description]
                require(len(matches) <= 1, "Ambiguous federated identity")
                if matches and not subset(body, matches[0]):
                    self.ts(f"tailnet/-/keys/{matches[0]['id']}", "DELETE")
                    require(not any(k["id"] == matches[0]["id"] for k in self.ts("tailnet/-/keys").get("keys", [])), "Obsolete federation deletion readback mismatch")
                    matches = []
                if not matches:
                    # No policy write or tag ownership changes, even on API rejection.
                    created = self.ts("tailnet/-/keys", "POST", body)
                    key_id = created["id"]
                else:
                    key_id = matches[0]["id"]
                readback = self.ts(f"tailnet/-/keys/{key_id}")
                require(subset(body, readback) and readback.get("audience"), "Tailscale federation readback mismatch; use --print-subject fallback")
                result[description] = {"client_id": key_id, "audience": readback["audience"], **claim}
        remaining = [self.ts(f"tailnet/-/keys/{entry['id']}") for entry in self.ts("tailnet/-/keys").get("keys", [])]
        managed = [k for k in remaining if k.get("description", "").startswith(("sf-plan-", "sf-production-"))]
        require({k["id"] for k in managed} == {v["client_id"] for v in result.values()}, "Managed Tailscale federation inventory readback mismatch")
        self.save("config/tailnet/federation.json", result)
        for name, value in result.items():
            variable = "TAILSCALE_" + re.sub(r"\W", "_", name).upper()
            self.setting(f"repos/{self.repo()}/actions/variables/{variable}", {"name": variable, "value": json.dumps(value)})

    def rulesets(self):
        repo = self.repo()
        source = self.cfg("source_repo")
        sha = self.cfg("release_sha")
        require(re.fullmatch(r"[a-f0-9]{40}", sha) and sha != "0" * 40, "Rulesets require a full non-placeholder release SHA")
        tags = self.gh(f"repos/{source}/tags?per_page=100")
        require(any(t["commit"]["sha"] == sha and re.fullmatch(r"v\d+\.\d+\.\d+", t["name"]) for t in tags), "Gate SHA must be a tagged release")
        self.gh(f"repos/{source}/contents/.github/workflows/sf-merge-gate.yml?ref={sha}")
        source_id = self.gh(f"repos/{source}")["id"]
        targets = (("public-repo", f"repos/{source}/rulesets"),
                   ("org-merge-gate", f"orgs/{self.cfg('org')}/rulesets"),
                   ("instance-repo", f"repos/{repo}/rulesets"))
        unmanaged = sorted(p.name for p in (HERE.parent / "rulesets").glob("*.json")
                           if not p.name.endswith(".pin.json") and p.stem not in {t for t, _ in targets})
        if unmanaged:
            progress("NOT APPLIED by rulesets (operator applies by hand, see rulesets/README.md): " + ", ".join(unmanaged))
        for template, endpoint in targets:
            body = json.loads((HERE.parent / "rulesets" / (template + ".json")).read_text())
            body["enforcement"] = "active"
            if template == "org-merge-gate":
                body["conditions"]["repository_name"]["include"] = [self.cfg("instance_repo")]
                workflow = body["rules"][0]["parameters"]["workflows"][0]
                workflow.update(repository_id=source_id, sha=sha)
            current = self.gh(endpoint + "?per_page=100")
            require(len(current) < 100, "Ruleset listing needs pagination")
            matches = [r for r in current if r["name"] == body["name"]]
            require(len(matches) <= 1, "Duplicate managed ruleset name")
            existing = self.gh(f"{endpoint}/{matches[0]['id']}") if matches else None
            if template == "org-merge-gate":
                selector = body["conditions"]["repository_name"]
                if existing:
                    previous = existing["conditions"]["repository_name"]
                    selector["include"] = sorted(set(previous["include"]) | {self.cfg("instance_repo")})
                    selector["exclude"] = list(previous.get("exclude", []))
                selector["exclude"] = sorted(set(selector["exclude"]) | {source.split("/")[1]})
            if existing is None or not subset(body, existing):
                result = self.gh(f"{endpoint}/{matches[0]['id']}" if matches else endpoint,
                                 "PUT" if matches else "POST", body)
                rule_id = matches[0]["id"] if matches else result.get("id")
            else:
                rule_id = matches[0]["id"]
            if self.a.apply:
                require(rule_id is not None and subset(body, self.gh(f"{endpoint}/{rule_id}")), "Full ruleset readback mismatch")
        if self.a.apply:
            self.save("config/receipts/rulesets.json", {"gate_sha": sha, "source_repository_id": source_id})

    def blob_token(self, subscription):
        return self.az("account", "get-access-token", "--subscription", subscription,
                       "--resource", "https://storage.azure.com/")["accessToken"]

    def blob(self, account, container, name, token, data=None, metadata=None, version=None):
        from urllib.parse import quote
        require(re.fullmatch(r"[a-z0-9]{3,24}", account), "Invalid storage account")
        url = f"https://{account}.blob.core.windows.net/{container}/" + quote(name, safe="/")
        if version:
            url += "?versionid=" + quote(version, safe="")
        headers = {"x-ms-version": "2025-01-05"}
        if data is not None:
            headers.update({"x-ms-blob-type": "BlockBlob", "If-None-Match": "*"})
            for key, val in (metadata or {}).items():
                headers["x-ms-meta-" + key] = val
        return self.http("GET" if data is None else "PUT", url, token, data, headers,
                         change=data is not None, binary=True)

    def restore_assist(self):
        require(self.cfg("ephemeral_recipient").startswith("age1"), "Ephemeral recipient required")
        require(self.a.key_cmd, "--key-cmd required: key must arrive through an in-memory pipe")
        source = self.cfg("blob")
        destination = self.cfg("staging_blob")
        staging_account = self.cfg("staging_account")
        require(staging_account != self.cfg("backup_account"), "Restore staging must use the separate staging account outside backup WORM")
        require(source.startswith("backups/") and destination.startswith("restore/"), "Expected backup and unique restore staging prefixes")
        fp = self.az("storage", "blob", "show", "--account-name", self.cfg("backup_account"), "--container-name", "backups", "--name", source,
                     "--auth-mode", "login", "--subscription", self.cfg("subscription"),
                     *(["--version-id", self.a.version] if self.a.version else []))["metadata"]["recipient_fp"]
        keys = self.load("config/age-keys.json", {})
        require(fp in keys and keys[fp].get("enabled") is True and digest(keys[fp]["recipient"].encode()) == fp,
                "Unknown/unreadable historical recipient fingerprint")
        if not self.a.apply:
            progress("DRY-RUN: ciphertext download | age decrypt with key pipe | age encrypt to ephemeral | restore-staging upload; verify ciphertext hash")
            return
        key = self.run(*shlex.split(self.a.key_cmd), raw=True, env=child_environment(adapter=True))
        require(self.age_recipient(key) == keys[fp]["recipient"], "Historical key recipient mismatch")
        token = self.blob_token(self.cfg("subscription"))
        encrypted = self.blob(self.cfg("backup_account"), "backups", source, token, version=self.a.version)
        # Decrypted archive goes directly from age to age via a kernel pipe. The
        # parent's Python heap never contains the plaintext archive.
        staged = self.reencrypt(encrypted, key, self.cfg("ephemeral_recipient"))
        self.blob(staging_account, "restore-staging", destination, token, staged,
                  {"source_recipient_fp": fp, "sha256": digest(staged)})
        require(digest(self.blob(staging_account, "restore-staging", destination, token)) == digest(staged), "Staged ciphertext hash mismatch")
        self.save("config/receipts/restore-assist.json", {"blob": source, "version": self.a.version, "staging_blob": destination,
                                                       "staging_account": staging_account, "staging_container": "restore-staging",
                                                       "sha256": digest(staged), "recipient_fp": fp})

    def reencrypt(self, ciphertext, key, recipient):
        import threading
        identity_read, identity_write = os.pipe()
        # age identities are small (< PIPE_BUF), so no temporary identity file.
        os.write(identity_write, key)
        os.close(identity_write)
        decrypt = subprocess.Popen(["age", "-d", "-i", f"/dev/fd/{identity_read}"], pass_fds=(identity_read,),
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=child_environment(adapter=True))
        os.close(identity_read)
        encrypt = subprocess.Popen(["age", "-r", recipient], stdin=decrypt.stdout, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=child_environment(adapter=True))
        decrypt.stdout.close()
        def feed():
            try:
                decrypt.stdin.write(ciphertext)
            except BrokenPipeError:
                pass
            finally:
                decrypt.stdin.close()
        writer = threading.Thread(target=feed, daemon=True)
        writer.start()
        try:
            result, _ = encrypt.communicate(timeout=300)
            writer.join(timeout=1)
            require(decrypt.wait(timeout=10) == 0 and encrypt.returncode == 0, "age re-encryption failed")
            return result
        finally:
            for process in (decrypt, encrypt):
                if process.poll() is None:
                    process.kill()
                    process.wait()

    def vault_move(self):
        migration = self.load("migration.json", {})
        require(migration.get("paused") is True, "Pause state-changing workflows and acquire migration lock first")
        phase = self.cfg("phase")
        receipt = self.load("config/receipts/vault-move.json", {"states": {}, "blobs": {}, "keys": {}})
        src, dst = migration.get("source", {}), migration.get("destination", {})
        require(src and dst and src != dst, "Distinct migration endpoints required")
        for side in (src, dst):
            require(re.fullmatch(r"[a-z0-9]{1,17}", side.get("prefix", "")), "Migration endpoint requires its instance prefix for custom role names")
            for account, scope, container in (("state_account", "state_scope", "tfstate"), ("backup_account", "backups_scope", "backups")):
                require(re.fullmatch(r"/subscriptions/[^/]+/resourceGroups/[^/]+/providers/Microsoft.Storage/storageAccounts/[^/]+/blobServices/default/containers/[^/]+", side[scope]) and
                        side[scope].startswith(f"/subscriptions/{side['subscription']}/") and
                        side[scope].endswith(f"/storageAccounts/{side[account]}/blobServices/default/containers/{container}"), "Temporary data roles must use the exact storage container")
            self.validate_vault(side)
            self.operator(side["subscription"])
        require(migration.get("lock_id"), "An instance-wide migration lock receipt is required")
        self.storage_grant(src["state_scope"], "Storage Blob Data Contributor", src["subscription"])
        if self.a.apply:
            self.retry(lambda: self.az("storage", "blob", "list", "--account-name", src["state_account"],
                                      "--container-name", "tfstate", "--auth-mode", "login", "--subscription", src["subscription"]))
        # Confirm the remote lease is still held, not merely a caller boolean.
        lock = self.az("storage", "blob", "show", "--account-name", src["state_account"], "--container-name", "tfstate",
                       "--name", "migration.lock", "--auth-mode", "login", "--subscription", src["subscription"])
        require(lock.get("properties", {}).get("lease", {}).get("status") == "locked", "Remote migration lock is not held")
        self.az("storage", "blob", "lease", "renew", "--account-name", src["state_account"], "--container-name", "tfstate",
                "--blob-name", "migration.lock", "--lease-id", migration["lock_id"], "--auth-mode", "login", "--subscription", src["subscription"], change=True)
        if phase == "state":
            self.storage_grant(dst["state_scope"], "Storage Blob Data Contributor", dst["subscription"])
            keys = set(src["backends"])
            require(keys == set(dst["backends"]) and {"trust", "tailnet"} <= keys and
                    any(k == "host" or k.startswith("host-") for k in keys) and
                    all(k in ("trust", "host", "tailnet") or re.fullmatch(r"host-[a-z][a-z0-9-]*", k) for k in keys),
                    "Migration must enumerate matching trust, every host and tailnet backend")
            targets = self.load("config/instance.tfvars.json", {}).get("targets", {})
            require(len(targets) <= 1 or {"host-" + t for t in targets} <= keys, "Enumerate a host backend for every registered target")
            for state_key in ["trust", *sorted(k for k in keys if k.startswith("host")), "tailnet"]:
                stack = "host" if state_key.startswith("host") else state_key
                self.tf_session = "migration-" + state_key
                old, new = src["backends"][state_key], dst["backends"][state_key]
                if not self.a.apply:
                    progress(f"DRY-RUN: {state_key}: inspect destination/source lineage, serial and inventory; migrate only matching source into empty destination and verify")
                    continue
                prior = receipt["states"].get(state_key)
                if prior:
                    require(prior["backend"] == new, "Migration destination changed from receipt")
                    summary = prior
                else:
                    self.backend(stack, old)
                    before_state = json.loads(self.tf(stack, "state", "pull"))
                    summary = {"lineage": before_state["lineage"], "serial": before_state["serial"],
                               "inventory_hash": digest(pack(before_state["resources"])), "backend": new, "status": "pending"}
                # Reconfigure without migration to inspect BEFORE force-copy.
                # An exact copy is safe to adopt; any other nonempty state is refused.
                self.backend(stack, new)
                right = json.loads(self.tf(stack, "state", "pull") or b"{}")
                if not self.state_matches(summary, right):
                    require(self.state_empty(right), "Destination state is nonempty and differs from source; refusing migration")
                    require(not prior or prior.get("status") == "pending", "Previously verified destination state is missing")
                    self.backend(stack, old)
                    left = json.loads(self.tf(stack, "state", "pull"))
                    require(self.state_matches(summary, left), "Source state changed since pending migration; workflows must stay paused")
                    receipt["states"][state_key] = summary
                    self.save("config/receipts/vault-move.json", receipt)
                    self.backend(stack, new, migrate=True)
                if self.a.apply:
                    right = json.loads(self.tf(stack, "state", "pull"))
                    require(self.state_matches(summary, right),
                            "Migrated state lineage/serial/inventory mismatch; workflows must stay paused")
                    receipt["states"][state_key] = dict(summary, status="verified")
                    self.save("config/receipts/vault-move.json", receipt)
        elif phase == "backups":
            self.storage_grant(src["backups_scope"], "Storage Blob Data Reader", src["subscription"])
            self.storage_grant(dst["backups_scope"], dst["prefix"] + " SF Backup Migration Writer", dst["subscription"])
            for side in (src, dst):
                if self.a.apply:
                    self.retry(lambda side=side: self.az("storage", "blob", "list", "--account-name", side["backup_account"], "--container-name", "backups", "--auth-mode", "login", "--subscription", side["subscription"]))
            blobs = self.az("storage", "blob", "list", "--account-name", src["backup_account"], "--container-name", "backups", "--include", "vmi", "--num-results", "*", "--auth-mode", "login", "--subscription", src["subscription"])
            if not self.a.apply:
                progress("DRY-RUN: copy every retained blob/version; compare both ciphertext hashes and destination WORM; preserve metadata and source")
                return
            source_token, destination_token = self.blob_token(src["subscription"]), self.blob_token(dst["subscription"])
            destination_inventory = self.az("storage", "blob", "list", "--account-name", dst["backup_account"], "--container-name", "backups", "--num-results", "*", "--auth-mode", "login", "--subscription", dst["subscription"])
            existing_names = {blob["name"] for blob in destination_inventory}
            for blob in blobs:
                require(blob.get("metadata", {}).get("recipient_fp"), "Retained backup version is missing recipient_fp metadata")
                name, version = blob["name"], blob.get("versionId")
                source_key = pack([name, version]).decode()
                destination = "backups/migrated/" + digest(source_key.encode()) + ".age"
                # Delta copies re-verify existing receipts, never overwrite WORM.
                content = self.blob(src["backup_account"], "backups", name, source_token, version=version)
                copied_before = source_key in receipt["blobs"] or destination in existing_names
                if not copied_before:
                    self.blob(dst["backup_account"], "backups", destination, destination_token, content, blob.get("metadata", {}))
                copied = self.blob(dst["backup_account"], "backups", destination, destination_token)
                require(digest(content) == digest(copied), "Copied ciphertext hash mismatch")
                result = self.az("storage", "blob", "show", "--account-name", dst["backup_account"], "--container-name", "backups", "--name", destination, "--auth-mode", "login", "--subscription", dst["subscription"])
                require(result.get("metadata", {}) == blob.get("metadata", {}), "Copied metadata mismatch")
                policy = result.get("properties", {}).get("immutabilityPolicy", {})
                require(policy.get("policyMode") in ("Locked", "Unlocked") and policy.get("expiryTime"), "Destination retention missing")
                from datetime import datetime, timezone, timedelta
                expiry = datetime.fromisoformat(policy["expiryTime"].replace("Z", "+00:00"))
                minimum = datetime.now(timezone.utc) + (timedelta() if copied_before else timedelta(days=29))
                original_policy = blob.get("properties", {}).get("immutabilityPolicy", {})
                if original_policy.get("expiryTime"):
                    minimum = max(minimum, datetime.fromisoformat(original_policy["expiryTime"].replace("Z", "+00:00")))
                require(expiry >= minimum, "Destination retention is shorter than required WORM")
                end = self.load("config/instance.tfvars.json", {}).get("pilot_end_date")
                if end and datetime.fromisoformat(end.replace("Z", "+00:00")) <= datetime.now(timezone.utc):
                    require(policy["policyMode"] == "Locked", "Destination WORM must be Locked after pilot end")
                receipt["blobs"][source_key] = {"name": destination, "sha256": digest(copied), "version": result.get("versionId"), "retention": policy}
                self.save("config/receipts/vault-move.json", receipt)
        elif phase == "keys":
            require(self.load("config/instance.tfvars.json", {}).get("custody") == "vault", "Key migration requires vault custody")
            keys = self.load("config/age-keys.json", {})
            require(keys, "Missing historical fingerprint/version inventory")
            for fp in keys:
                self.grant(src["key_vault_id"] + "/secrets/age-" + fp, "Key Vault Secrets User", src["subscription"])
            self.grant(dst["key_vault_id"], "Key Vault Secrets Officer", dst["subscription"])
            if self.a.apply:
                inventory = self.retry(lambda: self.az("keyvault", "secret", "list", "--vault-name", dst["key_vault_name"], "--subscription", dst["subscription"]))
                require(all(s["name"] in {"age-" + fp for fp in keys} for s in inventory), "Destination vault must contain only this instance's age secrets")
            for fp, entry in keys.items():
                require(entry.get("version_uri") and entry.get("enabled") is True and digest(entry["recipient"].encode()) == fp,
                        "Unknown/unreadable historical key version")
                require(entry["version_uri"].startswith(f"https://{src['key_vault_name']}.vault.azure.net/secrets/age-{fp}/"),
                        "Historical key version is outside the scoped source secret")
                if not self.a.apply:
                    progress("DRY-RUN: exact source secret version -> memory -> enabled destination version stdin; verify derived recipient")
                    continue
                secret = self.retry(lambda: self.az("keyvault", "secret", "show", "--id", entry["version_uri"], "--subscription", src["subscription"]))
                require(secret.get("attributes", {}).get("enabled") is True and self.age_recipient(secret["value"].encode()) == entry["recipient"], "Historical key fingerprint/recipient mismatch")
                if fp in receipt["keys"]:
                    check = self.az("keyvault", "secret", "show", "--id", receipt["keys"][fp]["destination_version_uri"], "--subscription", dst["subscription"])
                    require(check.get("attributes", {}).get("enabled") is True and self.age_recipient(check["value"].encode()) == entry["recipient"], "Resumed migrated key mismatch")
                    continue
                uri = self.key_write(dst, secret["value"].encode(), fp, entry["recipient"])
                receipt["keys"][fp] = {"source_version_uri": entry["version_uri"], "destination_version_uri": uri, "recipient": entry["recipient"]}
                self.save("config/receipts/vault-move.json", receipt)
        if self.a.apply:
            receipt.update(destination=dst, lock_id=migration["lock_id"], last_phase=phase, source_authoritative=True)
            self.save("config/receipts/vault-move.json", receipt)

    def revoke(self):
        # Select ONLY actual leaf authorization addresses from state. Never target
        # a module: that could delete storage/identities and their dependencies.
        backend = self.config.get("backends", {}).get("trust")
        require(backend, "Revocation requires the existing private trust backend")
        scope = (f"/subscriptions/{backend['subscription_id']}/resourceGroups/{backend['resource_group_name']}"
                 f"/providers/Microsoft.Storage/storageAccounts/{backend['storage_account_name']}/blobServices/default/containers/tfstate")
        self.storage_grant(scope, "Storage Blob Data Contributor", backend["subscription_id"])
        if not self.a.apply:
            progress("DRY-RUN: initialize trust backend, list authorization leaves, destroy only those leaves and verify removal")
            return
        self.retry(lambda: self.az("storage", "blob", "list", "--account-name", backend["storage_account_name"], "--container-name", "tfstate", "--auth-mode", "login", "--subscription", backend["subscription_id"]))
        self.backend("trust", backend)
        addresses = self.tf("trust", "state", "list").decode().splitlines()
        selected = [s for s in addresses if re.search(r"\.(?:azurerm_federated_identity_credential|azurerm_role_assignment)\.", s)]
        require(selected, "No revocable leaf authorization resources in initialized private trust backend")
        require(all(s.startswith("module.trust.") for s in selected), "Unexpected authorization state addresses")
        state = json.loads(self.tf("trust", "show", "-json"))
        def resources(module):
            yield from module.get("resources", [])
            for child in module.get("child_modules", []):
                yield from resources(child)
        roles = [r for r in resources(state["values"]["root_module"])
                 if r["address"] in selected and r["type"] == "azurerm_role_assignment"]
        require({r["address"] for r in roles} == {s for s in selected if ".azurerm_role_assignment." in s},
                "Missing revocation role scope inventory")
        scopes = [r["values"]["scope"] for r in roles]
        require(all(re.match(r"/subscriptions/[^/]+(?:/|$)", scope) for scope in scopes), "Invalid revocation role scope")
        self.unlock_role_scopes(scopes)
        variables = self.load("config/instance.tfvars.json", {})
        # Trust variables may include non-Terraform operator settings; select the
        # same public contract and private temporary JSON file as trust apply.
        self.tf("trust", "destroy", "-input=false", "-auto-approve",
                *[f"-target={s}" for s in selected], variables=self.trust_vars({**variables, "target_client_id": None}), change=True)
        if self.a.apply:
            remaining = self.tf("trust", "state", "list").decode().splitlines()
            require(not set(selected).intersection(remaining), "Revocation readback failed")

    @staticmethod
    def state_empty(state):
        # Azure's StateMgr creates a serial-zero snapshot on first access.
        # A lineage alone is therefore not evidence of an existing deployment.
        return state == {} or (state.get("serial") == 0 and bool(state.get("lineage")) and
                               state.get("resources") == [] and state.get("outputs", {}) == {} and
                               state.get("check_results") is None)

    @staticmethod
    def state_matches(summary, state):
        return (summary["lineage"] == state.get("lineage") and summary["serial"] == state.get("serial") and
                summary["inventory_hash"] == digest(pack(state.get("resources"))))


def parser():
    p = argparse.ArgumentParser(description="Operator bootstrap; dry-run unless --apply. Private config stays outside this checkout.")
    p.add_argument("mode", nargs="?", choices=["init", "trust", "age-key", "vault-move", "tailscale", "rulesets", "restore-assist", "oidc-probe", "cleanup"])
    action = p.add_mutually_exclusive_group()
    action.add_argument("--apply", action="store_true")
    action.add_argument("--dry-run", action="store_true")
    p.add_argument("--instance-dir", default=os.environ.get("SF_INSTANCE_DIR", "../sf-instance-private"))
    for name in ("org", "operator-login", "instance-repo", "reviewer", "source-repo", "release-sha", "vault-subscription", "subscription", "target", "role-home-target", "resource-group", "location", "escrow-cmd", "probe-run", "key-cmd", "blob", "version", "backup-account", "staging-account", "staging-blob", "ephemeral-recipient"):
        p.add_argument("--" + name)
    p.add_argument("--vault-mode", choices=["separate-subscription", "same-subscription"])
    p.add_argument("--scope", choices=["resource-group", "subscription"], help="resource-group by default; preserve existing target ownership when omitted")
    p.add_argument("--custody", choices=["operator", "vault"])
    p.add_argument("--phase", choices=["state", "backups", "keys"])
    for name in ("accept-vault-risk", "remove-target", "rotate", "token-stdin", "print-subject", "revoke", "close-pilot"):
        p.add_argument("--" + name, action="store_true")
    p.add_argument("--attempts", type=int, default=6)
    p.add_argument("--retry-delay", type=float, default=5)
    return p


def main():
    args = parser().parse_args()
    require(args.mode or args.print_subject or args.revoke, "Select a mode")
    require(args.attempts >= 1 and 0 <= args.retry_delay <= 30, "Invalid retry bounds")
    app = Bootstrap(args)
    # A process lock prevents local concurrent use of the same assignment ledger.
    # Remote instance migration leases remain mandatory for vault-move.
    with contextlib.ExitStack() as stack:
        if args.apply:
            app.home.mkdir(parents=True, exist_ok=True, mode=0o700)
            lock = stack.enter_context(open(app.home / ".bootstrap.lock", "a", encoding="utf-8"))
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise Refusal("Another bootstrap process holds the local lock") from exc
        require(not app.lock_ledger or args.mode == "cleanup", "Unrestored storage locks: run cleanup --apply before resuming")
        require(not app.ledger or args.mode == "cleanup", "Unfinished assignment ledger: run cleanup --apply before resuming")
        require(args.mode == "cleanup" or not list((app.home / "recovery").glob("*/errored.tfstate")), "Recover errored.tfstate with terraform state push against its intended backend, then securely delete it before continuing")
        interruption = {"raised": False}
        def interrupted(signum, _frame):
            if interruption["raised"]:
                return
            interruption["raised"] = True
            raise Refusal(f"Interrupted by signal {signum}; running cleanup")
        for sig in INTERRUPTS:
            signal.signal(sig, interrupted)
        try:
            if args.revoke:
                app.revoke()
            elif args.print_subject and args.mode != "tailscale":
                app.print_subject()
            elif args.mode != "cleanup":
                getattr(app, args.mode.replace("-", "_"))()
        finally:
            interruption["raised"] = True
            signal.pthread_sigmask(signal.SIG_BLOCK, INTERRUPTS)
            # A second interruption must not skip ledger persistence/cleanup.
            for sig in INTERRUPTS:
                signal.signal(sig, signal.SIG_IGN)
            try:
                try:
                    app.revoke_token()
                except (Refusal, ValueError, KeyError, OSError, subprocess.SubprocessError) as exc:
                    raise Refusal("TOKEN CLEANUP FAILED: revoke the short-lived API token in the Tailscale console") from exc
            finally:
                try:
                    app.cleanup()
                finally:
                    app.remove_tf_tree()
        if args.apply and not args.print_subject:
            receipt = {"mode": "revoke" if args.revoke else args.mode, "success": True, "temporary_roles_removed": True}
            app.save("config/receipts/last-bootstrap-operation.json", receipt)
            progress(json.dumps(receipt, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (Refusal, KeyError, ValueError, OSError, zipfile.BadZipFile, subprocess.SubprocessError) as error:
        # Never interpolate SDK responses or arbitrary exception text.
        progress("REFUSED: " + (str(error) if isinstance(error, Refusal) else "Invalid configuration or tool response; no secret diagnostics emitted"), file=sys.stderr)
        sys.exit(1)
