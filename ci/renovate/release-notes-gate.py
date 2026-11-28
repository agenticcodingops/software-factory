#!/usr/bin/env python3
"""Read upstream notes for every tracked image upgrade; never execute PR content."""

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
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
HAZARD = re.compile(r"breaking|migrat\w*|rollback[\s-]+floor", re.IGNORECASE)

DEFERRED = re.compile(r"notes\b[^.\n]{0,80}\b(?:ship|follow|land|come|publish\w*)\b[^.\n]{0,20}\b(?:with|in)\s+v?\d|undocumented here", re.IGNORECASE)


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
            if DEFERRED.search(body):
                findings.append(f"{tag}: notes deferred")
    if not found_target:
        raise ValueError("upstream target release notes are missing")
    return findings


PROTECTED_PREFIXES = (".github/workflows/", "ci/renovate/release-notes-gate", "hosting/paperclip-hermes/rulesets/")
PROTECTED_FILES = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS", ".github/workflows/release-notes-gate.yml")

ACTION_PIN = re.compile(r"( *(?:- +)?uses: +)([A-Za-z0-9_-][A-Za-z0-9_.-]*/[A-Za-z0-9_.-]+(?:/[^@ ]+)?)@([0-9a-f]{40})( +# +)(v[0-9]+\.[0-9]+\.[0-9]+)")
WORKFLOW_FINDING = "Workflow changes other than verified action pin bumps require the operator"


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
    # DetectSyntax also falls back to // comments and skips a first-line shebang:
    # https://raw.githubusercontent.com/moby/buildkit/master/frontend/dockerfile/parser/directives.go
    # https://docs.docker.com/reference/dockerfile/
    ascii_only(text)
    stages, pins = set(), []
    for raw in text.split("\n"):
        line = raw.lstrip(" ")
        if re.match(r"(?i)(#|//)\s*(syntax|escape|check)\s*=", line):
            raise ValueError("Dockerfile parser directives require the operator")
        if line.startswith("#"):
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


def flow_scan(line):
    """Reject physical lines that can hide later YAML nodes in scalar/flow content."""
    content = line.lstrip(" ")
    start, stack, dollar = True, [], 0
    index = 0

    def reject():
        raise ValueError("compose multiline quotes and flow collections are unsupported")

    while index < len(content):
        ch = content[index]
        nxt = content[index + 1:index + 2]
        if ch == " ":
            pass
        elif ch == "#" and (index == 0 or content[index - 1] == " "):
            break
        elif ch in "\"'":
            if not start:
                reject()
            closing = content.find(ch, index + 1)
            if closing == -1:
                reject()
            tail = content[closing + 1:]
            if not (not tail or tail.startswith(" ") or tail == ":" or
                    tail.startswith(": ") or (stack and tail[0] in ",]}")):
                reject()
            index, start = closing, False
        elif ch in "[{":
            if start:
                stack.append(ch)
            elif ch == "{" and not stack and content[index - 1] == "$":
                dollar += 1
            else:
                reject()
        elif ch in "]}":
            if stack:
                opening = stack.pop()
                if (opening, ch) not in (("[", "]"), ("{", "}")):
                    reject()
                start = False
            elif ch == "}" and dollar:
                dollar -= 1
            else:
                reject()
        elif ch == "," and stack:
            start = True
        elif ch == ":" and nxt in ("", " "):
            start = True
        elif ch == "-" and nxt in ("", " ") and start and not stack:
            pass
        elif ch == "?" and nxt in ("", " ") and start:
            raise ValueError("compose explicit and alias keys are unsupported")
        elif ch == "!" and start:
            raise ValueError("compose YAML tags are unsupported")
        elif ch == "&" and start:
            end = content.find(" ", index)
            rest = "" if end == -1 else content[end:].lstrip(" ")
            if rest and not rest.startswith("#"):
                reject()
            break
        else:
            start = False
        index += 1
    if stack or dollar:
        reject()


