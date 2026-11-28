"""Ruleset contracts: require workflow identity and limit release-tag bypass."""

import json
import re
from pathlib import Path
import hashlib
import unittest

ROOT = Path(__file__).resolve().parents[2]
RULESETS = ROOT / "hosting/paperclip-hermes/rulesets"


class RulesetTests(unittest.TestCase):
    def test_gate_requirement_is_workflow_identity_not_forgeable_context(self):
        branch = json.loads((RULESETS / "public-repo.json").read_text())
        contexts = [c["context"] for r in branch["rules"] if r["type"] == "required_status_checks"
                    for c in r["parameters"]["required_status_checks"]]
        self.assertNotIn("release-notes-gate", contexts)
        gate = json.loads((RULESETS / "org-release-notes-gate.json").read_text())
        self.assertEqual(gate["conditions"]["repository_name"]["include"], ["software-factory"])
        self.assertEqual(gate["conditions"]["ref_name"]["include"], ["refs/heads/main"])
        self.assertEqual(gate["bypass_actors"], branch["bypass_actors"])
        required = next(r["parameters"] for r in gate["rules"] if r["type"] == "workflows")
        self.assertFalse(required["do_not_enforce_on_create"])
        workflow, = required["workflows"]
        pin = json.loads((RULESETS / "org-release-notes-gate.pin.json").read_text())
        self.assertEqual(gate["enforcement"], "disabled", "unresolved template must never be active")
        self.assertEqual(workflow["repository_id"], 0)
        self.assertEqual(workflow["sha"], "0" * 40, "no literal commit pin: squash merge keeps no branch SHA")
        self.assertNotIn("ref", workflow)
        self.assertEqual(workflow["path"], ".github/workflows/release-notes-gate.yml")
        self.assertEqual(pin["path"], workflow["path"])
        data = (ROOT / workflow["path"]).read_bytes()
        self.assertNotIn(b"\r", data)
        self.assertEqual(hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest(), pin["blob_sha"],
                         "gate workflow changed: review it and update org-release-notes-gate.pin.json")

    def test_ruleset_tests_need_no_git_history(self):
        source = Path(__file__).read_text()
        self.assertNotIn("import " + "sub" + "process", source)
        self.assertNotIn("git" + " show", source)

    def test_every_payload_has_a_documented_apply_path(self):
        payloads = {p.name for p in RULESETS.glob("*.json") if not p.name.endswith(".pin.json")}
        readme = (RULESETS / "README.md").read_text()
        rows = dict(re.findall(r"^\| `([a-z-]+\.json)` \|[^|]*\| ([^|]+?) \|$", readme, re.M))
        self.assertEqual(set(rows), payloads, "every payload needs one endpoint-table row")
        source = (ROOT / "hosting/paperclip-hermes/bootstrap/bootstrap.py").read_text()
        managed = {m + ".json" for m in re.findall(r'\("([a-z-]+)", f"(?:repos|orgs)/[^"]*/rulesets"\)', source)}
        self.assertEqual({p for p, by in rows.items() if "bootstrap" in by}, managed)
        for payload in payloads - managed:
            self.assertIn("by hand", rows[payload])
            self.assertIn(json.loads((RULESETS / payload).read_text())["name"], readme)
        self.assertIn("commits?sha=main&path=", readme, "manual apply must resolve the main commit")
        self.assertIn("org-release-notes-gate.pin.json", readme)
        self.assertIn("software-factory-main", readme)
        self.assertIn("by hand", rows["public-repo.json"])
        self.assertIn("bootstrap", rows["public-repo.json"])
        self.assertRegex(readme, r'f=hosting/paperclip-hermes/rulesets/public-repo.json[\s\S]*?--input "\$f"')
        self.assertLess(readme.index("f=hosting/paperclip-hermes/rulesets/public-repo.json"), readme.index("d=hosting/paperclip-hermes/rulesets;"))

    def test_release_tags_only_allow_release_actor_team_and_operator(self):
        tags = json.loads((RULESETS / "public-release-tags.json").read_text())
        self.assertEqual(tags["target"], "tag")
        self.assertEqual(tags["conditions"]["ref_name"], {"include": ["refs/tags/v*"], "exclude": []})
        self.assertEqual({r["type"] for r in tags["rules"]}, {"creation", "update", "deletion"})
        actors = tags["bypass_actors"]
        self.assertEqual({a["actor_type"] for a in actors}, {"Team", "OrganizationAdmin"})
        self.assertTrue(all(a["bypass_mode"] == "always" for a in actors))
        team, = [a for a in actors if a["actor_type"] == "Team"]
        if team["actor_id"] == 0:
            self.assertEqual(tags["enforcement"], "disabled", "unresolved actor must never be active")
        else:
            self.assertGreater(team["actor_id"], 0)


if __name__ == "__main__":
    unittest.main()
