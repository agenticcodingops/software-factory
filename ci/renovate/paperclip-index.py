#!/usr/bin/env python3
"""Publish stable Paperclip image digests, with a first-observation soak clock."""

import base64
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
from urllib.error import HTTPError
from urllib.parse import quote

SPEC = importlib.util.spec_from_file_location("release_notes_gate", Path(__file__).with_name("release-notes-gate.py"))
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)
IMAGE = "ghcr.io/paperclipai/paperclip"
PATTERN = r"v?\d{4}\.\d{3,4}\.\d+"
BRANCH = "renovate-data"
FILE = "paperclip-releases.json"


def utc_timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError("index timestamps must be UTC")
    return parsed


def build(releases, previous, resolve, now):
    """All registry reads complete before callers may publish anything."""
    observed = {(entry["version"], entry["digest"]): entry["releaseTimestamp"]
                for entry in previous.get("releases", [])}
    entries = {}
    for release in releases:
        tag = release["tag_name"]
        if release.get("draft") or release.get("prerelease") or not re.fullmatch(PATTERN, tag):
            continue
        published = release["published_at"]
        published_date = utc_timestamp(published)
        bare = tag.removeprefix("v")
        for form in (bare, "v" + bare):
            if form in entries:
                continue
            try:
                digest = resolve(IMAGE, form)
            except HTTPError as error:
                if error.code == 404:
                    continue
                raise
            if not re.fullmatch(gate.DIGEST, digest):
                raise ValueError("invalid registry digest")
            timestamp = observed.get((form, digest), now)
            parsed = utc_timestamp(timestamp)
            if (form, digest) in observed and parsed < published_date:
                timestamp = published
            entries[form] = {"version": form, "digest": digest, "releaseTimestamp": timestamp}
    if not entries:
        raise ValueError("no stable Paperclip images resolved")
    return {"releases": [entries[tag] for tag in sorted(entries)],
            "sourceUrl": "https://github.com/paperclipai/paperclip"}


def publish(api, repo, now):
    # GitHub contents and refs contracts; all writes use the API, never git push.
    # https://docs.github.com/en/rest/repos/contents#create-or-update-file-contents
    # https://docs.github.com/en/rest/git/refs#create-a-reference
    content_path = f"repos/{repo}/contents/{FILE}"
    try:
        previous_file = api.request(f"{content_path}?ref={BRANCH}")
    except HTTPError as error:
        if error.code != 404:
            raise
        previous_file = None
    previous_bytes = base64.b64decode(previous_file["content"]) if previous_file is not None else b""
    previous = json.loads(previous_bytes) if previous_file is not None else {}
    result = build(api.releases("paperclipai/paperclip"), previous, api.registry_digest, now)
    content = (json.dumps(result, indent=2, sort_keys=True) + "\n").encode()
    if content == previous_bytes:
        return False
    ref_path = f"repos/{repo}/git/ref/heads/{BRANCH}"
    try:
        api.request(ref_path)["object"]["sha"]
    except HTTPError as error:
        if error.code != 404:
            raise
        default = api.request(f"repos/{repo}")["default_branch"]
        head = api.request(f"repos/{repo}/git/ref/heads/{quote(default, safe='')}")["object"]["sha"]
        api.request(f"repos/{repo}/git/refs", {"ref": "refs/heads/" + BRANCH, "sha": head})
    payload = {"message": "chore(renovate): refresh Paperclip release index",
               "content": base64.b64encode(content).decode(), "branch": BRANCH}
    if previous_file is not None:
        payload["sha"] = previous_file["sha"]
    api.request(content_path, payload, method="PUT")
    return True


def main():
    try:
        changed = publish(gate.GitHub(os.environ["GH_TOKEN"]), os.environ["GITHUB_REPOSITORY"],
                          datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
    except Exception as error:  # No partial index is written after any read/build failure.
        print(f"paperclip-index: failed ({type(error).__name__})", file=sys.stderr)
        return 1
    print("paperclip-index: updated" if changed else "paperclip-index: unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