def compose_pin(text, image, service):
    # One canonical physical image line, not a YAML parser. Reject escapes and indirection.
    # https://yaml.org/spec/1.2.2/#double-quoted-style
    # https://docs.docker.com/reference/compose-file/interpolation/
    ascii_only(text)
    top, current = None, None
    services, image_lines, pins = set(), set(), []
    pending_indent = None
    for line in text.split("\n"):
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        # YAML folds deeper lines into a preceding plain scalar, even across blanks.
        # Only empty block keys/dashes (and a key's bare anchor) may introduce children.
        if pending_indent is not None and indent > pending_indent:
            raise ValueError("compose multi-line plain scalars are unsupported")
        if line.lstrip(" ").startswith("#"):
            continue
        # Block scalars are scalar content the scanner cannot see into; give them their own error.
        # https://yaml.org/spec/1.2.2/#81-block-scalar-styles
        if re.fullmatch(r" *(?:- +)*(?:[^ #][^#]*?: +)?[|>][0-9+-]{0,2} *(?:(?<= )#.*)?", line):
            raise ValueError("compose block scalars are unsupported")
        # "#" starts a comment only after whitespace: https://yaml.org/spec/1.2.2/#66-comments
        container = re.fullmatch(r" *(?:(?:- +)?[A-Za-z0-9_.-]+: *(?:&[A-Za-z0-9_-]+ *)?|- *)(?:(?<= )#.*)?", line)
        # A sequence mapping's key starts after "- "; its next key is a sibling.
        key_indent = indent + 2 if re.match(r" *- [A-Za-z0-9_.-]+: ", line) else indent
        pending_indent = None if container else key_indent
        if "\\" in line:
            raise ValueError("compose backslashes are unsupported")
        indent = len(line) - len(line.lstrip(" "))
        if indent in (1, 3):
            raise ValueError("compose indentation requires the operator")
        if line.lstrip(" ").startswith(("?", "*")) or re.search(r"\*[A-Za-z0-9_-]+\s*:", line):
            raise ValueError("compose explicit and alias keys are unsupported")
        flow_scan(line)
        if "<<" in line and line != "    <<: *runtime":
            raise ValueError("compose merge must be the canonical runtime alias")
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
        # https://docs.docker.com/reference/compose-file/services/#env_file
        # https://docs.docker.com/reference/compose-file/services/#label_file
        if re.search(r"(?:^|[\s{,\[])[\"']?(?:env_file|label_file)[\"']?\s*:", line):
            raise ValueError("compose env_file and label_file are unsupported")
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


COMPOSE_TIMEOUT = 60
COMPOSE_VALUES = {
    "TAILSCALE_HOSTNAME": "pilot.tailnet.example",
    "FIXTURE_REPOSITORY": "owner/fixture-repository",
    "KIT_IMAGE_TAG": "local",
    "DASHBOARD_SUBNET": "172.30.99.0/24",
    "DASHBOARD_GATEWAY": "172.30.99.1",
    "HERMES_MODEL": "fixture-model",
    "KIT_MESSAGING_OFF": "",
}


def confirm_compose_pin(text, image, service, pin):
    # ci/install-tools.sh installs the checksum-verified standalone docker-compose
    # on PATH in both workflows. Never fall back to the runner's Docker CLI plugin.
    # config resolves the paperclip image the registry/notes checks describe:
    # https://docs.docker.com/reference/cli/docker/compose/config/
    # File-reading env_file/label_file keys are rejected before invoking Compose.
    # This does not check other services, Dockerfiles or override files.
    binary = shutil.which("docker-compose")
    if binary is None:
        raise ValueError("verified Compose is unavailable")

    def no_constant(value):
        raise ValueError("non-JSON Compose constant")

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate Compose JSON key")
            result[key] = value
        return result

    with tempfile.TemporaryDirectory(prefix="compose-gate-") as directory:
        root = Path(directory)
        (root / "compose.yaml").write_bytes(text.encode("utf-8"))
        config = root / "docker-config"
        config.mkdir()
        # Construct from scratch: only PATH and fixed fixture values survive.
        # The private cwd has no .env/override; HOME and Docker credentials are empty.
        env = {"PATH": os.environ.get("PATH", os.defpath), "HOME": directory,
               "DOCKER_CONFIG": str(config), **COMPOSE_VALUES}
        run = subprocess.run(
            [binary, "-f", "compose.yaml", "--profile", "*", "config", "--format", "json"],
            cwd=directory, env=env, capture_output=True, text=True,
            timeout=COMPOSE_TIMEOUT, check=True,
        )
        model = json.loads(run.stdout, parse_constant=no_constant, object_pairs_hook=unique_object)
        services = model.get("services") if isinstance(model, dict) else None
        resolved = services.get(service) if isinstance(services, dict) else None
        actual = resolved.get("image") if isinstance(resolved, dict) else None
        if not isinstance(actual, str) or actual != f"{image}:{pin[0]}@{pin[1]}":
            raise ValueError("Compose service image differs from the checked pin")


def checked_compose_pin(text, image, service):
    pin = compose_pin(text, image, service)
    confirm_compose_pin(text, image, service, pin)
    return pin


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("redirects are forbidden")


