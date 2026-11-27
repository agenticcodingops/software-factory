"""First-observation timestamps and atomic API publishing for the Paperclip index."""

import base64
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import importlib.util
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

SPEC = importlib.util.spec_from_file_location("index", Path(__file__).with_name("paperclip-index.py"))
index = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(index)
NOW = "2026-11-27T07:23:00Z"
EARLIER = "2026-11-20T07:23:00Z"
DIGEST = "sha256:" + "a" * 64
RELEASE = {"tag_name":"v2026.1117.0"}


def missing():
    return HTTPError("https://fixture.invalid/missing", 404, "missing", {}, None)


def encoded(result):
    return (json.dumps(result, indent=2, sort_keys=True) + "\n").encode()


class FakeAPI:
    def __init__(self, previous=None, branch=True):
        self.previous = previous
        self.branch = branch
        self.calls = []
        self.resolved = []
        self.error = None
        self.release_data = [RELEASE]
        self.unrelated_package_versions = 1000  # Deliberately irrelevant to release enumeration.

    def request(self, path, payload=None, method=None):
        self.calls.append((path, payload, method))
        if "/packages/" in path:
            raise AssertionError("the bounded index must never list packages")
        if path.endswith("?ref=renovate-data"):
            if self.previous is None:
                raise missing()
            return {"content":base64.b64encode(encoded(self.previous)).decode(), "sha":"previous-blob-placeholder"}
        if path.endswith("/git/ref/heads/renovate-data"):
            if not self.branch:
                raise missing()
            return {"object":{"sha":"c" * 40}}
        if path == "repos/fixture/repo":
            return {"default_branch":"main"}
        if path.endswith("/git/ref/heads/main"):
            return {"object":{"sha":"b" * 40}}
        if "/releases?" in path:
            return self.release_data
        if payload is not None:
            return {}
        raise AssertionError(path)

    releases = index.gate.GitHub.releases

    def registry_digest(self, image, tag):
        self.resolved.append((image, tag))
        if self.error:
            raise self.error
        return DIGEST


