"""Acceptance checks run the real command-line tools in isolated Git repositories."""

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parents[1]
ROOT = TOOLS.parents[2]


class RepositoryChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / "deny-list").mkdir()
        shutil.copyfile(ROOT / "deny-list/allow.txt", self.root / "deny-list/allow.txt")

    def run_check(self, script, expected, *args):
        env = dict(os.environ)
        env.pop("SF_PRIVATE_NAMES_HASH_FILE", None)
        result = subprocess.run([sys.executable, str(TOOLS / script), *args],
                                cwd=self.root, env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def test_empty_tree(self):
        self.run_check("deny-list.py", 0)
        self.run_check("check-workflows.py", 0)

    def test_planted_identifiers_fail_without_disclosure(self):
        planted = {
            "guid": "12345678" + "-1234" * 3 + "-123456789abc",
            "email": "private-operator" + "@" + "real-mail.invalid",
            "tailnet": "host.tail123abc" + ".ts.net",
        }
        for kind, value in planted.items():
            with self.subTest(kind=kind):
                (self.root / ".hidden-fixture").write_text(value.upper())
                result = self.run_check("deny-list.py", 1)
                self.assertIn("denied " + kind, result.stdout)
                self.assertNotIn(value.upper(), result.stdout + result.stderr)

    def test_public_forms_pass(self):
        (self.root / "CODEOWNERS").write_text("* @agenticcodingops/maintainers\n")
        (self.root / "docs.md").write_text(
            "12345+operator@users.noreply.github.com\n"
            "pilot.example.ts.net fixture.test.ts.net nested.pilot.EXAMPLE.TS.NET\n"
            "00000000-0000-0000-0000-000000000000\n"
            "operator@example.com password@storage.example.test\n"
            "`operator@example.com` 'operator@example.com'\n"
            "repo:<owner>@<id>/<repo>@<id>:environment:<env>\n")
        self.run_check("deny-list.py", 0)

    def test_suffix_spoofing_fails(self):
        for value in ("host.notexample" + ".ts.net", "host.nottest" + ".ts.net",
                      "operator" + "@" + "users.noreply.github.com.evil.invalid"):
            (self.root / "fixture").write_text(value)
            self.run_check("deny-list.py", 1)

    def test_allow_is_exact_and_does_not_exempt_a_line(self):
        (self.root / "fixture").write_text("operator@example.com private" + "@" + "mail.invalid")
        self.run_check("deny-list.py", 1)
        (self.root / "deny-list/allow.txt").write_text("*.invalid\n")
        self.run_check("deny-list.py", 2)

    def test_private_hashes(self):
        digest = hashlib.sha256(b"private-organization").hexdigest()
        private = self.root / "hashes"
        private.write_text("# instance-supplied\n" + digest + "\n")
        (self.root / "fixture").write_text("PRIVATE-ORGANIZATION")
        self.run_check("deny-list.py", 1, "--private-hashes", str(private))
        (self.root / "fixture").write_text("unrelated-organization")
        self.run_check("deny-list.py", 0, "--private-hashes", str(private))
        private.write_text("not-a-hash")
        self.run_check("deny-list.py", 2, "--private-hashes", str(private))
        private.unlink()
        self.run_check("deny-list.py", 2, "--private-hashes", str(private))

    def test_tracked_ignored_files_are_still_scanned(self):
        fixture = self.root / "tracked"
        fixture.write_text("host.real" + ".ts.net")
        subprocess.run(["git", "add", "tracked"], cwd=self.root, check=True)
        (self.root / ".gitignore").write_text("tracked\n")
        self.run_check("deny-list.py", 1)

    def workflow(self, reference, permissions="permissions: {contents: read}"):
        target = self.root / ".github/workflows/fixture.yml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("name: fixture\non: push\njobs:\n  check:\n    " + permissions
                          + "\n    runs-on: ubuntu-latest\n    steps:\n      - uses: " + reference + "\n")

    def test_unpinned_action_fails(self):
        for ref in ("actions/checkout@v7", "actions/checkout@abc123 # v7.0.1"):
            self.workflow(ref)
            self.run_check("check-workflows.py", 1)

    def test_sha_and_version_required(self):
        ref = "actions/checkout@" + "a" * 40
        self.workflow(ref)
        self.run_check("check-workflows.py", 1)
        self.workflow(ref + " # v7.0.1")
        self.run_check("check-workflows.py", 0)

    def test_each_job_needs_permissions(self):
        self.workflow("./.github/actions/check", "timeout-minutes: 5")
        self.run_check("check-workflows.py", 1)
        self.workflow("./.github/actions/check", "permissions: write-all")
        self.run_check("check-workflows.py", 1)
        self.workflow("./.github/actions/check", "permissions: {}")
        self.run_check("check-workflows.py", 0)


if __name__ == "__main__":
    unittest.main()
