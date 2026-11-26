"""Ruleset contracts: require workflow identity and limit release-tag bypass."""

import json
from pathlib import Path
import subprocess
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
        self.assertEqual(gate["enforcement"], "active")
        self.assertEqual(gate["conditions"]["repository_name"]["include"], ["software-factory"])
        self.assertEqual(gate["conditions"]["ref_name"]["include"], ["refs/heads/main"])
        self.assertEqual(gate["bypass_actors"], branch["bypass_actors"])
        required = next(r["parameters"] for r in gate["rules"] if r["type"] == "workflows")
        self.assertFalse(required["do_not_enforce_on_create"])
        workflow, = required["workflows"]
        self.assertGreater(workflow["repository_id"], 0)
        self.assertRegex(workflow["sha"], r"^[a-f0-9]{40}$")
        self.assertNotEqual(workflow["sha"], "0" * 40)
        self.assertNotIn("ref", workflow)
        self.assertEqual(workflow["path"], ".github/workflows/release-notes-gate.yml")
        pinned = subprocess.check_output(["git", "show", f'{workflow["sha"]}:{workflow["path"]}'], cwd=ROOT)
        self.assertEqual(pinned, (ROOT / workflow["path"]).read_bytes(), "repin after changing the gate workflow")

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
