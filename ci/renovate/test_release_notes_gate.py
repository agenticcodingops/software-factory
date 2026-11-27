"""The trusted-default-branch notes gate fails closed and labels the current PR head."""

import copy
from contextlib import redirect_stdout
from io import StringIO
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("gate", Path(__file__).with_name("release-notes-gate.py"))
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)
PATTERN = r"v?\d{4}\.\d{3,4}\.\d+"


def release(tag="v2026.1124.0", body="Routine fixes."):
    return {"tag_name": tag, "body": body}


class FakeAPI:
    def __init__(self, body="Routine fixes.", image="paperclip"):
        self.body = body
        self.image = image
        self.files = []
        self.workflow_content = {}
        self.responses = {}
        self.base_sha = "a" * 40
        self.calls = []
        self.pr = {"user": {"login": "renovate[bot]"}, "base": {"sha": "d" * 40, "ref": "main"},
                   "head": {"sha": "b" * 40, "repo": {"full_name": "fixture/repo"}}}

    def contents(self, repo, sha, path):
        if path.startswith(".github/workflows/"):
            return self.workflow_content[(sha, path)]
        image = next(image for image, (_, _, file, _) in gate.IMAGES.items() if file == path)
        tag = "2026.1117.0" if sha == self.base_sha or self.image != "paperclip" else "2026.1124.0"
        digest = "c" * 64 if sha == self.base_sha else "d" * 64
        if "Dockerfile" in path:
            tag = "v0.23.4" if sha == self.base_sha or self.image != "hermes" else "v0.23.5"
            return f"# base\nFROM {image}:{tag}@sha256:{digest} AS runtime\nRUN true \\\n    && true\nFROM runtime AS sandbox\n"
        return ("x-runtime: &runtime\n  restart: unless-stopped\nservices:\n  postgres:\n    image: postgres:17@sha256:" + "e" * 64 + "\n"
                f"  paperclip:\n    <<: *runtime\n    image: {image}:{tag}@sha256:{digest}\n"
                "  hermes:\n    image: software-factory-hermes:${KIT_IMAGE_TAG:-local}\n")

    def registry_digest(self, image, tag):
        return "sha256:" + "d" * 64

    def releases(self, repo):
        return [release("v0.23.5" if self.image == "hermes" else "v2026.1124.0", self.body)]

    protected_changes = gate.GitHub.protected_changes

    def request(self, path, payload=None):
        self.calls.append((path, copy.deepcopy(payload)))
        if path in self.responses:
            response = self.responses[path]
            if isinstance(response, Exception):
                raise response
            return copy.deepcopy(response)
        if "/compare/" in path:
            return {"files": self.files[:300], "merge_base_commit": {"sha": self.base_sha}}
        if "/git/ref/heads/" in path:
            return {"object": {"sha": self.base_sha}}
        return copy.deepcopy(self.pr) if "/pulls/" in path else {}


