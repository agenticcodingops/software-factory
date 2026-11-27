#!/usr/bin/env python3
"""Read upstream notes for every tracked image upgrade; never execute PR content."""

import base64
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener

IMAGES = {
    "ghcr.io/paperclipai/paperclip": (
        "paperclipai/paperclip", r"v?\d{4}\.\d{3,4}\.\d+",
        "hosting/paperclip-hermes/kit/compose.yaml", ("compose", "paperclip"),
    ),
    "nousresearch/hermes-agent": (
        "NousResearch/hermes-agent", r"v0\.\d+\.\d+",
        "hosting/paperclip-hermes/kit/Dockerfile.hermes", ("dockerfile", None),
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


PROTECTED_PREFIXES = (".github/workflows/", "ci/renovate/release-notes-gate", "hosting/paperclip-hermes/rulesets/")
PROTECTED_FILES = ("CODEOWNERS",)


TAG = r"[A-Za-z0-9_][A-Za-z0-9._-]*"
DIGEST = r"sha256:[a-f0-9]{64}"


def ascii_only(text):
    if re.search(r"[^\n\x20-\x7e]", text):
        raise ValueError("image source must contain printable ASCII and LF only")


def unique_pin(pins):
    if len(pins) != 1:
        raise ValueError("expected exactly one digest-pinned image per source file")
    return pins[0]


def dockerfile_pin(text, image):
    # Strict physical FROM lines; no continuation/parser directives are interpreted.
    # https://docs.docker.com/reference/dockerfile/
    ascii_only(text)
    stages, pins = set(), []
    for raw in text.split("\n"):
        line = raw.lstrip(" ")
        if line.startswith("#"):
            if re.match(r"(?i)#\s*(syntax|escape)\s*=", line):
                raise ValueError("Dockerfile parser directives require the operator")
            continue
        if re.fullmatch(r"(?i)(f|fr|fro)\\[ ]*", line):
            raise ValueError("split FROM keyword")
        if re.match(r"(?i)from(?![A-Za-z0-9_])", line):
            match = re.fullmatch(r"(?i:from)[ ]+([A-Za-z0-9._/:@-]+)(?:[ ]+(?i:as)[ ]+([A-Za-z0-9][A-Za-z0-9._-]*))?[ ]*", line)
            if not match:
                raise ValueError("unsupported FROM syntax")
            reference, stage = match.groups()
            pin = re.fullmatch(re.escape(image) + f":({TAG})@({DIGEST})", reference)
            if pin:
                pins.append(pin.groups())
            elif reference.lower() not in stages:
                raise ValueError("unrecognized FROM reference")
            if stage:
                stages.add(stage.lower())
    return unique_pin(pins)


def compose_pin(text, image, service):
    # One canonical physical image line, not a YAML parser. Reject escapes and indirection.
    # https://yaml.org/spec/1.2.2/#double-quoted-style
    # https://docs.docker.com/reference/compose-file/interpolation/
    ascii_only(text)
    top, current = None, None
    services, image_lines, pins = set(), set(), []
    for line in text.split("\n"):
        if not line.strip() or line.lstrip(" ").startswith("#"):
            continue
        if "\\" in line:
            raise ValueError("compose backslashes are unsupported")
        if not line.startswith(" "):
            if line.startswith(("---", "...")):
                raise ValueError("multiple compose documents are unsupported")
            top, current = line, None
        elif re.match(r"^  [^ ]", line):
            current = line
            if top == "services:":
                if current in services:
                    raise ValueError("duplicate compose service")
                services.add(current)
        if re.search(r"(?:^|[\s{,\[])[\"']?(?:extends|include)[\"']?\s*:", line):
            raise ValueError("compose includes and extends are unsupported")
        if re.search(r"(?:^|[\s{,\[])[\"']?image[\"']?\s*:", line) or image in line:
            canonical = re.fullmatch(r"    image: ([a-z0-9._/-]+)(?::(" + TAG + r"))?@(" + DIGEST + r")", line)
            local = re.fullmatch(r"    image: [a-z0-9][a-z0-9._-]*:\$\{[A-Z][A-Z0-9_]*:-[a-z0-9][a-z0-9._-]*\}", line)
            if not canonical and not (local and top == "services:" and current and current != "  " + service + ":"):
                raise ValueError("unsupported image syntax")
            key = (top, current)
            if key in image_lines:
                raise ValueError("duplicate image line")
            image_lines.add(key)
            if canonical and canonical[1] == image:
                if not canonical[2] or top != "services:" or current != "  " + service + ":":
                    raise ValueError("tracked image must be tagged under its canonical service")
                pins.append((canonical[2], canonical[3]))
    return unique_pin(pins)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("redirects are forbidden")


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
        with build_opener(NoRedirect()).open(request, timeout=30) as response:
            if response.status != 200:
                raise ValueError("unexpected GitHub status")
            return json.load(response)

    def registry_digest(self, image, tag):
        # Anonymous registry credentials only; GH_TOKEN is confined to api.github.com.
        # https://docs.docker.com/reference/api/registry/auth/
        # https://raw.githubusercontent.com/opencontainers/distribution-spec/main/spec.md
        if image.startswith("ghcr.io/"):
            name = image.removeprefix("ghcr.io/")
            token_url = f"https://ghcr.io/token?service=ghcr.io&scope=repository:{name}:pull"
            registry = "https://ghcr.io"
        else:
            name = image
            token_url = f"https://auth.docker.io/token?service=registry.docker.io&scope=repository:{name}:pull"
            registry = "https://registry-1.docker.io"
        opener = build_opener(NoRedirect())
        with opener.open(Request(token_url), timeout=30) as response:
            if response.status != 200:
                raise ValueError("registry token lookup failed")
            token = json.load(response)["token"]
        request = Request(f"{registry}/v2/{name}/manifests/{quote(tag, safe='')}", method="HEAD", headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json",
        })
        with opener.open(request, timeout=30) as response:
            digest = response.headers.get("Docker-Content-Digest", "")
            if response.status != 200 or not re.fullmatch(DIGEST, digest):
                raise ValueError("registry manifest digest unavailable")
            return digest

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

    def protected_changes(self, repo, base_sha, head_sha):
        # Immutable SHAs prevent a PR push from changing this answer.
        # Files appear only on page 1 and are capped at 300:
        # https://docs.github.com/en/rest/commits/commits#compare-two-commits
        basehead = f"{quote(base_sha, safe='')}...{quote(head_sha, safe='')}"
        files = self.request(f"repos/{repo}/compare/{basehead}?per_page=1&page=1")["files"]
        if len(files) >= 300:
            raise ValueError("comparison file list may be truncated")
        return any(name in PROTECTED_FILES or name.startswith(PROTECTED_PREFIXES)
                   for file in files for name in (file.get("filename", ""), file.get("previous_filename", "")))


def evaluate(api, repo, pr, base_sha):
    findings = []
    for image, (source, pattern, path, extractor) in IMAGES.items():
        before = api.contents(repo, base_sha, path)
        after = api.contents(pr["head"]["repo"]["full_name"], pr["head"]["sha"], path)
        kind, service = extractor
        extract = (lambda text: compose_pin(text, image, service)) if kind == "compose" else (lambda text: dockerfile_pin(text, image))
        old_tag, old_digest = extract(before)
        new_tag, new_digest = extract(after)
        if (old_tag, old_digest) == (new_tag, new_digest):
            continue
        if api.registry_digest(image, new_tag) != new_digest:
            raise ValueError("pinned digest does not match the tag")
        if old_tag != new_tag:
            findings.extend(f"{image} {item}" for item in notes_findings(old_tag, new_tag, api.releases(source), pattern))
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
        if api.protected_changes(repo, base_sha, sha):
            findings.append("Gate, workflow, ruleset or CODEOWNERS changes require the operator")
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
                  description="Operator required: gate, workflow, ruleset, CODEOWNERS change, release risk or unavailable data" if findings else "Upstream release notes passed")
    api.request(status_path, status)
    # Avoid emitting upstream text into workflow command processing.
    print(f"release-notes-gate: {'needs-operator' if findings else 'passed'} ({len(findings)} finding(s))")
    return int(bool(findings))


if __name__ == "__main__":
    sys.exit(main())
