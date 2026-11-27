"""Run the documented apply commands with a local gh stub; never contact GitHub."""

import copy
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
RULESETS = ROOT / "hosting/paperclip-hermes/rulesets"
STUB = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
state = pathlib.Path(os.environ["APPLY_STATE"])
with (state / "calls").open("a") as log:
    log.write(json.dumps(args) + "\n")
path = args[1] if args[0] == "api" else ""
if "--method" in args:
    phase = args[args.index("--method") + 1]
elif path == "user": phase = "user"
elif "/commits?" in path: phase = "sha"
elif "/contents/" in path: phase = "blob"
elif "/teams/" in path: phase = "team"
elif "rulesets?" in path: phase = "list"
elif "/rulesets/" in path: phase = "readback"
else: phase = "repo"
if os.environ.get("FAIL_PHASE") == phase:
    print('{"message":"API error","status":"403"}')
    sys.exit(1)
if os.environ.get("BAD_PHASE") == phase:
    if phase in ("POST", "PUT"):
        body = pathlib.Path(args[args.index("--input") + 1]).read_text()
        (state / "body").write_text(body)
    print(os.environ.get("BAD_VALUE", '{"message":"bad value"}'))
    sys.exit(0)
if phase in ("POST", "PUT"):
    body = pathlib.Path(args[args.index("--input") + 1]).read_text()
    (state / "body").write_text(body)
    print("123")
elif phase == "readback":
    body = json.loads((state / "body").read_text())
    if body["name"] == "software-factory-main":
        body.update(id=123, source_type="Repository")
        for actor in body["bypass_actors"]:
            if actor["actor_type"] == "OrganizationAdmin": actor["actor_id"] = None
        for rule in body["rules"]:
            if rule["type"] == "pull_request":
                rule["parameters"].update(dismissal_restriction={"enabled":False, "allowed_actors":[]},
                                          require_extra_approval_for_unattributed_changes=True, required_reviewers=[])
            if rule["type"] == "required_status_checks":
                rule["parameters"]["do_not_enforce_on_create"] = False
        change = os.environ.get("READBACK_CHANGE")
        if change == "extra_rule": body["rules"].append({"type":"required_signatures"})
        if change == "removed_rule": body["rules"].pop(0)
        for rule in body["rules"]:
            if rule["type"] == "pull_request":
                if change == "last_push": rule["parameters"]["require_last_push_approval"] = False
                if change == "merge": rule["parameters"]["allowed_merge_methods"].append("merge")
                if change == "removed_parameter": del rule["parameters"]["require_code_owner_review"]
            if rule["type"] == "required_status_checks" and change == "integration":
                rule["parameters"]["required_status_checks"][0]["integration_id"] = 42
    change = os.environ.get("READBACK_CHANGE")
    if change == "enforcement": body["enforcement"] = "disabled"
    if change == "conditions": body["conditions"]["ref_name"]["include"].append("refs/heads/other")
    if change == "repo_condition": body["conditions"]["repository_name"]["include"].append("other-repo")
    if change == "bypass":
        body["bypass_actors"].append({"actor_id":5, "actor_type":"RepositoryRole", "bypass_mode":"always"})
    if change == "bypass_mode":
        for actor in body["bypass_actors"]:
            if actor["actor_type"] == "OrganizationAdmin" and actor["bypass_mode"] == "pull_request":
                actor["bypass_mode"] = "always"
    if os.environ.get("NULL_ADMIN"):
        for actor in body["bypass_actors"]:
            if actor["actor_type"] == "OrganizationAdmin": actor["actor_id"] = None
    if os.environ.get("NULL_TEAM"):
        for actor in body["bypass_actors"]:
            if actor["actor_type"] == "Team": actor["actor_id"] = None
    print(json.dumps(body))