class NotesTests(unittest.TestCase):
    def test_all_risk_words_and_skipped_releases(self):
        for word in ("BREAKING", "Migration", "rollback floor", "rollback-floor"):
            with self.subTest(word=word):
                result = gate.notes_findings("2026.1117.0", "2026.1124.0", [
                    release("v2026.1120.0", word), release(),
                    release("v2026.1125.0", "breaking"),
                ], PATTERN)
                self.assertEqual(len(result), 1)
                self.assertIn("1120", result[0])

    def test_deferred_notes_and_migration_stems(self):
        for body, hazard, deferred in (
            ("Full curated release notes for this window ship with v0.24.0. Items undocumented here on purpose: hermes gateway migrate --backends", True, True),
            ("Full curated release notes for this window ship with v0.24.0", False, True),
            ("migrate", True, False),
            ("No migrations, no configuration changes, no API changes.", True, False),
            ("## Breaking Changes\nNone of these require action...", True, False),
            ("this release becomes the rollback floor", True, False),
            ("Complete patch notes: corrected a typo and fixed session cleanup.", False, False),
            ("Routine fixes.", False, False),
        ):
            with self.subTest(body=body):
                result = gate.notes_findings("v0.23.3", "v0.23.4", [release("v0.23.4", body)], r"v0\.\d+\.\d+")
                self.assertEqual(any("notes deferred" in f for f in result), deferred)
                self.assertEqual(len(result), int(hazard) + int(deferred))

    def test_safe_notes_and_bad_missing_or_empty_notes(self):
        self.assertEqual(gate.notes_findings("2026.1117.0", "2026.1124.0", [release()], PATTERN), [])
        for releases in ([], [release(body="")], [dict(release(), draft=True)], [dict(release(), prerelease=True)]):
            with self.assertRaises(ValueError):
                gate.notes_findings("2026.1117.0", "2026.1124.0", releases, PATTERN)
        with self.assertRaises(ValueError):
            gate.notes_findings("v0.23.4", "v2026.9.24", [], r"v0\.\d+\.\d+")

    def invoke(self, api, base="main"):
        with tempfile.TemporaryDirectory() as temp:
            event = Path(temp) / "event.json"
            event.write_text(json.dumps({"number": 7, "repository": {"default_branch": "main"}, "pull_request": {"base": {"ref": base}}}))
            with patch.dict(os.environ, {"GITHUB_EVENT_PATH": str(event), "GITHUB_REPOSITORY": "fixture/repo", "GH_TOKEN": "fixture"}), patch.object(gate, "GitHub", return_value=api), redirect_stdout(StringIO()):
                return gate.main()

    def test_risk_labels_and_fails_actual_head_status(self):
        for word in ("breaking", "migration", "rollback floor"):
            api = FakeAPI(word)
            self.assertEqual(self.invoke(api), 1)
            self.assertIn(("repos/fixture/repo/issues/7/labels", {"labels": ["needs-operator"]}), api.calls)
            self.assertEqual(api.calls[-1][0], "repos/fixture/repo/statuses/" + "b" * 40)
            self.assertEqual(api.calls[-1][1]["state"], "failure")

    def test_safe_notes_pass(self):
        api = FakeAPI()
        self.assertEqual(self.invoke(api), 0)
        self.assertEqual(api.calls[-1][1]["state"], "success")
        self.assertFalse(any("/labels" in path for path, _ in api.calls))

    def test_fetch_failure_labels_and_fails_closed(self):
        api = FakeAPI()
        with patch.object(api, "releases", side_effect=OSError("unavailable")):
            self.assertEqual(self.invoke(api), 1)
        self.assertEqual(api.calls[-1][1]["state"], "failure")

    def test_plain_scalar_quote_hidden_upgrade_fails_before_network(self):
        api = FakeAPI()
        original = api.contents

        def hidden(repo, sha, file):
            if sha != api.base_sha and file.endswith("compose.yaml"):
                return ("x-e:\n  a: &runtime\n    image: evil.example/paperclip:9@sha256:" +
                        "d" * 64 + "\nservices:\n  paperclip:\n    <<: *runtime\n    x-a ': '\n" +
                        "    image: ghcr.io/paperclipai/paperclip:2026.1117.0@sha256:" +
                        "c" * 64 + "\n    ' # '\n")
            return original(repo, api.base_sha, file)

        with patch.object(api, "contents", side_effect=hidden), patch.object(api, "registry_digest") as registry, patch.object(api, "releases") as releases:
            self.assertEqual(self.invoke(api), 1)
        registry.assert_not_called()
        releases.assert_not_called()
        self.assertEqual(api.calls[-1][1]["state"], "failure")
        self.assertIn(("repos/fixture/repo/issues/7/labels", {"labels": ["needs-operator"]}), api.calls)

    def test_digest_only_needs_no_release_notes(self):
        api = FakeAPI(image="none")
        with patch.object(api, "releases", side_effect=AssertionError("digest must not need notes")), patch.object(api, "registry_digest", wraps=api.registry_digest) as registry:
            self.assertEqual(self.invoke(api), 0)
        self.assertEqual(registry.call_count, 2)

    def test_digest_mismatch_and_registry_errors_fail_closed(self):
        for image in ("none", "paperclip"):
            api = FakeAPI(image=image)
            with patch.object(api, "registry_digest", return_value="sha256:" + "e" * 64):
                self.assertEqual(self.invoke(api), 1)
            self.assertIn(("repos/fixture/repo/issues/7/labels", {"labels": ["needs-operator"]}), api.calls)
            self.assertEqual(api.calls[-1][1]["state"], "failure")
        api = FakeAPI()
        with patch.object(api, "registry_digest", side_effect=OSError("offline")):
            self.assertEqual(self.invoke(api), 1)
        self.assertEqual(api.calls[-1][1]["state"], "failure")

    def test_hidden_upgrade_fails_closed_end_to_end(self):
        api = FakeAPI(image="hermes")
        before = api.contents
        def hidden(repo, sha, file):
            if sha != api.base_sha and "Dockerfile" in file:
                return "# nousresearch/hermes-agent:v0.23.4@sha256:" + "c" * 64 + "\nFROM nousresearch/hermes-agent:\\\n" + "v0.24.0@sha256:" + "d" * 64 + " AS runtime\n"
            return before(repo, sha, file)
        with patch.object(api, "contents", side_effect=hidden):
            self.assertEqual(self.invoke(api), 1)
        self.assertEqual(api.calls[-1][1]["state"], "failure")
        self.assertIn(("repos/fixture/repo/issues/7/labels", {"labels": ["needs-operator"]}), api.calls)

    def test_compose_block_scalar_hidden_upgrade_fails_before_network(self):
        api = FakeAPI()
        original = api.contents
        def hidden(repo, sha, file):
            text = original(repo, sha, file)
            if sha != api.base_sha and file.endswith("compose.yaml"):
                pin = "    image: ghcr.io/paperclipai/paperclip:2026.1117.0@sha256:" + "c" * 64
                return "services:\n  paperclip:\n   <<: *evil\n   command: |\n" + pin + "\n"
            if "Dockerfile" in file:
                return original(repo, api.base_sha, file)
            return text
        with patch.object(api, "contents", side_effect=hidden), patch.object(api, "registry_digest") as registry, patch.object(api, "releases") as releases:
            self.assertEqual(self.invoke(api), 1)
        registry.assert_not_called()
        releases.assert_not_called()
        self.assertEqual(api.calls[-1][1]["state"], "failure")
        self.assertIn(("repos/fixture/repo/issues/7/labels", {"labels": ["needs-operator"]}), api.calls)

    def test_any_author_and_both_images_are_checked_against_current_main(self):
        for author in ("renovate[bot]", "agent", "operator"):
            for image in ("paperclip", "hermes"):
                with self.subTest(author=author, image=image):
                    api = FakeAPI("migration", image)
                    api.pr["user"]["login"] = author
                    with patch.object(api, "contents", wraps=api.contents) as contents:
                        self.assertEqual(self.invoke(api), 1)
                    before = [call.args[1] for call in contents.call_args_list[::2]]
                    self.assertEqual(before, [api.base_sha] * 2)
                    self.assertNotIn(api.pr["base"]["sha"], before)

    def test_nondefault_target_posts_nothing(self):
        api = FakeAPI()
        self.assertEqual(self.invoke(api, base="side"), 0)
        self.assertEqual(api.calls, [])
        api.pr["base"]["ref"] = "side"
        self.assertEqual(self.invoke(api), 1)
        self.assertTrue(all(payload is None for _, payload in api.calls))

    def test_workflow_changes_fail_including_deletions_and_renames(self):
        for file in ({"filename": ".github/workflows/forged.yml", "status": "added"},
                     {"filename": ".github/workflows/old.yml", "status": "removed"},
                     {"filename": "docs/old.yml", "previous_filename": ".github/workflows/old.yml"}):
            api = FakeAPI()
            api.files = [{"filename": f"docs/{i}.md"} for i in range(100)] + [file]
            self.assertEqual(self.invoke(api), 1)
            self.assertEqual(api.calls[-1][1]["state"], "failure")

    def test_truncated_diff_fails_closed(self):
        api = FakeAPI()
        api.files = [{"filename": "docs/file.md"}] * 3000
        self.assertEqual(self.invoke(api), 1)
        self.assertEqual(api.calls[-1][1]["state"], "failure")

    def test_workflow_check_is_bound_to_captured_shas_not_live_pr_files(self):
        api = FakeAPI()
        api.files = [{"filename": ".github/workflows/forged.yml", "status": "added"}]
        request = api.request
        def racing(path, payload=None):
            if "/pulls/7/files" in path:
                api.calls.append((path, None))
                return []
            return request(path, payload)
        with patch.object(api, "request", side_effect=racing):
            self.assertEqual(self.invoke(api), 1)
        paths = [path for path, _ in api.calls]
        self.assertFalse(any("/pulls/7/files" in path for path in paths))
        self.assertIn(f"repos/fixture/repo/compare/{'a' * 40}...{'b' * 40}?per_page=1&page=1", paths)
        self.assertIn(("repos/fixture/repo/issues/7/labels", {"labels": ["needs-operator"]}), api.calls)
        self.assertEqual(api.calls[-1][1]["state"], "failure")

    def test_compare_file_cap_fails_closed_at_300(self):
        for count, expected in ((299, 0), (300, 1)):
            api = FakeAPI()
            api.files = [{"filename": f"docs/{i}.md"} for i in range(count)]
            self.assertEqual(self.invoke(api), expected)
        api = FakeAPI()
        with patch.object(api, "request", side_effect=lambda path, payload=None: {} if "/compare/" in path else FakeAPI.request(api, path, payload)):
            self.assertEqual(self.invoke(api), 1)

    def test_gate_trust_base_changes_require_operator(self):
        for file in ({"filename": "ci/renovate/release-notes-gate.py"},
                     {"filename": "docs/old.py", "previous_filename": "ci/renovate/release-notes-gate.py"},
                     {"filename": "hosting/paperclip-hermes/rulesets/public-repo.json"},
                     {"filename": "CODEOWNERS"}):
            with self.subTest(file=file):
                api = FakeAPI()
                api.files = [file]
                self.assertEqual(self.invoke(api), 1)
        for name in ("renovate/default.json", "ci/renovate/check.cjs", "docs/CODEOWNERS"):
            with self.subTest(name=name):
                api = FakeAPI()
                api.files = [{"filename": name}]
                self.assertEqual(self.invoke(api), 0)

    def test_main_change_fails_closed(self):
        api = FakeAPI()
        request = api.request

        def changed(path, payload=None):
            result = request(path, payload)
            if "/git/ref/heads/" in path and sum("/git/ref/heads/" in p for p, _ in api.calls) == 2:
                result["object"]["sha"] = "e" * 40
            return result

        with patch.object(api, "request", side_effect=changed):
            self.assertEqual(self.invoke(api), 1)
        self.assertEqual(api.calls[-1][1]["state"], "failure")

    def test_retarget_during_run_posts_no_result(self):
        api = FakeAPI()
        request = api.request

        def changed(path, payload=None):
            result = request(path, payload)
            if path.endswith("/pulls/7") and sum(p.endswith("/pulls/7") for p, _ in api.calls) == 2:
                result["base"]["ref"] = "side"
            return result

        with patch.object(api, "request", side_effect=changed):
            self.assertEqual(self.invoke(api), 1)
        self.assertEqual([p["state"] for _, p in api.calls if p and "state" in p], ["pending", "error"])

        self.assertFalse(any("/labels" in path for path, _ in api.calls))

    def test_workflow_uses_default_branch_code_only(self):
        import yaml
        workflow = yaml.safe_load((Path(__file__).resolve().parents[2] / ".github/workflows/release-notes-gate.yml").read_text())
        self.assertEqual(workflow[True]["pull_request_target"]["branches"], ["main"])
        checkout = workflow["jobs"]["notes"]["steps"][0]
        self.assertEqual(checkout["with"]["ref"], "${{ github.sha }}")
        self.assertFalse(checkout["with"]["persist-credentials"])

    def test_pagination_checks_intermediate_page(self):
        api = gate.GitHub("fixture")
        with patch.object(api, "request", side_effect=[[release()] * 100, [release("v2026.1120.0", "migration")]]) as request:
            results = gate.notes_findings("2026.1117.0", "2026.1124.0", api.releases("fixture/repo"), PATTERN)
        self.assertEqual(len(results), 1)
        self.assertIn("page=2", request.call_args[0][0])

    def test_changed_head_cannot_receive_passing_status(self):
        api = FakeAPI()
        request = api.request

        def changed(path, payload=None):
            result = request(path, payload)
            if path.endswith("/pulls/7") and sum(p.endswith("/pulls/7") for p, _ in api.calls) == 2:
                result["head"]["sha"] = "c" * 40
            return result

        with patch.object(api, "request", side_effect=changed), self.assertRaisesRegex(ValueError, "head changed"):
            self.invoke(api)
        self.assertFalse(any(p and p.get("state") == "success" for _, p in api.calls))


