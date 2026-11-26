"""Exercise release selection and asset creation from the actual workflow scripts."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[4]
WORKFLOW = yaml.load((ROOT / ".github/workflows/release.yml").read_text(), Loader=yaml.BaseLoader)
STEPS = WORKFLOW["jobs"]["release"]["steps"]
SELECT = next(step for step in STEPS if step.get("id") == "assets")
UPLOAD = next(step for step in STEPS if step.get("name") == "Attach the kit archive and checksum")


class ReleaseWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git("init", "-q")
        kit = self.root / "hosting/paperclip-hermes/kit"
        kit.mkdir(parents=True)
        (kit / "fixture.txt").write_text("tagged kit\n")
        (self.root / ".release-please-manifest.json").write_text('{".": "1.2.3"}\n')
        self.git("add", ".")
        self.git("-c", "user.name=Test", "-c", "user.email=" + "operator@example.com", "commit", "-qm", "fixture")
        self.git("tag", "v1.2.3")
        self.git("tag", "v1.0.0")
        (kit / "fixture.txt").write_text("newer working tree\n")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        gh = self.bin / "gh"
        gh.write_text("#!" + sys.executable + "\n" + """
import json
import os
from pathlib import Path
import sys

if sys.argv[1] == "api":
    if os.environ.get("API_FAILURE"):
        sys.exit(1)
    release = json.loads(os.environ["FAKE_RELEASE"])
    releases = [{"tag_name": "v8.8.8", "assets": []}]
    if release:
        releases.append(release)
    print(json.dumps(releases))
elif sys.argv[1:3] == ["release", "upload"]:
    with Path("uploads.jsonl").open("a") as stream:
        stream.write(json.dumps(sys.argv[3:]) + "\\n")
else:
    sys.exit("unexpected gh command")
""")
        gh.chmod(0o755)
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
                        GITHUB_OUTPUT=str(self.root / "outputs"), GH_REPO="example/repo",
                        EVENT_NAME="push", REQUESTED_TAG="", NEW_RELEASE_TAG="", RELEASE_CREATED="false")

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True, text=True)

    def select(self, assets=(), tag="v1.2.3", expected=0, **env):
        self.env.update(env)
        self.env["FAKE_RELEASE"] = json.dumps(
            {"tag_name": tag, "assets": [{"name": name} for name in assets]} if tag else None)
        output = self.root / "outputs"
        output.write_text("")
        result = subprocess.run(["bash", "-c", SELECT["run"]], cwd=self.root,
                                env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return output.read_text().removeprefix("tag=").strip()

    def upload(self, tag):
        checkout = next(step for step in STEPS if step.get("with", {}).get("ref", "").startswith("refs/tags/"))
        self.assertEqual(checkout["with"]["ref"], "refs/tags/${{ steps.assets.outputs.tag }}")
        self.assertEqual(checkout["if"], "steps.assets.outputs.tag != ''")
        self.assertEqual(UPLOAD["if"], checkout["if"])
        self.assertEqual(UPLOAD["env"]["RELEASE_TAG"], "${{ steps.assets.outputs.tag }}")
        self.git("checkout", "--force", "--detach", "refs/tags/" + tag)
        subprocess.run(["bash", "-c", UPLOAD["run"]], cwd=self.root,
                       env=dict(self.env, RELEASE_TAG=tag), check=True, capture_output=True)
        asset = "paperclip-hermes-kit-" + tag + ".tar.gz"
        with tarfile.open(self.root / asset) as archive:
            self.assertEqual(archive.extractfile("paperclip-hermes-kit/fixture.txt").read(), b"tagged kit\n")
        subprocess.run(["sha256sum", "-c", asset + ".sha256"], cwd=self.root, check=True, capture_output=True)
        calls = [json.loads(line) for line in (self.root / "uploads.jsonl").read_text().splitlines()]
        self.assertEqual(calls[-1], [tag, asset, asset + ".sha256", "--clobber"])

    def test_new_release_uploads_tagged_archive(self):
        tag = self.select(NEW_RELEASE_TAG="v1.2.3", RELEASE_CREATED="true")
        self.assertEqual(tag, "v1.2.3")
        self.upload(tag)

    def test_dispatch_repairs_existing_release_idempotently(self):
        tag = self.select(tag="v1.0.0", EVENT_NAME="workflow_dispatch", REQUESTED_TAG="v1.0.0",
                          assets=("paperclip-hermes-kit-v1.0.0.tar.gz", "paperclip-hermes-kit-v1.0.0.tar.gz.sha256"))
        self.assertEqual(tag, "v1.0.0")
        self.upload(tag)
        self.upload(tag)
        self.assertEqual(len((self.root / "uploads.jsonl").read_text().splitlines()), 2)
        for step in STEPS:
            if step.get("id") == "release" or "RELEASE_TOKEN" in step.get("env", {}):
                self.assertEqual(step["if"], "github.event_name != 'workflow_dispatch'")

    def test_dispatch_defaults_to_current_version(self):
        self.assertEqual(self.select(EVENT_NAME="workflow_dispatch"), "v1.2.3")

    def test_existing_release_missing_assets_is_repaired(self):
        asset = "paperclip-hermes-kit-v1.2.3.tar.gz"
        for assets in ((), (asset,), (asset + ".sha256",)):
            with self.subTest(assets=assets):
                tag = self.select(assets=assets)
                self.assertEqual(tag, "v1.2.3")
                self.upload(tag)

    def test_complete_existing_release_is_skipped(self):
        asset = "paperclip-hermes-kit-v1.2.3.tar.gz"
        self.assertEqual(self.select(assets=(asset, asset + ".sha256")), "")

    def test_absent_current_release_is_skipped(self):
        self.assertEqual(self.select(tag=None), "")

    def test_dispatch_requires_an_existing_release(self):
        self.select(tag=None, EVENT_NAME="workflow_dispatch", expected=1)

    def test_release_api_failure_fails_the_job(self):
        self.select(API_FAILURE="true", expected=1)

    def test_invalid_dispatch_tag_is_rejected(self):
        self.select(EVENT_NAME="workflow_dispatch", REQUESTED_TAG="../unsafe", expected=1)


if __name__ == "__main__":
    unittest.main()
