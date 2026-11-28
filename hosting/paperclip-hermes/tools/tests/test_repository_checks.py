"""Acceptance checks run the real command-line tools in isolated Git repositories."""

import ast
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parents[1]
ROOT = TOOLS.parents[2]


def scan_unittest_only(roots):
    """Find tests that unittest discovery would silently omit or cannot run."""
    findings = []
    for root in map(Path, roots):
        for path in sorted(root.rglob("test*.py")):
            tree = ast.parse(path.read_text(), filename=str(path))
            prefix = str(path) + ": "
            classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}

            def testcase_base(base, visiting):
                if isinstance(base, ast.Attribute):
                    return isinstance(base.value, ast.Name) and base.value.id == "unittest" and base.attr == "TestCase"
                if not isinstance(base, ast.Name):
                    return False
                if base.id in classes:
                    if base.id in visiting:
                        return False
                    return any(testcase_base(parent, visiting | {base.id}) for parent in classes[base.id].bases)
                return base.id.endswith("TestCase")

            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                    findings.append(prefix + "module-level test function " + node.name)
                if isinstance(node, ast.ClassDef):
                    tests = node.name.startswith("Test") or any(
                        isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name.startswith("test")
                        for child in node.body)
                    if tests and not any(testcase_base(base, {node.name}) for base in node.bases):
                        findings.append(prefix + "test class has no TestCase base: " + node.name)
            pytest_names = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name == "pytest" or alias.name.startswith("pytest."):
                            pytest_names.add(alias.asname or "pytest")
                            findings.append(prefix + "pytest import")
                elif isinstance(node, ast.ImportFrom) and node.module and (node.module == "pytest" or node.module.startswith("pytest.")):
                    pytest_names.update(alias.asname or alias.name for alias in node.names)
                    findings.append(prefix + "pytest import")
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant) and
                        isinstance(node.args[0].value, str) and node.args[0].value.split(".")[0] == "pytest" and
                        ((isinstance(node.func, ast.Name) and node.func.id == "__import__") or
                         (isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and
                          node.func.value.id == "importlib" and node.func.attr == "import_module"))):
                    findings.append(prefix + "pytest dynamic import")
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in pytest_names:
                    findings.append(prefix + "pytest attribute access")
            directory = path.parent
            while directory != root:
                if not (directory / "__init__.py").is_file():
                    findings.append(prefix + "undiscoverable directory " + str(directory))
                directory = directory.parent
    return findings


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

    def unittest_tree(self, source, relative="test_case.py"):
        root = self.root / "tests"
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
        return scan_unittest_only([root])

    def test_unittest_only_in_discovered_dirs(self):
        roots = [TOOLS / "tests", ROOT / "ci/renovate"]
        self.assertEqual(scan_unittest_only(roots), [])
        cases = [
            ("module_level", "def test_x(): pass", "test_case.py", True),
            ("pytest_import", "import pytest", "test_case.py", True),
            ("bare_class", "class TestFoo:\n    def test_a(self): pass", "test_case.py", True),
            ("missing_init", "import unittest\nclass Tests(unittest.TestCase):\n    def test_y(self): pass", "sub/test_y.py", True),
            ("testcase", "import unittest\nclass Tests(unittest.TestCase):\n    def test_x(self): pass", "test_case.py", False),
        ]
        for name, source, relative, rejected in cases:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(source)
                self.assertEqual(bool(scan_unittest_only([root])), rejected)

    def test_unittest_only_acceptance_module_level(self):
        for source in ("def test_x(): pass", "async def test_x(): pass"):
            with self.subTest(source=source):
                self.assertTrue(self.unittest_tree(source))

    def test_unittest_only_acceptance_pytest_import(self):
        for source in ("import pytest", "from pytest import fixture", "import pytest as p\np.mark.slow", "from pytest import mark as m\nm.slow",
                       'import importlib\nimportlib.import_module("pytest")', '__import__("pytest")'):
            with self.subTest(source=source):
                self.assertTrue(self.unittest_tree(source))

    def test_unittest_only_acceptance_bare_class(self):
        for base in ("", "(object)", "(dict)", "(Helper)"):
            with self.subTest(base=base):
                self.assertTrue(self.unittest_tree(f"class Helper: pass\nclass TestFoo{base}:\n    def test_a(self): pass"))

    def test_unittest_only_rejects_named_classes_without_testcase_base(self):
        for source in ("class TestFoo(dict): pass", "class Helper: pass\nclass TestFoo(Helper): pass",
                       "class A(B): pass\nclass B(A): pass\nclass TestFoo(A): pass"):
            with self.subTest(source=source):
                self.assertTrue(self.unittest_tree(source))

    def test_unittest_only_accepts_transitive_local_testcase_chain(self):
        for base in ("TestCase", "unittest.TestCase", "ImportedTestCase"):
            with self.subTest(base=base):
                source = (f"class Root({base}): pass\nclass Middle(Root): pass\n"
                          "class Leaf(Middle): pass\nclass TestFoo(Leaf):\n    def test_a(self): pass")
                self.assertEqual(self.unittest_tree(source), [])

    def test_unittest_only_acceptance_missing_init(self):
        source = "import unittest\nclass Tests(unittest.TestCase):\n    def test_y(self): pass"
        self.assertTrue(self.unittest_tree(source, "sub/nested/test_y.py"))
        (self.root / "tests/sub/__init__.py").touch()
        self.assertTrue(scan_unittest_only([self.root / "tests"]))
        (self.root / "tests/sub/nested/__init__.py").touch()
        self.assertEqual(scan_unittest_only([self.root / "tests"]), [])

    def test_unittest_only_acceptance_testcase(self):
        self.assertEqual(self.unittest_tree("import unittest\nclass Tests(unittest.TestCase):\n    def test_x(self): pass"), [])

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
            "*.example.ts.net *.test.ts.net *.nested.example.ts.net\n"
            "00000000-0000-0000-0000-000000000000\n"
            "operator@example.com password@storage.example.test\n"
            "`operator@example.com` 'operator@example.com'\n"
            "repo:<owner>@<id>/<repo>@<id>:environment:<env>\n")
        self.run_check("deny-list.py", 0)

    def test_wildcard_tailnet_is_rejected(self):
        value = "*.tail123abc" + ".ts.net"
        (self.root / "wildcard.txt").write_text(value)
        result = self.run_check("deny-list.py", 1)
        self.assertIn("wildcard.txt:1: denied tailnet", result.stdout)
        self.assertNotIn(value, result.stdout + result.stderr)

    def test_tailnet_after_punctuation_is_rejected(self):
        for name, value in (("dot", "NO_PROXY=.tail123abc" + ".ts.net"),
                            ("underscore", "_acme-challenge.host.tail123abc" + ".ts.net"),
                            ("hyphen", "x-host.tail123abc" + ".ts.net")):
            with self.subTest(name):
                path = self.root / f"{name}.txt"
                path.write_text(value)
                result = self.run_check("deny-list.py", 1)
                self.assertIn(f"{name}.txt:1: denied tailnet", result.stdout)
                self.assertNotIn(value, result.stdout + result.stderr)
                path.unlink()

    def test_utf16le_tailnet_is_rejected(self):
        self.check_encoded_tailnet("utf-16-le")

    def check_encoded_tailnet(self, encoding):
        value = "host.tail123abc" + ".ts.net"
        (self.root / "encoded.txt").write_bytes(("public text\n" + value).encode(encoding))
        result = self.run_check("deny-list.py", 1)
        self.assertIn("encoded.txt:2: denied tailnet", result.stdout)
        self.assertNotIn(value, result.stdout + result.stderr)

    def test_supported_text_encodings_are_scanned(self):
        for encoding in ("utf-8-sig", "utf-16", "utf-16-be", "utf-32", "utf-32-le", "utf-32-be"):
            with self.subTest(encoding=encoding):
                self.check_encoded_tailnet(encoding)

    def test_bom_marked_big_endian_text_is_scanned(self):
        for encoding, bom in (("utf-16-be", b"\xfe\xff"), ("utf-32-be", b"\x00\x00\xfe\xff")):
            with self.subTest(encoding=encoding):
                value = "host.tail123abc" + ".ts.net"
                (self.root / "encoded.txt").write_bytes(bom + value.encode(encoding))
                result = self.run_check("deny-list.py", 1)
                self.assertIn("encoded.txt:1: denied tailnet", result.stdout)

    def test_undecodable_text_fails_closed_without_disclosure(self):
        for data in (b"private\xff", b"\xff\xfeh\x00x", b"plain\x00text"):
            with self.subTest(data=data):
                (self.root / "unsupported.txt").write_bytes(data)
                result = self.run_check("deny-list.py", 1)
                self.assertIn("unsupported.txt: denied undecodable text", result.stdout)
                self.assertNotIn("private", result.stdout + result.stderr)

    def test_supported_public_text_encodings_pass(self):
        for encoding in ("utf-8", "utf-16", "utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be"):
            with self.subTest(encoding=encoding):
                (self.root / "encoded.txt").write_bytes("host.example.ts.net\n".encode(encoding))
                self.run_check("deny-list.py", 0)

    def test_operator_pr_bypass_preserves_code_owner_review(self):
        ruleset = json.loads((ROOT / "hosting/paperclip-hermes/rulesets/public-repo.json").read_text())
        self.assertEqual(ruleset["bypass_actors"], [
            {"actor_type": "OrganizationAdmin", "bypass_mode": "pull_request"}])
        review = next(rule["parameters"] for rule in ruleset["rules"] if rule["type"] == "pull_request")
        self.assertTrue(review["require_code_owner_review"])
        self.assertGreaterEqual(review["required_approving_review_count"], 1)
        self.assertIn("* @vibecoddingops", (ROOT / "CODEOWNERS").read_text().splitlines())

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

    def test_action_input_named_uses_is_not_a_reference(self):
        (self.root / "action.yml").write_text(
            "name: fixture\ninputs:\n  uses:\n    description: target\n"
            "runs:\n  using: composite\n  steps:\n"
            "    - uses: ./local-action\n      with:\n        uses: arbitrary-input\n")
        self.workflow("./local-action\n        with:\n          uses: arbitrary-input")
        self.run_check("check-workflows.py", 0)

    def test_composite_action_references_require_sha_and_version(self):
        for ref, expected in (("actions/checkout@v7", 1),
                              ("actions/checkout@" + "a" * 40, 1),
                              ("actions/checkout@" + "a" * 40 + " # v7.0.1", 0)):
            with self.subTest(ref=ref):
                (self.root / "action.yml").write_text(
                    "name: fixture\nruns:\n  using: composite\n  steps:\n    - uses: " + ref + "\n")
                self.run_check("check-workflows.py", expected)

    def test_reusable_workflow_references_require_sha_and_version(self):
        target = self.root / ".github/workflows/fixture.yml"
        target.parent.mkdir(parents=True)
        for ref, expected in (("owner/repo/.github/workflows/check.yml@v1", 1),
                              ("owner/repo/.github/workflows/check.yml@" + "a" * 40, 1),
                              ("owner/repo/.github/workflows/check.yml@" + "a" * 40 + " # v1.0.0", 0)):
            with self.subTest(ref=ref):
                target.write_text("on: push\njobs:\n  check:\n    permissions: {}\n    uses: " + ref + "\n")
                self.run_check("check-workflows.py", expected)

    def test_non_scalar_action_reference_is_reported(self):
        self.workflow("{invalid: mapping}")
        result = self.run_check("check-workflows.py", 1)
        self.assertIn("uses must pin a full commit SHA", result.stdout)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
