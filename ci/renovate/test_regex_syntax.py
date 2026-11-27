"""Enforce the RE2 syntax subset even when native RE2 is absent in CI."""

import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[2]
UNSUPPORTED = re.compile(r"\(\?=|\(\?!|\(\?<=|\(\?<!|\\[1-9]|\\k<|\(\?>|\*\+|\+\+|\?\+")


def check_regex(value):
    if UNSUPPORTED.search(value):
        raise ValueError("unsupported RE2 syntax: " + value)


def regexes(value, key="", path=""):
    if isinstance(value, dict):
        for child, item in value.items():
            yield from regexes(item, child, path + "." + child)
    elif isinstance(value, list):
        for number, item in enumerate(value):
            yield from regexes(item, key, f"{path}[{number}]")
    elif isinstance(value, str):
        if re.fullmatch(r"!?/.*?/[a-zA-Z]*", value, re.S):
            yield path, value
        elif key == "versioning" and value.startswith("regex:"):
            yield path, value.removeprefix("regex:")
        elif key in {"matchStrings", "managerFilePatterns", "extractVersion"}:
            yield path, value


class RegexSyntaxTests(unittest.TestCase):
    def test_presets_use_re2_compatible_syntax(self):
        found = []
        for file in ("default.json", "instance.json"):
            for location, value in regexes(json.loads((ROOT / "renovate" / file).read_text())):
                found.append((file, location))
                with self.subTest(file=file, location=location):
                    check_regex(value)
        self.assertGreaterEqual(len(found), 11, "regex walk must cover the known preset expressions")

    def test_rejects_unsupported_syntax_and_accepts_named_groups(self):
        for value in (r"/^v0\.(?!99)\d+\.\d+$/", r"a(?=b)", r"(?<=a)b", r"(?<!a)b", r"(a)\1", r"\k<name>", r"(?>a)", "a*+", "a++", "a?+"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                check_regex(value)
        check_regex(r"^v?(?<major>\d{4})\.(?<minor>\d{3,4})\.(?<patch>\d+)$")

    def test_walk_covers_all_regex_locations_including_nested_negated_patterns(self):
        value = {"nested": ["!/foo/i", "/bar/", {"versioning":"regex:^(?<major>1)$", "matchStrings":["one"],
                  "managerFilePatterns":["two"], "extractVersion":"three", "ordinary":"ignored"}]}
        self.assertEqual([v for _, v in regexes(value)], ["!/foo/i", "/bar/", "^(?<major>1)$", "one", "two", "three"])