class GitHub:
    def __init__(self, token):
        self.token = token

    def request(self, path, payload=None, method=None):
        request = Request(
            "https://api.github.com/" + path,
            headers={"Authorization": "Bearer " + self.token,
                     "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
            data=None if payload is None else json.dumps(payload).encode(), method=method,
        )
        with build_opener(NoRedirect()).open(request, timeout=30) as response:
            expected = (200,) if request.get_method() == "GET" else (200, 201)
            if response.status not in expected:
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
        comparison = self.request(f"repos/{repo}/compare/{basehead}?per_page=1&page=1")
        files = comparison["files"]
        if len(files) >= 300:
            raise ValueError("comparison file list may be truncated")
        workflows = []
        for file in files:
            names = (file.get("filename", ""), file.get("previous_filename", ""))
            if any(name in PROTECTED_FILES or name.startswith(PROTECTED_PREFIXES[1:]) for name in names):
                return ["Gate, workflow, ruleset or CODEOWNERS changes require the operator"]
            if any(name.startswith(".github/workflows/") for name in names):
                workflows.append(file)
        if len(workflows) > 20 or any(file["status"] != "modified" for file in workflows):
            return [WORKFLOW_FINDING]
        if not workflows:
            return []
        merge_base = comparison["merge_base_commit"]["sha"]
        changed = []
        for file in workflows:
            old_text = self.contents(repo, merge_base, file["filename"])
            new_text = self.contents(repo, head_sha, file["filename"])
            if any(re.search(r"[^\n\x20-\x7e]", text) for text in (old_text, new_text)):
                return [WORKFLOW_FINDING]
            old, new = old_text.split("\n"), new_text.split("\n")
            if len(old) != len(new):
                return [WORKFLOW_FINDING]
            differences = [(a, b) for a, b in zip(old, new) if a != b]
            if not differences:
                return [WORKFLOW_FINDING]
            changed.extend(differences)
            if len(changed) > 50:
                return [WORKFLOW_FINDING]
        # Workflow files are printable ASCII with LF endings. Changed lines must be
        # byte-identical except for the 40-hex SHA and version. This proves
        # same-action, same-major pin bumps against tags at gate time,
        # not the safety of upstream action code. Only two reviews remain the other
        # control; no release-age soak applies to Actions tags. Ref/tag API contracts:
        # https://docs.github.com/en/rest/git/refs
        # https://docs.github.com/en/rest/git/tags
        for old, new in changed:
            before, after = ACTION_PIN.fullmatch(old), ACTION_PIN.fullmatch(new)
            if not before or not after or (before[1], before[2], before[4]) != (after[1], after[2], after[4]):
                return [WORKFLOW_FINDING]
            upstream = "/".join(after[2].split("/")[:2])
            lower = tuple(map(int, before[5][1:].split(".")))
            upper = tuple(map(int, after[5][1:].split(".")))
            if upstream.lower() == repo.lower() or upper[0] != lower[0] or upper <= lower:
                return [WORKFLOW_FINDING]
            obj = self.request(f"repos/{upstream}/git/ref/tags/{after[5]}")["object"]
            for _ in range(5):
                if obj["type"] != "tag":
                    break
                tag_sha = obj["sha"]
                if not re.fullmatch(r"[0-9a-f]{40}", tag_sha):
                    raise ValueError("invalid upstream tag object")
                obj = self.request(f"repos/{upstream}/git/tags/{tag_sha}")["object"]
            if obj["type"] != "commit" or obj["sha"] != after[3]:
                return [WORKFLOW_FINDING]
        return []


def evaluate(api, repo, pr, base_sha):
    findings = []
    for image, (source, pattern, path, extractor) in IMAGES.items():
        before = api.contents(repo, base_sha, path)
        after = api.contents(pr["head"]["repo"]["full_name"], pr["head"]["sha"], path)
        kind, service = extractor
        extract = (lambda text: checked_compose_pin(text, image, service)) if kind == "compose" else (lambda text: dockerfile_pin(text, image))
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
        print("release-notes-gate: PR base changed during the run; reopen the PR or push a commit to rerun")
        return 1
    sha = pr["head"]["sha"]
    status_path = f"repos/{repo}/statuses/{sha}"
    status = {"context": "release-notes-gate", "state": "pending", "description": "Checking upstream image release notes"}
    api.request(status_path, status)
    try:
        base_path = f"repos/{repo}/git/ref/heads/{quote(default_branch, safe='')}"
        base_sha = api.request(base_path)["object"]["sha"]
        findings = evaluate(api, repo, pr, base_sha)
        findings.extend(api.protected_changes(repo, base_sha, sha))
        if api.request(base_path)["object"]["sha"] != base_sha:
            raise ValueError("default branch changed while checking notes; rerun the gate")
    except Exception as error:  # Any API, parse or missing-notes failure closes the gate.
        print(f"Release notes unavailable: {type(error).__name__}")
        findings = ["Upstream notes could not be verified"]
    latest = api.request(f"repos/{repo}/pulls/{number}")
    if latest["base"]["ref"] != default_branch:
        print("release-notes-gate: PR base changed during the run; reopen the PR or push a commit to rerun")
        status.update(state="error", description="PR base changed during the run; rerun")
        api.request(status_path, status)
        return 1
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