elif phase == "user": print("operator-fixture")
elif phase == "sha": print("a" * 40)
elif phase == "blob": print(json.loads((pathlib.Path(os.environ["APPLY_ROOT"]) / "hosting/paperclip-hermes/rulesets/org-release-notes-gate.pin.json").read_text())["blob_sha"])
elif phase in ("team", "repo"): print("456")
elif phase == "list": print("123" if os.environ.get("EXISTING") else "")
'''



class PublicReferencesTests(unittest.TestCase):
    def test_committed_scoped_files_name_only_the_public_repository(self):
        paths = subprocess.check_output(
            ["git", "ls-files", "-z", "--", "hosting/", "renovate/", "ci/renovate/"], cwd=ROOT,
        ).decode().split("\0")
        private_names = [b"-".join(parts) for parts in (
            (b"dev", b"environments"), (b"agentic", b"research", b"stack"),
            (b"workout", b"trackroutinely"),
        )]
        for name in filter(None, paths):
            with self.subTest(path=name):
                data = (ROOT / name).read_bytes()
                for match in re.finditer(rb"agenticcodingops/([A-Za-z0-9_.-]+)", data):
                    # Organization API routes and CODEOWNERS teams are not repositories.
                    prefix = data[max(0, match.start() - 5):match.start()]
                    if prefix.endswith(b"orgs/") and match[1] in (b"rulesets", b"teams"):
                        continue
                    if prefix.endswith(b"@") and match[1] == b"maintainers":
                        continue
                    self.assertEqual(match[1].removesuffix(b".git"), b"software-factory",
                                     "non-public repository reference")
                for private in private_names:
                    self.assertNotIn(private, data, "private repository name")


class ManualApplyTests(unittest.TestCase):
    def blocks(self):
        text = (RULESETS / "README.md").read_text()
        blocks = re.findall(r"```bash\n(.*?)\n```", text, re.S)
        apply_blocks = [b for b in blocks if "gh api" in b]
        self.assertEqual(len(apply_blocks), 3, "all three documented applies must execute")
        return apply_blocks

    def run_block(self, block, cwd=ROOT, missing_jq=False, failing_projection=False, **scenario):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state / "gh").write_text(STUB)
            (state / "gh").chmod(0o755)
            if missing_jq:
                # Do not inherit /usr/bin, where CI installs jq beside Bash and diff.
                for tool in ("bash", "python3", "diff", "mktemp", "rm", "cat"):
                    (state / tool).symlink_to(shutil.which(tool))
            if failing_projection:
                (state / "jq").write_text('#!/bin/sh\nif [ "$1" = -S ]; then exit 9; fi\nexec ' +
                                          shutil.which("jq") + ' "$@"\n')
                (state / "jq").chmod(0o755)
            temp = state / "tmp"
            temp.mkdir()
            env = {**os.environ, "PATH":directory if missing_jq else directory + os.pathsep + os.environ["PATH"],
                   "APPLY_STATE":directory, "APPLY_ROOT":str(cwd), "RELEASE_TEAM_SLUG":"release-fixture",
                   "TMPDIR":str(temp), **scenario}
            result = subprocess.run(["bash", "-c", block], cwd=cwd, env=env, capture_output=True, text=True)
            calls = [json.loads(line) for line in (state / "calls").read_text().splitlines()]
            leftovers = list(temp.iterdir())
            body = json.loads((state / "body").read_text()) if (state / "body").exists() else None
            return result, calls, leftovers, body

    def test_missing_jq_stops_before_any_write(self):
        for number, block in enumerate(self.blocks()):
            with self.subTest(block=number):
                result, calls, leftovers, _ = self.run_block(block, missing_jq=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(any("--method" in call for call in calls))
                self.assertEqual(leftovers, [])

    def test_failed_json_projections_cannot_compare_as_empty_streams(self):
        for number, block in enumerate(self.blocks()):
            with self.subTest(block=number):
                result, _, leftovers, _ = self.run_block(block, failing_projection=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(leftovers, [])

    def test_invalid_readback_json_fails_comparison(self):
        for number, block in enumerate(self.blocks()):
            with self.subTest(block=number):
                result, _, leftovers, _ = self.run_block(block, BAD_PHASE="readback", BAD_VALUE="{")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(leftovers, [])

    def test_failed_subshell_keeps_parent_shell_alive(self):
        for block in self.blocks():
            phase = "blob" if "org-release-notes-gate.json" in block else "team" if "public-release-tags.json" in block else "user"
            result, _, leftovers, _ = self.run_block(block + "\nresult=$?\nprintf 'parent survived:%s\\n' \"$result\"\n", FAIL_PHASE=phase)
            self.assertEqual(result.returncode, 0)
            self.assertIn("parent survived:1", result.stdout)
            self.assertEqual(leftovers, [])

    def test_apply_stops_on_api_errors_and_invalid_ids(self):
        for number, block in enumerate(self.blocks()):
            phases = ["user", "list", "POST", "PUT", "readback"]
            if "org-release-notes-gate.json" in block:
                phases += ["sha", "blob", "repo"]
            if "public-release-tags.json" in block:
                phases += ["team"]
            for phase in phases:
                with self.subTest(block=number, phase=phase):
                    result, calls, leftovers, _ = self.run_block(block, FAIL_PHASE=phase, EXISTING="1" if phase == "PUT" else "")
                    self.assertNotEqual(result.returncode, 0)
                    writes = [c for c in calls if "--method" in c]
                    self.assertEqual(len(writes), int(phase in ("POST", "PUT", "readback")))
                    if phase in ("POST", "PUT"):
                        self.assertFalse(any("/rulesets/" in c[1] for c in calls if c[0:2] != ["api", "--method"]))
                    self.assertEqual(leftovers, [])
            shapes = {"list":["123\n456", "{}", "0"], "POST":["{}", "0", ""]}
            if "org-release-notes-gate.json" in block:
                shapes.update(sha=["{}", "", "A" * 40], repo=["{}", "0", ""], blob=["b" * 40, "{}", ""])
            if "public-release-tags.json" in block:
                shapes["team"] = ["{}", "0", ""]
            for phase, values in shapes.items():
                for value in values:
                    with self.subTest(block=number, phase=phase, value=value):
                        result, calls, leftovers, _ = self.run_block(block, BAD_PHASE=phase, BAD_VALUE=value)
                        self.assertNotEqual(result.returncode, 0)
                        self.assertEqual(sum("--method" in c for c in calls), int(phase == "POST"))
                        if phase == "blob":
                            self.assertIn("workflow at main differs from the reviewed pin", result.stderr)
                        if phase == "POST":
                            self.assertIn("ruleset ID is unresolved", result.stderr)
                            self.assertFalse(any("/rulesets/" in c[1] for c in calls if "--method" not in c))
                        self.assertEqual(leftovers, [])

    def test_gate_pin_file_shape_is_checked_before_any_write(self):
        block = next(b for b in self.blocks() if "org-release-notes-gate.json" in b)
        for field, value in (("blob_sha", "x"), ("path", "")):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                cwd = Path(directory)
                destination = cwd / "hosting/paperclip-hermes/rulesets"
                shutil.copytree(RULESETS, destination)
                pin_file = destination / "org-release-notes-gate.pin.json"
                pin = json.loads(pin_file.read_text())
                pin[field] = value
                pin_file.write_text(json.dumps(pin))
                result, calls, leftovers, _ = self.run_block(block, cwd=cwd)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("workflow pin is unresolved", result.stderr)
                self.assertFalse(any("--method" in c for c in calls))
                self.assertEqual(leftovers, [])

    def test_branch_readback_compares_rules_and_extra_rule(self):
        block = next(b for b in self.blocks() if "public-repo.json" in b)
        for change in ("last_push", "merge", "integration", "removed_parameter", "extra_rule", "removed_rule"):
            with self.subTest(change=change):
                result, _, leftovers, _ = self.run_block(block, READBACK_CHANGE=change)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotEqual(result.stdout, "operator-fixture\n")
                self.assertEqual(leftovers, [])

    def test_readback_compares_enforcement_conditions_and_bypass_actors(self):
        for number, block in enumerate(self.blocks()):
            changes = ["enforcement", "conditions", "bypass"]
            if "public-release-tags.json" not in block:
                changes.append("bypass_mode")
            if "org-release-notes-gate.json" in block:
                changes.append("repo_condition")
            for change in changes:
                with self.subTest(block=number, change=change):
                    result, _, leftovers, _ = self.run_block(block, READBACK_CHANGE=change)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotEqual(result.stdout, "operator-fixture\n")
                    self.assertEqual(leftovers, [])

    def test_each_apply_is_a_standalone_subshell(self):
        for block in self.blocks():
            self.assertTrue(block.startswith("(\nset -euo pipefail\n"))
            self.assertTrue(block.endswith("\n)"))
            self.assertIn("gh api user --jq .login", block)

    def test_only_organization_admin_null_actor_id_is_normalized(self):
        for number, block in enumerate(self.blocks()):
            with self.subTest(block=number):
                result, _, leftovers, _ = self.run_block(block, NULL_ADMIN="1")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(leftovers, [])
            if "public-release-tags.json" in block:
                result, _, leftovers, _ = self.run_block(block, NULL_ADMIN="1", NULL_TEAM="1")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("actor_id", result.stdout)
                self.assertEqual(leftovers, [])

    def test_successful_create_and_update_have_empty_readback_diff(self):
        for number, block in enumerate(self.blocks()):
            for existing in ("", "1"):
                with self.subTest(block=number, existing=existing):
                    result, calls, leftovers, body = self.run_block(block, EXISTING=existing)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(result.stdout, "operator-fixture\n", "readback diff must be empty")
                    self.assertEqual(len([c for c in calls if "--method" in c]), 1)
                    self.assertEqual(leftovers, [])
                    self.assertEqual(body["enforcement"], "active")
                    filename = next(name for name in ("org-release-notes-gate.json", "public-release-tags.json", "public-repo.json") if name in block)
                    template = json.loads((RULESETS / filename).read_text())
                    normalized = copy.deepcopy(body)
                    endpoint = "repos/agenticcodingops/software-factory/rulesets"
                    if filename == "org-release-notes-gate.json":
                        endpoint = "orgs/agenticcodingops/rulesets"
                        workflow = next(r for r in normalized["rules"] if r["type"] == "workflows")["parameters"]["workflows"][0]
                        self.assertEqual(workflow["sha"], "a" * 40)
                        self.assertEqual(workflow["repository_id"], 456)
                        original = next(r for r in template["rules"] if r["type"] == "workflows")["parameters"]["workflows"][0]
                        workflow["sha"], workflow["repository_id"] = original["sha"], original["repository_id"]
                    elif filename == "public-release-tags.json":
                        teams = [a for a in normalized["bypass_actors"] if a["actor_type"] == "Team"]
                        self.assertEqual(len(teams), 1)
                        self.assertEqual(teams[0]["actor_id"], 456)
                        teams[0]["actor_id"] = next(a for a in template["bypass_actors"] if a["actor_type"] == "Team")["actor_id"]
                    normalized["enforcement"] = template["enforcement"]
                    self.assertEqual(normalized, template)
                    write = next(c for c in calls if "--method" in c)
                    method = "PUT" if existing else "POST"
                    self.assertEqual(write[:4], ["api", "--method", method, endpoint + ("/123" if existing else "")])
                    readbacks = [c for c in calls if "--method" not in c and "/rulesets/" in c[1]]
                    self.assertEqual([c[1] for c in readbacks], [endpoint + "/123"])
                    listing = next(c for c in calls if "rulesets?" in c[1])
                    self.assertTrue(listing[1].startswith(endpoint + "?"))
                    self.assertIn('select(.name=="' + template["name"] + '")', listing[listing.index("--jq") + 1])
                    if filename != "org-release-notes-gate.json":
                        self.assertIn("includes_parents=false", listing[1])


if __name__ == "__main__":
    unittest.main()
