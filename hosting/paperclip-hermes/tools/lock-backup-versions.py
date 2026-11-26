#!/usr/bin/env python3
"""Close pilot WORM policies using the operator's Entra login, preserving each expiry."""

import argparse
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
import json
import re
import subprocess
import sys
from urllib.parse import quote, unquote, urlencode
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET


def timestamp(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        result = parsedate_to_datetime(value)
    if result.tzinfo is None:
        raise ValueError("retention dates must include a timezone")
    return result.astimezone(timezone.utc)


def inventory(request):
    marker, seen = "", set()
    while True:
        query = {"restype": "container", "comp": "list", "include": "versions,immutabilitypolicy", "marker": marker}
        _, body = request("GET", "/backups?" + urlencode(query), {})
        page = ET.fromstring(body)
        if page.tag != "EnumerationResults" or page.find("Blobs") is None:
            raise ValueError("invalid version inventory")
        for blob in page.findall("Blobs/Blob"):
            name, version = blob.findtext("Name"), blob.findtext("VersionId")
            if name and blob.find("Name").get("Encoded") == "true":
                name = unquote(name)
            mode = blob.findtext("Properties/ImmutabilityPolicyMode", "").lower()
            expiry = blob.findtext("Properties/ImmutabilityPolicyUntilDate")
            if not name or not version or mode not in {"locked", "unlocked"} or not expiry:
                raise ValueError("version has missing identity or WORM policy; investigate before pilot closure")
            yield name, version, mode, timestamp(expiry)
        marker = page.findtext("NextMarker") or ""
        if not marker:
            break
        if marker in seen:
            raise ValueError("repeated inventory continuation token")
        seen.add(marker)


def lock_versions(request, pilot_end, apply=False, now=None):
    clock = (lambda: now) if now is not None else (lambda: datetime.now(timezone.utc))
    if clock() < timestamp(pilot_end):
        raise ValueError("pilot has not ended")
    pending = 0
    for name, version, mode, expiry in inventory(request):
        if mode == "locked" or expiry <= clock():
            continue
        pending += 1
        if apply:
            path = "/backups/" + quote(name, safe="") + "?" + urlencode({"comp": "immutabilityPolicies", "versionid": version})
            headers, _ = request("PUT", path, {
                "x-ms-immutability-policy-mode": "Locked",
                "x-ms-immutability-policy-until-date": format_datetime(expiry, usegmt=True),
            })
            if headers.get("x-ms-immutability-policy-mode", "").lower() != "locked" or timestamp(headers.get("x-ms-immutability-policy-until-date", "")) != expiry:
                raise ValueError("locking response did not preserve expiry and confirm Locked")
    if apply and any(mode == "unlocked" and expiry > clock() for _, _, mode, expiry in inventory(request)):
        raise ValueError("unexpired Unlocked versions remain; keep uploads paused and retry")
    return pending


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True)
    parser.add_argument("--pilot-end", required=True)
    parser.add_argument("--apply", action="store_true", help="lock versions; default only counts candidates")
    args = parser.parse_args()
    try:
        if not re.fullmatch(r"[a-z0-9]{3,24}", args.account):
            raise ValueError("invalid storage account name")
        # Capture the token in memory; no token in argv, files or output. Never request account keys.
        def request(method, path, headers):
            token = json.loads(subprocess.run([
                "az", "account", "get-access-token", "--resource", "https://storage.azure.com/", "--output", "json",
            ], check=True, capture_output=True, text=True).stdout)["accessToken"]
            request_headers = {"Authorization": "Bearer " + token, "x-ms-version": "2023-11-03",
                               "x-ms-date": format_datetime(datetime.now(timezone.utc), usegmt=True), **headers}
            req = Request(f"https://{args.account}.blob.core.windows.net" + path, headers=request_headers, method=method)
            with urlopen(req, timeout=60) as response:
                return {key.lower(): value for key, value in response.headers.items()}, response.read()

        count = lock_versions(request, args.pilot_end, args.apply)
        print(f"pilot WORM closure: {count} version(s) {'locked and inventory verified' if args.apply else 'require locking (read-only)'}")
        return 0
    except (OSError, ValueError, KeyError, TypeError, ET.ParseError, subprocess.SubprocessError):
        print("pilot WORM closure failed; keep uploads paused, inspect authorization/policies and rerun", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