class PinExtractionTests(unittest.TestCase):
    hermes = "nousresearch/hermes-agent"
    paper = "ghcr.io/paperclipai/paperclip"
    digest = "sha256:" + "c" * 64

    def docker(self, tag="v0.23.4"):
        return f"# base\nFROM {self.hermes}:{tag}@{self.digest} AS runtime\nRUN true \\\n    && true\nFROM runtime AS sandbox\n"

    def compose(self, tag="2026.1117.0"):
        return f"services:\n  paperclip:\n    image: {self.paper}:{tag}@{self.digest}\n  hermes:\n    image: software-factory-hermes:${{KIT_IMAGE_TAG:-local}}\n"

    def test_plain_pins_are_read(self):
        for tag in ("v0.23.4", "v0.23.5"):
            self.assertEqual(gate.dockerfile_pin(self.docker(tag), self.hermes), (tag, self.digest))
        for tag in ("2026.1117.0", "2026.1124.0"):
            self.assertEqual(gate.compose_pin(self.compose(tag), self.paper, "paperclip"), (tag, self.digest))

    def test_real_kit_files_parse(self):
        root = Path(__file__).resolve().parents[2]
        for image, (_, pattern, file, (kind, service)) in gate.IMAGES.items():
            text = (root / file).read_text()
            pin = gate.compose_pin(text, image, service) if kind == "compose" else gate.dockerfile_pin(text, image)
            self.assertRegex(pin[0], "^" + pattern + "$")

    def test_plain_scalar_continuations_are_rejected(self):
        root = Path(__file__).resolve().parents[2]
        text = (root / gate.IMAGES[self.paper][2]).read_text()
        tracked = next(line for line in text.splitlines(True) if line.startswith("    image: " + self.paper))
        for continuation in ("      other.example/paperclip\n", "     other\n",
                             "\n      other\n", "      # comment\n"):
            with self.subTest(continuation=continuation), self.assertRaisesRegex(ValueError, "multi-line plain scalars"):
                gate.compose_pin(text.replace(tracked, tracked + continuation), self.paper, "paperclip")
        with self.assertRaisesRegex(ValueError, "multi-line plain scalars"):
            gate.compose_pin(text.replace("  restart: unless-stopped\n", "  restart: unless-stopped\n    other\n"),
                             self.paper, "paperclip")
        self.assertEqual(gate.compose_pin(text, self.paper, "paperclip")[0], "2026.1117.0")

    def test_yaml_tags_are_rejected(self):
        root = Path(__file__).resolve().parents[2]
        text = (root / gate.IMAGES[self.paper][2]).read_text()
        variants = [text.replace("  paperclip:\n", "  paperclip:\n" + fragment, 1)
                    for fragment in ("    !reset\n", "    !override\n", "    !!map\n",
                                     "    &a\n    !reset\n", "    - !reset\n")]
        variants += [text.replace("  paperclip:\n", "  paperclip: !reset\n", 1),
                     text.replace("services:\n", "services: !!map\n", 1)]
        for variant in variants:
            with self.subTest(text=variant), self.assertRaises(ValueError):
                gate.compose_pin(variant, self.paper, "paperclip")
        self.assertEqual(gate.compose_pin(text, self.paper, "paperclip")[0], "2026.1117.0")

    def test_flow_scan_single_line_rules(self):
        rejected = [
            '    k: "a"b', '    k: &a b', '    k: !!str b', '    k: a[b',
            '    k: [a}', '    k: ${A', '    k: a]', '    k: [${A}]',
            "    k: 'it''s'", '    k: [', '    k: [a${B]}',
        ]
        accepted = [
            "    k: a # it's", 'x-runtime: &runtime', 'x-runtime: &runtime # c',
            '    k: ${A:-${B:-c}}', '    k: {a: [b, "c"]}',
        ]
        for line in rejected:
            with self.subTest(line=line), self.assertRaises(ValueError):
                gate.flow_scan(line)
        for line in accepted:
            with self.subTest(line=line):
                gate.flow_scan(line)

    def test_comment_cannot_satisfy_the_dockerfile_pin(self):
        comment = f"# {self.hermes}:v0.23.4@{self.digest}\n"
        variants = [
            comment + f"FROM {self.hermes}:\\\nv0.24.0@{self.digest} AS runtime\n",
            comment + f"ARG I={self.hermes}\nFROM ${{I}}:v0.24.0@{self.digest} AS runtime\n",
            comment + "FROM other:1 AS runtime\n",
            f"FROM evil.example/{self.hermes}:v0.23.4@{self.digest} AS runtime\n",
            self.docker() + "FR\\\nOM evil:1\n",
            "# escape=`\n" + self.docker(),
            "# syntax=docker/dockerfile:1\n" + self.docker(),
            "// syntax=docker/dockerfile:1\n" + self.docker(),
            "//syntax=evil.example/frontend:1\n" + self.docker(),
            "#!/usr/bin/env docker\n// syntax = evil.example/frontend:1\n" + self.docker(),
            self.docker() + "FROM evil:1\n",
            self.docker().replace("FROM", "\tFROM", 1),
            self.docker() + self.docker(),
        ]
        for text in variants:
            with self.subTest(text=text), self.assertRaises(ValueError):
                gate.dockerfile_pin(text, self.hermes)

    def test_dockerfile_rejects_non_ascii_and_control_whitespace(self):
        for text in (
            self.docker() + "\tFROM evil.example/x:1 AS final\n",
            self.docker().replace("RUN true", "RUN true\r", 1),
            self.docker() + "# caf\u00e9\n",
            "\ufeff" + self.docker(),
        ):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "printable ASCII"):
                gate.dockerfile_pin(text, self.hermes)

    def test_comment_cannot_satisfy_the_compose_pin(self):
        comment = f"# {self.paper}:2026.1117.0@{self.digest}\n"
        line = f"    image: {self.paper}:2026.1117.0@{self.digest}\n"
        variants = [
            comment + f'services:\n  paperclip:\n    image: "{self.paper}:\\\n      2026.1201.0@{self.digest}"\n',
            comment + f"services:\n  paperclip:\n    image: {self.paper}:${{T:-2026.1201.0@{self.digest}}}\n",
            comment,
            f"services:\n  other:\n{line}  paperclip:\n    image: evil/x@{self.digest}\n",
            self.compose() + "x: {image: evil}\n",
            self.compose().replace("image:", '"image":', 1),
            self.compose() + "    extends: base\n",
            self.compose() + "  paperclip:\n" + line,
            self.compose().replace(":2026.1117.0@", "@"),
            self.compose().replace(line, line + line),
            self.compose().replace(line, "    image: software-factory-hermes:${KIT_IMAGE_TAG:-local}\n"),
            self.compose().replace(self.paper, "mirror.example/" + self.paper),
        ]
        evil = "x-e:\n  a: &evil\n    image: evil.example/paperclip:9@sha256:" + "d" * 64 + "\n"
        explicit = "    ? image\n    : ghcr.io/paperclipai/${P:-paperclip}:2026.1201.0@sha256:" + "d" * 64 + "\n"
        variants += [
            "x-e:\n  a: &runtime\n    image: evil.example/paperclip:9@sha256:" + "d" * 64 + "\n" + "services:\n  paperclip:\n    <<: *runtime\n    labels: [\n" + line + "    ]\n",
            "services:\n  paperclip:\n    labels: {\n" + line + "    }\n" + explicit,
            "services:\n  paperclip:\n    labels: '\n" + line + "    '\n" + explicit,
            "x-k: &k image\nservices:\n  paperclip:\n    labels: {\n" + line + "    }\n    *k : ghcr.io/paperclipai/${P:-paperclip}:2026.1201.0@sha256:" + "d" * 64 + "\n",
            'services:\n  previous:\n    labels: "\n  paperclip:\n' + line + '    "\n  "paperclip":\n    image: evil.example/paperclip:9@sha256:' + 'd' * 64 + '\n',
            evil + "services:\n  paperclip:\n    labels: {\n" + line + "    }\n    <<: [*runtime, *evil]\n",
            evil + "services:\n <<:\n  paperclip:\n" + line + " paperclip:\n  <<: *evil\n",
            evil + "services:\n  paperclip:\n   <<: *evil\n   command: |\n" + line,
            evil + "services:\n  paperclip:\n   <<: *evil\n   labels:\n   - |\n" + line,
        ]
        runtime = evil.replace("&evil", "&runtime")
        for opening, closing in (
            ("    x-a'b: 'c", "    ' # '"),
            ('    a"b: "c', '    " # "'),
            ("    x-k: [ # ]", "    ] # ["),
            ("    x-k: { # }", "    } # {"),
            ("    x-a ': '", "    ' # '"),
            ("    x-a, ': '", "    ' # '"),
            ("    x-a :': '", "    ' # '"),
            ('    x-a ": "', '    " # "'),
            ("    x-k: [ a ', # ' ]", "    , x ' ] # ' [ ]"),
            ("    x-k: { a ', # ' }", "    , x ' } # ' { }"),
            ("    x-k ]: [", "    ]"),
            ("    labels: [", "    ]"),
        ):
            variants.append(runtime + "services:\n  paperclip:\n    <<: *runtime\n" +
                            opening + "\n" + line + closing + "\n")
        # The decoy service itself can be hidden inside a scalar. Quotes in comments
        # must never close the scanner's quotes or collections.
        for opening, closing in (
            ('      a"b: \'x" y', '      z "y\' # "'),
            ("      a'b: \"x' y", "      z 'y\" # '"),
            ('    x-j: [a - "b, c: \'x" y]', '      z - "q\'] # "'),
            ("    x-j: [a - 'b, c: \"x' y]", "      z - 'q\"] # '"),
            ("    x-a'b: 'c", "    ' # '"),
        ):
            variants.append("services:\n  previous:\n" + opening + "\n  paperclip:\n" +
                            line + closing + '\n  "paperclip":\n    image: evil.example/paperclip:9@sha256:' +
                            "d" * 64 + "\n")
        for text in variants:
            with self.subTest(text=text), self.assertRaises(ValueError):
                gate.compose_pin(text, self.paper, "paperclip")

    def test_non_ascii_and_document_markers_fail(self):
        for text in (self.compose() + "\r", self.compose() + "\ufeff", "---\n" + self.compose()):
            with self.assertRaises(ValueError):
                gate.compose_pin(text, self.paper, "paperclip")