class IndexTests(unittest.TestCase):
    def test_both_forms_one_form_and_unresolved(self):
        for forms in (("2026.1117.0", "v2026.1117.0"), ("2026.1117.0",), ("v2026.1117.0",)):
            def resolve(image, tag):
                self.assertEqual(image, index.IMAGE)
                if tag not in forms:
                    raise missing()
                return DIGEST
            result = index.build([RELEASE], {}, resolve, NOW)
            self.assertEqual([e["version"] for e in result["releases"]], sorted(forms))
            self.assertTrue(all(e["releaseTimestamp"] == NOW for e in result["releases"]))
        def one_release(image, tag):
            if "1118" in tag:
                raise missing()
            return DIGEST
        result = index.build([RELEASE, {"tag_name":"2026.1118.0"}], {}, one_release, NOW)
        self.assertEqual(len(result["releases"]), 2)

    def test_nonstable_releases_never_resolve(self):
        skipped = [{"tag_name":tag} for tag in ("nightly", "canary", "2026.1117.0-beta", "sha-abcdef", "v0.23.4")]
        skipped += [dict(RELEASE, draft=True), dict(RELEASE, prerelease=True)]
        calls = []
        def resolve(image, tag):
            calls.append(tag)
            return DIGEST
        index.build(skipped + [RELEASE], {}, resolve, NOW)
        self.assertEqual(calls, ["2026.1117.0", "v2026.1117.0"])

    def test_timestamp_kept_for_same_digest_and_restarted_for_repush(self):
        previous = index.build([RELEASE], {}, lambda *_: DIGEST, EARLIER)
        unchanged = index.build([RELEASE], previous, lambda *_: DIGEST, NOW)
        self.assertEqual(unchanged, previous)
        changed = index.build([RELEASE], previous, lambda *_: "sha256:" + "b" * 64, NOW)
        self.assertTrue(all(e["releaseTimestamp"] == NOW for e in changed["releases"]))

    def test_builder_never_lists_packages_even_beyond_100_versions(self):
        api = FakeAPI()
        index.publish(api, "fixture/repo", NOW)
        self.assertIn(("repos/paperclipai/paperclip/releases?per_page=100&page=1", None, None), api.calls)
        self.assertTrue(api.resolved)
        self.assertFalse(any("/packages/" in path for path, _, _ in api.calls))

    def test_resolver_errors_abort_before_any_write(self):
        for error in (OSError("offline"), HTTPError("https://fixture.invalid", 500, "unavailable", {}, None), ValueError("redirect")):
            api = FakeAPI()
            api.error = error
            with self.assertRaises(type(error)):
                index.publish(api, "fixture/repo", NOW)
            self.assertTrue(all(payload is None for _, payload, _ in api.calls))
        api = FakeAPI()
        with patch.object(api, "registry_digest", side_effect=[DIGEST, OSError("second form failed")]), self.assertRaises(OSError):
            index.publish(api, "fixture/repo", NOW)
        self.assertTrue(all(payload is None for _, payload, _ in api.calls))

    def test_empty_result_exits_nonzero_and_keeps_previous(self):
        api = FakeAPI(index.build([RELEASE], {}, lambda *_: DIGEST, EARLIER))
        api.error = missing()
        with patch.dict(os.environ, {"GH_TOKEN":"github-placeholder", "GITHUB_REPOSITORY":"fixture/repo"}), patch.object(index.gate, "GitHub", return_value=api), redirect_stderr(StringIO()), redirect_stdout(StringIO()):
            self.assertEqual(index.main(), 1)
        self.assertTrue(all(payload is None for _, payload, _ in api.calls))

    def test_unchanged_content_performs_no_put(self):
        previous = index.build([RELEASE], {}, lambda *_: DIGEST, EARLIER)
        api = FakeAPI(previous)
        self.assertFalse(index.publish(api, "fixture/repo", NOW))
        self.assertTrue(all(payload is None for _, payload, _ in api.calls))

    def test_missing_branch_created_before_put(self):
        api = FakeAPI(branch=False)
        self.assertTrue(index.publish(api, "fixture/repo", NOW))
        writes = [(path, payload, method) for path, payload, method in api.calls if payload is not None]
        self.assertEqual(writes[0], ("repos/fixture/repo/git/refs", {"ref":"refs/heads/renovate-data", "sha":"b" * 40}, None))
        self.assertEqual(writes[1][0], "repos/fixture/repo/contents/paperclip-releases.json")
        self.assertEqual(writes[1][2], "PUT")
        self.assertEqual(writes[1][1]["branch"], "renovate-data")
        self.assertNotIn("sha", writes[1][1])
        self.assertEqual(json.loads(base64.b64decode(writes[1][1]["content"])), index.build([RELEASE], {}, lambda *_: DIGEST, NOW))

    def test_update_uses_previous_blob_and_errors_do_not_create_branch(self):
        api = FakeAPI(index.build([RELEASE], {}, lambda *_: "sha256:" + "b" * 64, EARLIER))
        self.assertTrue(index.publish(api, "fixture/repo", NOW))
        self.assertEqual(api.calls[-1][1]["sha"], "previous-blob-placeholder")
        api = FakeAPI()
        with patch.object(api, "request", side_effect=HTTPError("https://fixture.invalid", 403, "forbidden", {}, None)), self.assertRaises(HTTPError):
            index.publish(api, "fixture/repo", NOW)
        self.assertEqual(api.calls, [])

    def test_index_registry_requests_never_use_github_token(self):
        from io import BytesIO
        registry_requests = []
        def open_request(request, timeout):
            registry_requests.append(request)
            response = BytesIO(b'{"token":"registry-placeholder"}')
            response.status = 200
            response.headers = {"Docker-Content-Digest":DIGEST}
            return response
        with patch.object(index.gate, "build_opener") as builder:
            builder.return_value.open.side_effect = open_request
            index.build([RELEASE], {}, index.gate.GitHub("github-placeholder").registry_digest, NOW)
        self.assertEqual(len(registry_requests), 4)
        for request in registry_requests:
            self.assertTrue(request.full_url.startswith("https://ghcr.io/"))
            self.assertNotIn("github-placeholder", str(request.header_items()))


if __name__ == "__main__":
    unittest.main()
