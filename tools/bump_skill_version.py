#!/usr/bin/env python3
"""Bump one public skill version and keep README display metadata aligned."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SEMVER_PATTERN = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def semver_key(version: str) -> tuple[int, int, int]:
    match = SEMVER_PATTERN.fullmatch(version)
    if not match:
        raise ValueError(f"invalid stable semver: {version}")
    return tuple(int(value) for value in match.groups())


def bump(registry_path: Path, readme_path: Path, slug: str, new_version: str) -> tuple[str, str]:
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    item = next((entry for entry in registry.get("items", []) if entry.get("slug") == slug), None)
    if not item:
        raise ValueError(f"unknown public skill: {slug}")
    old_version = str(item["version"])
    if semver_key(new_version) <= semver_key(old_version):
        raise ValueError(f"new version {new_version} must be greater than {old_version}")

    readme = readme_path.read_text(encoding="utf-8")
    old_marker = f"**`{item['skill_id']}` · v{old_version} ·"
    new_marker = f"**`{item['skill_id']}` · v{new_version} ·"
    if readme.count(old_marker) != 1:
        raise ValueError(f"README version marker is missing or duplicated: {old_marker}")

    item["version"] = new_version
    registry_path.write_text(json.dumps(registry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    readme_path.write_text(readme.replace(old_marker, new_marker, 1), encoding="utf-8")
    return old_version, new_version


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill", required=True, help="Canonical registry slug")
    parser.add_argument("--version", required=True, help="New stable semver")
    args = parser.parse_args()
    try:
        old_version, new_version = bump(
            ROOT / "registry.json",
            ROOT / "README.md",
            args.skill,
            args.version,
        )
    except ValueError as error:
        raise SystemExit(f"ERROR: {error}") from error
    print(f"{args.skill}: {old_version} -> {new_version}")


if __name__ == "__main__":
    main()