class ComposeOracleTests(unittest.TestCase):
    invoke = NotesTests.invoke
    paper = PinExtractionTests.paper

    def kit(self):
        return (Path(__file__).resolve().parents[2] / gate.IMAGES[self.paper][2]).read_text()

    def fake_binary(self, directory, program):
        binary = Path(directory) / "docker-compose"
        binary.write_text(f"#!{sys.executable}\n" + program + "\n")
        binary.chmod(0o755)
        return patch.dict(os.environ, {"PATH": directory + os.pathsep + os.environ["PATH"]})

    def test_real_compose_confirms_kit_pin(self):
        text = self.kit()
        self.assertEqual(gate.checked_compose_pin(text, self.paper, "paperclip"),
                         gate.compose_pin(text, self.paper, "paperclip"))

    def test_oracle_is_independent_of_scanner_on_divergent_inputs(self):
        text = self.kit()
        pin = gate.compose_pin(text, self.paper, "paperclip")
        tracked = next(line for line in text.splitlines(True) if line.startswith("    image: " + self.paper))
        variants = [text.replace(tracked, tracked + continuation) for continuation in
                    ("      other.example/paperclip\n", "     other\n", "\n      other\n")]
        variants.append(text.replace("  paperclip:\n", "  paperclip:\n    !reset\n", 1))
        # Mutate away the entire scanner: only the real Compose oracle can reject.
        for variant in variants:
            with self.subTest(text=variant), patch.object(gate, "compose_pin", return_value=pin):
                with self.assertRaises(ValueError):
                    gate.checked_compose_pin(variant, self.paper, "paperclip")

    def test_scanner_rejects_before_oracle_runs(self):
        with patch.object(gate.subprocess, "run") as run, self.assertRaises(ValueError):
            gate.checked_compose_pin("services: {}", self.paper, "paperclip")
        run.assert_not_called()

    def test_oracle_failures_label_and_fail_status_on_base_and_head(self):
        expected = self.paper + ":2026.1117.0@sha256:" + "c" * 64
        programs = {
            "exit": "raise SystemExit(4)",
            "timeout": "import time; time.sleep(30)",
            "invalid-json": "print('not JSON')",
            "missing-service": "print('{\"services\": {}}')",
            "different-image": "print('{\"services\": {\"paperclip\": {\"image\": \"other:1\"}}}')",
        }
        for value in ([], None, {"services": []}, {"services": {"paperclip": []}},
                      {"services": {"paperclip": {}}}, {"services": {"paperclip": {"image": 1}}}):
            programs[repr(value)] = "print(" + repr(json.dumps(value)) + ")"
        programs["duplicate-key"] = "print(" + repr('{"services":{},"services":{"paperclip":{"image":' + json.dumps(expected) + '}}}') + ")"
        programs["non-json-constant"] = "print(" + repr('{"x":NaN,"services":{"paperclip":{"image":' + json.dumps(expected) + '}}}') + ")"
        # Let base or head alone fail. The equal scanner pins must not skip the oracle.
        for side in ("base", "head"):
            for name, failure in programs.items():
                with self.subTest(side=side, failure=name), tempfile.TemporaryDirectory() as directory:
                    program = ("from pathlib import Path\n"
                               "p = Path(" + repr(str(Path(directory) / "calls")) + ")\n"
                               "n = int(p.read_text()) if p.exists() else 0\np.write_text(str(n+1))\n"
                               "if n == " + str(int(side == "head")) + ":\n" +
                               "\n".join("    " + line for line in failure.splitlines()) +
                               "\nelse:\n    print(" + repr(json.dumps({"services": {"paperclip": {"image": expected}}})) + ")")
                    api = FakeAPI()
                    original = api.contents
                    def unchanged(repo, sha, path):
                        return original(repo, api.base_sha, path)
                    with self.fake_binary(directory, program), patch.object(api, "contents", side_effect=unchanged), patch.object(gate, "COMPOSE_TIMEOUT", 5 if name == "timeout" else 60):
                        self.assertEqual(self.invoke(api), 1)
                    self.assertEqual(int((Path(directory) / "calls").read_text()), 1 + int(side == "head"))
                    self.assertEqual(api.calls[-1][1]["state"], "failure")
                    self.assertIn(("repos/fixture/repo/issues/7/labels", {"labels": ["needs-operator"]}), api.calls)

    def test_missing_verified_binary_fails(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"PATH": directory}):
            with self.assertRaisesRegex(ValueError, "verified Compose is unavailable"):
                gate.checked_compose_pin(self.kit(), self.paper, "paperclip")

    def test_subprocess_has_only_explicit_environment_and_private_files(self):
        text = self.kit()
        pin = gate.compose_pin(text, self.paper, "paperclip")
        expected = {"services": {"paperclip": {"image": f"{self.paper}:{pin[0]}@{pin[1]}"}}}
        program = "print(" + repr(json.dumps(expected)) + ")"
        runner_env = {"GH_TOKEN": "runner-fixture", "GITHUB_TOKEN": "runner-fixture",
                      "COMPOSE_FILE": "runner.yaml", "COMPOSE_PROFILES": "runner",
                      "DOCKER_HOST": "runner", "ARBITRARY_RUNNER_VALUE": "runner"}
        original = subprocess.run
        def inspect(*args, **kwargs):
            root = Path(kwargs["cwd"])
            self.assertEqual(set(p.name for p in root.iterdir()), {"compose.yaml", "docker-config"})
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)
            self.assertEqual((root / "compose.yaml").read_bytes(), text.encode())
            self.assertEqual(list((root / "docker-config").iterdir()), [])
            env = kwargs["env"]
            self.assertEqual(set(env), {"PATH", "HOME", "DOCKER_CONFIG"} | set(gate.COMPOSE_VALUES))
            self.assertTrue(set(runner_env).isdisjoint(env))
            self.assertEqual(env, {"PATH": os.environ["PATH"], "HOME": str(root),
                                   "DOCKER_CONFIG": str(root / "docker-config"), **gate.COMPOSE_VALUES})
            self.assertEqual(args[0][1:], ["-f", "compose.yaml", "--profile", "*", "config", "--format", "json"])
            self.assertEqual(kwargs["timeout"], 60)
            return original(*args, **kwargs)
        with tempfile.TemporaryDirectory() as directory, self.fake_binary(directory, program), patch.dict(os.environ, runner_env):
            with patch.object(gate.subprocess, "run", side_effect=inspect) as run:
                self.assertEqual(gate.checked_compose_pin(text, self.paper, "paperclip"), pin)
            self.assertEqual(run.call_count, 1)
            self.assertFalse(Path(run.call_args.kwargs["cwd"]).exists())


