"""Compute the next release version.

Rules (first match wins):
  1. --bump (manual workflow_dispatch input)
  2. PR label: major / minor / patch
  3. Conventional-commit PR title: "type!:" or "BREAKING CHANGE" -> major, "feat" -> minor
  4. patch
If no tag exists yet, the version in manifest.json is used unchanged (first release).
"""

from __future__ import annotations

import argparse
import json
import re
import sys

SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def bump_kind(labels: list[str], title: str, manual: str | None) -> str:
    if manual in ("major", "minor", "patch"):
        return manual
    for kind in ("major", "minor", "patch"):
        if kind in labels:
            return kind
    title = title or ""
    if re.match(r"^\w+(\([^)]*\))?!:", title) or "BREAKING CHANGE" in title:
        return "major"
    if re.match(r"^feat(\([^)]*\))?:", title, re.IGNORECASE):
        return "minor"
    return "patch"


def next_version(latest_tag: str | None, manifest_version: str, kind: str) -> str:
    if not latest_tag:
        if not SEMVER.match(manifest_version):
            raise ValueError(f"manifest version {manifest_version!r} is not semver")
        return manifest_version.lstrip("v")
    m = SEMVER.match(latest_tag.strip())
    if not m:
        raise ValueError(f"latest tag {latest_tag!r} is not semver")
    major, minor, patch = map(int, m.groups())
    if kind == "major":
        return f"{major + 1}.0.0"
    if kind == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--latest-tag", default="")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--labels", default="[]")
    parser.add_argument("--title", default="")
    parser.add_argument("--bump", default="")
    args = parser.parse_args(argv)
    try:
        labels = json.loads(args.labels) or []
    except json.JSONDecodeError:
        labels = []
    with open(args.manifest, encoding="utf-8") as f:
        manifest_version = json.load(f)["version"]
    kind = bump_kind(labels, args.title, args.bump or None)
    print(next_version(args.latest_tag or None, manifest_version, kind))
    return 0


if __name__ == "__main__":
    sys.exit(main())
