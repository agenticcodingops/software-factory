"""Run the documented apply commands with a local gh stub; never contact GitHub."""

import json
import os
from pathlib import Path
import re
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
    print(os.environ.get("BAD_VALUE", '{"message":"bad value"}'))
    sys.exit(0)
if phase in ("POST", "PUT"):
    body = pathlib.Path(args[args.index("--input") + 1]).read_text()
    (state / "body").write_text(body)
    print("123")
elif phase == "readback":
    body = json.loads((state / "body").read_text())
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


class ManualApplyTests(unittest.TestCase):
    def blocks(self):
        text = (RULESETS / "README.md").read_text()
        blocks = re.findall(r"```bash\n(.*?)\n```", text, re.S)
        apply_blocks = [b for b in blocks if "gh api" in b]
        self.assertEqual(len(apply_blocks), 3, "all three documented applies must execute")
        return apply_blocks

    def run_block(self, block, **scenario):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state / "gh").write_text(STUB)
            (state / "gh").chmod(0o755)
            temp = state / "tmp"
            temp.mkdir()
            env = {**os.environ, "PATH":directory + os.pathsep + os.environ["PATH"],
                   "APPLY_STATE":directory, "APPLY_ROOT":str(ROOT), "RELEASE_TEAM_SLUG":"release-fixture",
                   "TMPDIR":str(temp), **scenario}
            result = subprocess.run(["bash", "-c", block], cwd=ROOT, env=env, capture_output=True, text=True)
            calls = [json.loads(line) for line in (state / "calls").read_text().splitlines()]
            leftovers = list(temp.iterdir())
            return result, calls, leftovers

    def test_failed_subshell_keeps_parent_shell_alive(self):
        for block in self.blocks():
            phase = "blob" if "org-release-notes-gate.json" in block else "team" if "public-release-tags.json" in block else "user"
            result, _, leftovers = self.run_block(block + "\nresult=$?\nprintf 'parent survived:%s\\n' \"$result\"\n", FAIL_PHASE=phase)
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
                    result, calls, leftovers = self.run_block(block, FAIL_PHASE=phase, EXISTING="1" if phase == "PUT" else "")
                    self.assertNotEqual(result.returncode, 0)
                    writes = [c for c in calls if "--method" in c]
                    self.assertEqual(len(writes), int(phase in ("POST", "PUT", "readback")))
                    if phase in ("POST", "PUT"):
                        self.assertFalse(any("/rulesets/" in c[1] for c in calls if c[0:2] != ["api", "--method"]))
                    self.assertEqual(leftovers, [])
            shapes = {"list":["123\n456", "{}", "0"], "POST":["{}", "0", ""]}
            if "org-release-notes-gate.json" in block:
                shapes.update(sha=["{}", "", "A" * 40], repo=["{}", "0", ""])
            if "public-release-tags.json" in block:
                shapes["team"] = ["{}", "0", ""]
            for phase, values in shapes.items():
                for value in values:
                    with self.subTest(block=number, phase=phase, value=value):
                        result, calls, leftovers = self.run_block(block, BAD_PHASE=phase, BAD_VALUE=value)
                        self.assertNotEqual(result.returncode, 0)
                        self.assertEqual(sum("--method" in c for c in calls), int(phase == "POST"))
                        self.assertEqual(leftovers, [])

    def test_each_apply_is_a_standalone_subshell(self):
        for block in self.blocks():
            self.assertTrue(block.startswith("(\nset -euo pipefail\n"))
            self.assertTrue(block.endswith("\n)"))
            self.assertIn("gh api user --jq .login", block)

    def test_only_organization_admin_null_actor_id_is_normalized(self):
        for number, block in enumerate(self.blocks()):
            with self.subTest(block=number):
                result, _, leftovers = self.run_block(block, NULL_ADMIN="1")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(leftovers, [])
            if "public-release-tags.json" in block:
                result, _, leftovers = self.run_block(block, NULL_ADMIN="1", NULL_TEAM="1")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("actor_id", result.stdout)
                self.assertEqual(leftovers, [])

    def test_successful_create_and_update_have_empty_readback_diff(self):
        for number, block in enumerate(self.blocks()):
            for existing in ("", "1"):
                with self.subTest(block=number, existing=existing):
                    result, calls, leftovers = self.run_block(block, EXISTING=existing)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(len([c for c in calls if "--method" in c]), 1)
                    self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