class RegistryTests(unittest.TestCase):
    def response(self, data=b"", status=200, headers=None):
        from io import BytesIO
        result = BytesIO(data)
        result.status = status
        result.headers = headers or {}
        return result

    def test_registry_urls_and_no_github_token(self):
        digest = "sha256:" + "d" * 64
        for image, token_url, head_url in (
            ("ghcr.io/paperclipai/paperclip", "https://ghcr.io/token?service=ghcr.io&scope=repository:paperclipai/paperclip:pull", "https://ghcr.io/v2/paperclipai/paperclip/manifests/2026.1117.0"),
            ("nousresearch/hermes-agent", "https://auth.docker.io/token?service=registry.docker.io&scope=repository:nousresearch/hermes-agent:pull", "https://registry-1.docker.io/v2/nousresearch/hermes-agent/manifests/v0.23.4"),
        ):
            tag = "v0.23.4" if image.startswith("nous") else "2026.1117.0"
            with patch.object(gate, "build_opener") as builder:
                opener = builder.return_value
                opener.open.side_effect = [self.response(b'{"token":"registry-placeholder"}'), self.response(headers={"Docker-Content-Digest": digest})]
                self.assertEqual(gate.GitHub("github-placeholder").registry_digest(image, tag), digest)
                calls = opener.open.call_args_list
                self.assertEqual([call.args[0].full_url for call in calls], [token_url, head_url])
                self.assertEqual(calls[1].args[0].get_method(), "HEAD")
                self.assertEqual(calls[1].args[0].get_header("Authorization"), "Bearer registry-placeholder")
                self.assertIn("application/vnd.oci.image.index.v1+json", calls[1].args[0].get_header("Accept"))
                for call in calls:
                    self.assertNotIn("github-placeholder", str(call.args[0].header_items()))
                    self.assertEqual(call.kwargs, {"timeout": 30})
                self.assertIsInstance(builder.call_args.args[0], gate.NoRedirect)

    def test_bad_registry_responses_fail(self):
        for status, digest in ((200, None), (200, "sha256:BAD"), (201, "sha256:" + "d" * 64), (404, "sha256:" + "d" * 64)):
            with self.subTest(status=status, digest=digest), patch.object(gate, "build_opener") as builder:
                builder.return_value.open.side_effect = [self.response(b'{"token":"registry-placeholder"}'), self.response(status=status, headers={"Docker-Content-Digest":digest} if digest else {})]
                with self.assertRaises(ValueError):
                    gate.GitHub("github-placeholder").registry_digest("ghcr.io/paperclipai/paperclip", "2026.1117.0")

    def test_github_reads_reject_non200_and_writes_accept_creation(self):
        for status in (201, 204, 404, 500):
            with self.subTest(status=status), patch.object(gate, "build_opener") as builder:
                builder.return_value.open.return_value = self.response(b'{}', status=status)
                with self.assertRaises(ValueError):
                    gate.GitHub("github-placeholder").request("repos/fixture/repo")
        with patch.object(gate, "build_opener") as builder:
            builder.return_value.open.return_value = self.response(b'{"sha":"created"}', status=201)
            self.assertEqual(gate.GitHub("github-placeholder").request("repos/fixture/repo/contents/index.json", {}, method="PUT"), {"sha":"created"})

    def test_redirects_are_refused(self):
        with self.assertRaises(ValueError):
            gate.NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://other.example/")


