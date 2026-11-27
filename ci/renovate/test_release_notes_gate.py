"""The trusted-default-branch notes gate fails closed and labels the current PR head."""

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
    def __init__(self, body="Routine fixes.", image="paperclip"):
        self.body = body
        self.image = image
        self.files = []
        self.base_sha = "a" * 40
        self.calls = []
        self.pr = {"user": {"login": "renovate[bot]"}, "base": {"sha": "d" * 40, "ref": "main"},
                   "head": {"sha": "b" * 40, "repo": {"full_name": "fixture/repo"}}}

    def contents(self, repo, sha, path):
        image = next(image for image, (_, _, file, _) in gate.IMAGES.items() if file == path)
        tag = "2026.1117.0" if sha == self.base_sha or self.image != "paperclip" else "2026.1124.0"
        digest = "c" * 64 if sha == self.base_sha else "d" * 64
        if "Dockerfile" in path:
            tag = "v0.23.4" if sha == self.base_sha or self.image != "hermes" else "v0.23.5"
            return f"# base\nFROM {image}:{tag}@sha256:{digest} AS runtime\nRUN true \\\n    && true\nFROM runtime AS sandbox\n"
        return ("services:\n  postgres:\n    image: postgres:17@sha256:" + "e" * 64 + "\n"
                f"  paperclip:\n    <<: *runtime\n    image: {image}:{tag}@sha256:{digest}\n"
                "  hermes:\n    image: software-factory-hermes:${KIT_IMAGE_TAG:-local}\n")

    def registry_digest(self, image, tag):
        return "sha256:" + "d" * 64

    def releases(self, repo):
        return [release("v0.23.5" if self.image == "hermes" else "v2026.1124.0", self.body)]

    workflow_changes = gate.GitHub.workflow_changes

    def request(self, path, payload=None):
        self.calls.append((path, copy.deepcopy(payload)))
        if "/files?" in path:
            page = int(path.rsplit("=", 1)[1])
            return self.files[(page - 1) * 100:page * 100]
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
        self.assertEqual(self.invoke(api), 0)
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
            self.assertEqual(self.invoke(api), 0)
        self.assertEqual([p["state"] for _, p in api.calls if p and "state" in p], ["pending"])

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


if __name__ == "__main__":
    unittest.main()


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
            self.docker() + "FROM evil:1\n",
            "\t" + self.docker(),
            self.docker() + self.docker(),
        ]
        for text in variants:
            with self.subTest(text=text), self.assertRaises(ValueError):
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
        for text in variants:
            with self.subTest(text=text), self.assertRaises(ValueError):
                gate.compose_pin(text, self.paper, "paperclip")

    def test_non_ascii_and_document_markers_fail(self):
        for text in (self.compose() + "\r", self.compose() + "\ufeff", "---\n" + self.compose()):
            with self.assertRaises(ValueError):
                gate.compose_pin(text, self.paper, "paperclip")


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

    def test_redirects_are_refused(self):
        with self.assertRaises(ValueError):
            gate.NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://other.example/")
