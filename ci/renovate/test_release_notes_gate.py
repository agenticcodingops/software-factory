"""The trusted-base notes gate fails closed and labels the current PR head."""

import copy
from contextlib import redirect_stdout
from io import StringIO
import importlib.util
import json
import os
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
    def __init__(self, body="Routine fixes."):
        self.body = body
        self.calls = []
        self.pr = {"user": {"login": "renovate[bot]"}, "base": {"sha": "a" * 40},
                   "head": {"sha": "b" * 40, "repo": {"full_name": "fixture/repo"}}}

    def contents(self, repo, sha, path):
        image = next(image for image, (_, _, file) in gate.IMAGES.items() if file == path)
        tag = "2026.1117.0" if sha == "a" * 40 else "2026.1124.0"
        if "Dockerfile" in path:
            tag = "v0.23.4"
        return f"{image}:{tag}@sha256:{'c' * 64}"

    def releases(self, repo):
        return [release(body=self.body)]

    def request(self, path, payload=None):
        self.calls.append((path, copy.deepcopy(payload)))
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

    def test_safe_notes_and_bad_missing_or_empty_notes(self):
        self.assertEqual(gate.notes_findings("2026.1117.0", "2026.1124.0", [release()], PATTERN), [])
        for releases in ([], [release(body="")], [dict(release(), draft=True)], [dict(release(), prerelease=True)]):
            with self.assertRaises(ValueError):
                gate.notes_findings("2026.1117.0", "2026.1124.0", releases, PATTERN)
        with self.assertRaises(ValueError):
            gate.notes_findings("v0.23.4", "v2026.9.24", [], r"v0\.\d+\.\d+")

    def invoke(self, api):
        with tempfile.TemporaryDirectory() as temp:
            event = Path(temp) / "event.json"
            event.write_text(json.dumps({"number": 7}))
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

    def test_digest_only_needs_no_release_notes(self):
        api = FakeAPI()
        before = api.contents
        with patch.object(api, "contents", side_effect=lambda repo, sha, file: before(repo, "a" * 40, file)), patch.object(api, "releases", side_effect=AssertionError("digest must not need notes")):
            self.assertEqual(gate.evaluate(api, "fixture/repo", api.pr), [])

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
            if sum("/pulls/" in p for p, _ in api.calls) == 2:
                result["head"]["sha"] = "c" * 40
            return result

        with patch.object(api, "request", side_effect=changed), self.assertRaisesRegex(ValueError, "head changed"):
            self.invoke(api)
        self.assertFalse(any(p and p.get("state") == "success" for _, p in api.calls))


if __name__ == "__main__":
    unittest.main()