class WorkflowPinTests(unittest.TestCase):
    invoke = NotesTests.invoke
    old = "a" * 40
    new = "f" * 40
    file = ".github/workflows/fixture.yml"

    def line(self, sha=None, tag="v7.0.2", action="actions/checkout", prefix="      - uses: "):
        return f"{prefix}{action}@{sha or self.new} # {tag}\n"

    def api(self, old=None, new=None, status="modified"):
        api = FakeAPI(image="none")
        api.files = [{"filename":self.file, "status":status}]
        api.workflow_content = {
            (api.base_sha, self.file): old or self.line(self.old, "v7.0.1"),
            (api.pr["head"]["sha"], self.file): new or self.line(),
        }
        api.responses["repos/actions/checkout/git/ref/tags/v7.0.2"] = {"object":{"type":"commit", "sha":self.new}}
        return api

    def assert_result(self, api, expected):
        self.assertEqual(self.invoke(api), expected)
        self.assertEqual(api.calls[-1][1]["state"], "failure" if expected else "success")
        self.assertEqual(any("/labels" in path for path, _ in api.calls), bool(expected))

    def test_verified_lightweight_and_annotated_patch_bumps_pass(self):
        for annotated in (False, True):
            with self.subTest(annotated=annotated):
                api = self.api()
                if annotated:
                    api.responses["repos/actions/checkout/git/ref/tags/v7.0.2"] = {"object":{"type":"tag", "sha":"e" * 40}}
                    api.responses["repos/actions/checkout/git/tags/" + "e" * 40] = {"object":{"type":"commit", "sha":self.new}}
                self.assert_result(api, 0)

    def test_minor_and_action_subpath_bumps_pass(self):
        api = self.api(old=self.line(self.old, "v7.0.1", "actions/checkout/sub"), new=self.line(tag="v7.1.0", action="actions/checkout/sub"))
        api.responses["repos/actions/checkout/git/ref/tags/v7.1.0"] = {"object":{"type":"commit", "sha":self.new}}
        self.assert_result(api, 0)

    def test_unverified_or_missing_tag_fails(self):
        from urllib.error import HTTPError
        for response in ({"object":{"type":"commit", "sha":"c" * 40}}, HTTPError("https://api.github.com/fixture", 404, "missing", {}, None), {}):
            api = self.api()
            api.responses["repos/actions/checkout/git/ref/tags/v7.0.2"] = response
            self.assert_result(api, 1)

    def test_only_same_action_same_major_version_advances_pass(self):
        for new in (
            self.line(tag="v8.0.0"), self.line(tag="v6.0.0"), self.line(tag="v7.0.1"),
            self.line(action="actions/setup-node"), self.line(prefix="    - uses: "),
            self.line(prefix="      uses: "), self.line() + "      run: echo changed\n",
            self.line() + "permissions: write-all\n", self.line() + "        with:\n",
            self.line(sha="v7"), self.line(action="./local"), self.line(action="docker://example/action"),
        ):
            with self.subTest(new=new):
                api = self.api(new=new)
                for tag in ("v8.0.0", "v6.0.0", "v7.0.1"):
                    api.responses[f"repos/actions/checkout/git/ref/tags/{tag}"] = {"object":{"type":"commit", "sha":self.new}}
                api.responses["repos/actions/setup-node/git/ref/tags/v7.0.2"] = {"object":{"type":"commit", "sha":self.new}}
                self.assert_result(api, 1)
                self.assertFalse(any("/git/ref/tags/" in path for path, _ in api.calls))
        for extra in ("      run: echo old\n", "permissions: read-all\n"):
            self.assert_result(self.api(old=self.line(self.old, "v7.0.1") + extra, new=self.line() + extra.replace("old", "new").replace("read-all", "write-all")), 1)
        api = self.api(old=self.line(self.old, "v7.0.1", "fixture/repo"), new=self.line(action="fixture/repo"))
        api.responses["repos/fixture/repo/git/ref/tags/v7.0.2"] = {"object":{"type":"commit", "sha":self.new}}
        self.assert_result(api, 1)
        self.assertFalse(any("/git/ref/tags/" in path for path, _ in api.calls))
        self.assertIsNone(gate.ACTION_PIN.fullmatch(self.line(action="./local")))
        self.assertIsNone(gate.ACTION_PIN.fullmatch(self.line(action="docker://example/action")))

    def test_tag_must_resolve_to_commit_object(self):
        for kind in ("tree", "blob"):
            for annotated in (False, True):
                with self.subTest(kind=kind, annotated=annotated):
                    api = self.api()
                    obj = {"object":{"type":kind, "sha":self.new}}
                    ref = "repos/actions/checkout/git/ref/tags/v7.0.2"
                    if annotated:
                        api.responses[ref] = {"object":{"type":"tag", "sha":"e" * 40}}
                        api.responses["repos/actions/checkout/git/tags/" + "e" * 40] = obj
                    else:
                        api.responses[ref] = obj
                    self.assert_result(api, 1)

    def test_empty_diff_requires_operator(self):
        for same in (self.line(), ""):
            with self.subTest(same=same):
                api = self.api()
                for key in api.workflow_content:
                    api.workflow_content[key] = same
                self.assert_result(api, 1)

    def test_workflow_pin_bumps_preserve_all_other_bytes(self):
        old = self.line(self.old, "v7.0.1") + "        if: true\n"
        new = self.line() + "        if: true\n"
        variants = [new.replace("\n", char, 1) for char in ("\x0c", "\x0b", "\x1c", "\x85", "\u2028", "\r\n")]
        for char in ("\u00a0", "\t", "  "):
            variants += [new.replace(" # ", char + "# "), new.replace(" # ", " #" + char)]
        variants += [new.replace("v7.0.2\n", "v7.0.2 \n"), new.replace("v7.0.2", "v7.0.\u0663"), new.replace("if: true", "if: true # caf\u00e9")]
        for text in variants:
            with self.subTest(text=text):
                api = self.api(old=old, new=text)
                api.responses["repos/actions/checkout/git/ref/tags/v7.0.\u0663"] = {"object":{"type":"commit", "sha":self.new}}
                self.assert_result(api, 1)
                self.assertFalse(any("/git/ref/tags/" in path for path, _ in api.calls))

        # A pin bump must preserve the final LF as well as every unchanged line.
        api = self.api(old=self.line(self.old, "v7.0.1"), new=self.line().removesuffix("\n"))
        self.assert_result(api, 1)
        self.assertFalse(any("/git/ref/tags/" in path for path, _ in api.calls))

    def test_workflow_status_and_strict_paths_are_protected(self):
        for status in ("added", "removed", "renamed", "copied", "changed", "unknown"):
            self.assert_result(self.api(status=status), 1)
        for path in ("ci/renovate/release-notes-gate.py", "hosting/paperclip-hermes/rulesets/public-repo.json", "CODEOWNERS"):
            api = self.api()
            api.files = [{"filename":path, "status":"modified", "patch": self.line()}]
            self.assert_result(api, 1)
            self.assertFalse(any("/git/ref/tags/" in path for path, _ in api.calls))

    def test_gate_workflow_is_strictly_protected_even_for_verified_bumps(self):
        api = self.api()
        self.assertEqual(api.protected_changes("fixture/repo", api.base_sha, api.pr["head"]["sha"]), [])
        path = ".github/workflows/release-notes-gate.yml"
        api.files.append({"filename":path, "status":"modified"})
        for sha in (api.base_sha, api.pr["head"]["sha"]):
            api.workflow_content[(sha, path)] = api.workflow_content[(sha, self.file)]
        self.assertEqual(api.protected_changes("fixture/repo", api.base_sha, api.pr["head"]["sha"]),
                         ["Gate, workflow, ruleset or CODEOWNERS changes require the operator"])
        self.assert_result(api, 1)

    def test_work_limits_stop_before_upstream_tag_calls(self):
        api = self.api()
        api.files *= 21
        with patch.object(api, "contents", wraps=api.contents) as contents:
            self.assert_result(api, 1)
        self.assertFalse(any(call.args[2].startswith(".github/workflows/") for call in contents.call_args_list))
        api = self.api(old=self.line(self.old, "v7.0.1") * 51, new=self.line() * 51)
        self.assert_result(api, 1)
        self.assertFalse(any("/git/ref/tags/" in path for path, _ in api.calls))

    def test_immutable_merge_base_contents_errors_and_tag_chain_limit(self):
        api = self.api()
        before = api.contents
        def fail(repo, sha, path):
            if path == self.file:
                raise OSError("contents unavailable")
            return before(repo, sha, path)
        with patch.object(api, "contents", side_effect=fail):
            self.assert_result(api, 1)
        api = self.api()
        compare = f"repos/fixture/repo/compare/{api.base_sha}...{api.pr['head']['sha']}?per_page=1&page=1"
        api.responses[compare] = {"files":api.files}
        self.assert_result(api, 1)
        api = self.api()
        api.responses["repos/actions/checkout/git/ref/tags/v7.0.2"] = {"object":{"type":"tag", "sha":"e" * 40}}
        api.responses["repos/actions/checkout/git/tags/" + "e" * 40] = {"object":{"type":"tag", "sha":"e" * 40}}
        self.assert_result(api, 1)
        self.assertEqual(sum("/git/tags/" in path for path, _ in api.calls), 5)
        api = self.api()
        merge_base = "9" * 40
        api.responses[compare] = {"files":api.files, "merge_base_commit":{"sha":merge_base}}
        api.workflow_content[(merge_base, self.file)] = api.workflow_content.pop((api.base_sha, self.file))
        with patch.object(api, "contents", wraps=api.contents) as contents:
            self.assert_result(api, 0)
        self.assertIn(("fixture/repo", merge_base, self.file), [c.args for c in contents.call_args_list])

    def test_mixed_image_changes_still_get_evaluated(self):
        api = self.api()
        self.assert_result(api, 0)  # FakeAPI(image='none') still changes both digests.
        api = self.api()
        api.image, api.body = "hermes", "migration"
        with patch.object(api, "releases", wraps=api.releases) as releases:
            self.assert_result(api, 1)
        releases.assert_called_once_with("NousResearch/hermes-agent")

    def test_action_soak_documentation_matches_image_only_policy(self):
        root = Path(__file__).resolve().parents[2]
        readme = (root / "renovate/README.md").read_text()
        self.assertIn("Actions pin bumps have no soak period", readme)
        self.assertIn("two current-head reviews are the only other control", readme)
        source = (root / "ci/renovate/release-notes-gate.py").read_text()
        self.assertIn("no release-age soak applies to Actions tags", source)
        preset = json.loads((root / "renovate/default.json").read_text())
        age_rules = [r for r in preset["packageRules"] if "minimumReleaseAge" in r]
        self.assertTrue(age_rules)
        self.assertTrue(all(r.get("matchDatasources") == ["docker"] or r.get("matchPackageNames") == ["ghcr.io/paperclipai/paperclip"] for r in age_rules))

    def test_contents_decode_and_github_token_host(self):
        from io import BytesIO
        import base64
        api = gate.GitHub("github-placeholder")
        with patch.object(api, "request", return_value={"content":base64.b64encode(b"\xff").decode()}), self.assertRaises(UnicodeDecodeError):
            api.contents("fixture/repo", "a" * 40, self.file)
        with patch.object(gate, "build_opener") as builder:
            response = BytesIO(b'{}')
            response.status = 200
            builder.return_value.open.return_value = response
            api.request("repos/fixture/repo")
            request = builder.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, "https://api.github.com/repos/fixture/repo")
            self.assertEqual(request.get_header("Authorization"), "Bearer github-placeholder")
            self.assertIsInstance(builder.call_args.args[0], gate.NoRedirect)


if __name__ == "__main__":
    unittest.main()
