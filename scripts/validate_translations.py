#!/usr/bin/env python3
"""Validate the Delivery Status app translation files.

This repository contains only translation strings:

* ``iOS/<locale>.json``       - JSON dictionaries mapping a source string to its
                                translation.
* ``Android/values*/strings.xml`` - Android string resource files.

The validator checks the things a contributor should confirm before opening a
pull request (see ``.github/PULL_REQUEST_TEMPLATE.md``):

* every iOS file is valid JSON with no duplicate keys and only string values;
* every Android file is well-formed XML with no duplicate ``name`` attributes;
* format placeholders (``%@``, ``%lld``, ``%1$@``, ``%s``, ``%d`` ...) are
  consistent between the source key and its translation.

Structural problems (invalid JSON/XML, duplicate keys, non-string values,
mismatched placeholders) are treated as errors and make the command exit with a
non-zero status. Key-coverage differences between locales are reported as
warnings only, because translation coverage naturally drifts across languages.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
IOS_DIR = REPO_ROOT / "iOS"
ANDROID_DIR = REPO_ROOT / "Android"

# printf / NSString style format specifiers, e.g. %@, %lld, %1$@, %2$lld, %s,
# %1$d, %.2f. Group 1 captures the "conversion type" (an optional length
# modifier plus the conversion character) with the positional index, flags,
# width and precision stripped, so that %@ and %1$@ normalise to the same type,
# and %lld and %2$lld both normalise to "lld". A literal %% carries no group and
# is ignored.
# Note: the space flag is intentionally excluded. These strings contain literal
# percent signs written as, e.g., "25% of orders" (not escaped as "%%"), so a
# percent followed by a space must not be treated as a format specifier.
PLACEHOLDER_RE = re.compile(
    r"%(?:\d+\$)?[-+0#]*\d*(?:\.\d+)?((?:hh|h|ll|l|L|q|z|t|j)?[@a-zA-Z])|%%"
)


class Report:
    """Accumulates errors and warnings across all validated files."""

    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, message: str) -> None:
        self.errors.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)


def _placeholder_multiset(text: str) -> dict[str, int]:
    """Count format-specifier conversion types, ignoring positional order."""
    counts: dict[str, int] = {}
    for conversion in PLACEHOLDER_RE.findall(text):
        if not conversion:
            # A literal %% match: not a real placeholder.
            continue
        counts[conversion] = counts.get(conversion, 0) + 1
    return counts


def validate_ios(report: Report, strict_placeholders: bool) -> None:
    if not IOS_DIR.is_dir():
        report.warn(f"iOS directory not found at {IOS_DIR}; skipping.")
        return

    files = sorted(IOS_DIR.glob("*.json"))
    if not files:
        report.warn(f"No iOS JSON files found in {IOS_DIR}.")
        return

    coverage: dict[str, set[str]] = {}

    for path in files:
        rel = path.relative_to(REPO_ROOT)
        raw = path.read_text(encoding="utf-8")

        duplicate_keys: list[str] = []

        def detect_duplicates(pairs: list[tuple[str, object]]) -> dict:
            seen: set[str] = set()
            for key, _ in pairs:
                if key in seen:
                    duplicate_keys.append(key)
                seen.add(key)
            return dict(pairs)

        try:
            data = json.loads(raw, object_pairs_hook=detect_duplicates)
        except json.JSONDecodeError as exc:
            report.error(f"{rel}: invalid JSON ({exc}).")
            continue

        if not isinstance(data, dict):
            report.error(f"{rel}: top-level JSON value must be an object.")
            continue

        for key in duplicate_keys:
            report.error(f"{rel}: duplicate key {key!r}.")

        keys: set[str] = set()
        for key, value in data.items():
            keys.add(key)
            if not isinstance(value, str):
                report.error(
                    f"{rel}: value for {key!r} must be a string, got {type(value).__name__}."
                )
                continue
            if value.strip() == "":
                report.warn(f"{rel}: empty translation for {key!r}.")
                continue
            expected = _placeholder_multiset(key)
            actual = _placeholder_multiset(value)
            if expected and expected != actual:
                message = (
                    f"{rel}: placeholder mismatch for {key!r} "
                    f"(source {sorted(expected.items())} vs translation {sorted(actual.items())})."
                )
                if strict_placeholders:
                    report.error(message)
                else:
                    report.warn(message)

        coverage[rel.as_posix()] = keys

    _report_coverage(report, coverage, "iOS")


def validate_android(report: Report, strict_placeholders: bool) -> None:
    if not ANDROID_DIR.is_dir():
        report.warn(f"Android directory not found at {ANDROID_DIR}; skipping.")
        return

    files = sorted(ANDROID_DIR.glob("values*/strings.xml"))
    if not files:
        report.warn(f"No Android strings.xml files found under {ANDROID_DIR}.")
        return

    coverage: dict[str, set[str]] = {}
    # Parsed text of every file, keyed by relative path, for placeholder checks.
    texts: dict[str, dict[str, str]] = {}

    for path in files:
        rel = path.relative_to(REPO_ROOT)
        try:
            root = ET.fromstring(path.read_text(encoding="utf-8"))
        except ET.ParseError as exc:
            report.error(f"{rel}: malformed XML ({exc}).")
            continue

        keys: set[str] = set()
        seen: set[str] = set()
        strings: dict[str, str] = {}
        for element in root.findall("string"):
            name = element.get("name")
            if not name:
                report.error(f"{rel}: <string> element without a 'name' attribute.")
                continue
            if name in seen:
                report.error(f"{rel}: duplicate string name {name!r}.")
            seen.add(name)
            keys.add(name)

            text = element.text or ""
            strings[name] = text
            translatable = element.get("translatable", "true").lower() != "false"
            if translatable and text.strip() == "":
                report.warn(f"{rel}: empty translation for {name!r}.")

        coverage[rel.as_posix()] = keys
        texts[rel.as_posix()] = strings

    # Compare placeholders in each localized string against the base file, whose
    # text is the source string the resource name refers to.
    base_rel = (ANDROID_DIR / "values" / "strings.xml").relative_to(REPO_ROOT).as_posix()
    base_strings = texts.get(base_rel)
    if base_strings is not None:
        for rel_name, strings in sorted(texts.items()):
            if rel_name == base_rel:
                continue
            for name, text in strings.items():
                if name not in base_strings or text.strip() == "":
                    continue
                expected = _placeholder_multiset(base_strings[name])
                actual = _placeholder_multiset(text)
                if expected and expected != actual:
                    message = (
                        f"{rel_name}: placeholder mismatch for {name!r} "
                        f"(source {sorted(expected.items())} vs translation "
                        f"{sorted(actual.items())})."
                    )
                    if strict_placeholders:
                        report.error(message)
                    else:
                        report.warn(message)

    _report_coverage(report, coverage, "Android")


def _report_coverage(
    report: Report, coverage: dict[str, set[str]], platform: str
) -> None:
    if len(coverage) < 2:
        return
    # Use the file with the most keys as the coverage reference.
    reference_file = max(coverage, key=lambda name: len(coverage[name]))
    reference_keys = coverage[reference_file]
    for name, keys in sorted(coverage.items()):
        if name == reference_file:
            continue
        missing = reference_keys - keys
        extra = keys - reference_keys
        if missing:
            report.warn(
                f"{platform}: {name} is missing {len(missing)} key(s) present in "
                f"{reference_file}."
            )
        if extra:
            report.warn(
                f"{platform}: {name} has {len(extra)} key(s) not present in "
                f"{reference_file}."
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--platform",
        choices=["ios", "android", "all"],
        default="all",
        help="Which platform's translations to validate (default: all).",
    )
    parser.add_argument(
        "--strict-placeholders",
        action="store_true",
        help="Treat placeholder mismatches as errors instead of warnings.",
    )
    args = parser.parse_args(argv)

    report = Report()
    if args.platform in ("ios", "all"):
        validate_ios(report, args.strict_placeholders)
    if args.platform in ("android", "all"):
        validate_android(report, args.strict_placeholders)

    for warning in report.warnings:
        print(f"WARN: {warning}")
    for error in report.errors:
        print(f"ERROR: {error}")

    print(
        f"\nValidation finished: {len(report.errors)} error(s), "
        f"{len(report.warnings)} warning(s)."
    )
    return 1 if report.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
