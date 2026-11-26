#!/usr/bin/env python3
"""Read upstream notes for every tracked image upgrade; never execute PR content."""

import base64
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

IMAGES = {
    "ghcr.io/paperclipai/paperclip": (
        "paperclipai/paperclip", r"v?\d{4}\.\d{3,4}\.\d+",
        "hosting/paperclip-hermes/kit/compose.yaml",
    ),
    "nousresearch/hermes-agent": (
        "NousResearch/hermes-agent", r"v0\.\d+\.\d+",
        "hosting/paperclip-hermes/kit/Dockerfile.hermes",
    ),
}
HAZARD = re.compile(r"breaking|migration|rollback[\s-]+floor", re.IGNORECASE)


def version(tag, pattern):
    if not re.fullmatch(pattern, tag):
        raise ValueError("unrecognized image release tag")
    return tuple(map(int, tag.removeprefix("v").split(".")))


def notes_findings(old, new, releases, pattern):
    """Every stable release in (old, new] is checked, including skipped versions."""
    lower, upper = version(old, pattern), version(new, pattern)
    if upper <= lower:
        raise ValueError("image version is not an upgrade")
    found_target = False
    findings = []
    for release in releases:
        tag = release["tag_name"]
        if release.get("draft") or release.get("prerelease") or not re.fullmatch(pattern, tag):
            continue
        if lower < version(tag, pattern) <= upper:
            body = release.get("body") or ""
            if not body.strip():
                raise ValueError("upstream release notes are empty")
            if version(tag, pattern) == upper:
                found_target = True
            match = HAZARD.search(body)
            if match:
                findings.append(f"{tag}: {match.group(0)}")
    if not found_target:
        raise ValueError("upstream target release notes are missing")
    return findings


class GitHub:
    def __init__(self, token):
        self.token = token

    def request(self, path, payload=None):
        request = Request(
            "https://api.github.com/" + path,
            headers={"Authorization": "Bearer " + self.token,
                     "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
            data=None if payload is None else json.dumps(payload).encode(),
        )
        with urlopen(request, timeout=30) as response:
            return json.load(response)

    def contents(self, repo, sha, path):
        result = self.request(f"repos/{repo}/contents/{path}?ref={quote(sha, safe='')}")
        return base64.b64decode(result["content"], validate=False).decode()

    def releases(self, repo):
        # Read all pages so an intermediate migration cannot hide behind newer releases.
        for page in range(1, 101):
            batch = self.request(f"repos/{repo}/releases?per_page=100&page={page}")
            yield from batch
            if len(batch) < 100:
                return
        raise ValueError("upstream release pagination limit reached")

    def workflow_changes(self, repo, number):
        # GitHub returns at most 3,000 PR files. Never accept a truncated diff.
        for page in range(1, 31):
            files = self.request(f"repos/{repo}/pulls/{number}/files?per_page=100&page={page}")
            for file in files:
                if any(file.get(key, "").startswith(".github/workflows/")
                       for key in ("filename", "previous_filename")):
                    return True
            if len(files) < 100:
                return False
        raise ValueError("PR files pagination limit reached")


def evaluate(api, repo, pr, base_sha):
    findings = []
    for image, (source, pattern, path) in IMAGES.items():
        before = api.contents(repo, base_sha, path)
        after = api.contents(pr["head"]["repo"]["full_name"], pr["head"]["sha"], path)
        pins = re.compile(re.escape(image) + r":([^@\s]+)@sha256:[a-f0-9]{64}\b")
        old, new = pins.findall(before), pins.findall(after)
        if len(old) != 1 or len(new) != 1:
            raise ValueError("expected exactly one digest-pinned image per source file")
        if old != new:
            findings.extend(f"{image} {item}" for item in notes_findings(old[0], new[0], api.releases(source), pattern))
    return findings


def main():
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    default_branch = event["repository"]["default_branch"]
    # Required-workflow rules ignore trigger filters; guard both event and live PR.
    if event["pull_request"]["base"]["ref"] != default_branch:
        return 0
    repo = os.environ["GITHUB_REPOSITORY"]
    number = event["number"]
    api = GitHub(os.environ["GH_TOKEN"])
    pr = api.request(f"repos/{repo}/pulls/{number}")
    if pr["base"]["ref"] != default_branch:
        return 0
    sha = pr["head"]["sha"]
    status_path = f"repos/{repo}/statuses/{sha}"
    status = {"context": "release-notes-gate", "state": "pending", "description": "Checking upstream image release notes"}
    api.request(status_path, status)
    try:
        base_path = f"repos/{repo}/git/ref/heads/{quote(default_branch, safe='')}"
        base_sha = api.request(base_path)["object"]["sha"]
        findings = evaluate(api, repo, pr, base_sha)
        if api.workflow_changes(repo, number):
            findings.append("Workflow changes require the operator")
        if api.request(base_path)["object"]["sha"] != base_sha:
            raise ValueError("default branch changed while checking notes; rerun the gate")
    except Exception as error:  # Any API, parse or missing-notes failure closes the gate.
        print(f"Release notes unavailable: {type(error).__name__}")
        findings = ["Upstream notes could not be verified"]
    latest = api.request(f"repos/{repo}/pulls/{number}")
    if latest["base"]["ref"] != default_branch:
        return 0
    if latest["head"]["sha"] != sha:
        raise ValueError("PR head changed while checking notes; rerun the gate")
    if findings:
        api.request(f"repos/{repo}/issues/{number}/labels", {"labels": ["needs-operator"]})
    status.update(state="failure" if findings else "success",
                  description="Operator required: workflow change, release risk or unavailable notes" if findings else "Upstream release notes passed")
    api.request(status_path, status)
    # Avoid emitting upstream text into workflow command processing.
    print(f"release-notes-gate: {'needs-operator' if findings else 'passed'} ({len(findings)} finding(s))")
    return int(bool(findings))


if __name__ == "__main__":
    sys.exit(main())
