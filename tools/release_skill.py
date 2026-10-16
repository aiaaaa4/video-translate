#!/usr/bin/env python3
"""Publish exactly one registry-managed skill to ClawHub."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SEMVER_PATTERN = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-[0-9A-Za-z.-]+)?$")
CLAWHUB_PACKAGE = "clawhub@0.23.1"


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def run(command: list[str], *, capture_output: bool = False) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, check=True, text=True, capture_output=capture_output)
    except subprocess.CalledProcessError as error:
        if error.stdout:
            print(error.stdout, end="")
        if error.stderr:
            print(error.stderr, end="", file=sys.stderr)
        fail(f"command failed: {' '.join(command[:3])}")


def load_registry() -> tuple[dict, dict]:
    registry = json.loads((ROOT / "registry.json").read_text(encoding="utf-8"))
    repository = registry.get("repository")
    if not isinstance(repository, dict):
        fail("registry.repository is missing")
    return registry, repository


def select_skill(registry: dict, slug: str) -> dict:
    for item in registry.get("items", []):
        if item.get("slug") == slug and item.get("kind") == "skill":
            return item
    fail(f"unknown public skill: {slug}")
    return {}


def source_commit() -> str:
    result = run(["git", "rev-parse", "HEAD"], capture_output=True)
    return result.stdout.strip()


def ensure_pushed_main(repository: dict) -> None:
    status = run(["git", "status", "--porcelain"], capture_output=True).stdout.strip()
    if status:
        fail("formal releases require a clean Git worktree")

    branch_result = subprocess.run(
        ["git", "symbolic-ref", "--short", "-q", "HEAD"],
        text=True,
        capture_output=True,
    )
    branch = branch_result.stdout.strip()
    if branch and branch != repository["branch"]:
        fail(f"formal releases must run from {repository['branch']}, got {branch}")

    remote_ref = f"refs/heads/{repository['branch']}"
    remote = run(["git", "ls-remote", "origin", remote_ref], capture_output=True).stdout.strip()
    remote_commit = remote.split()[0] if remote else ""
    if not remote_commit:
        fail(f"could not resolve origin/{repository['branch']}")
    if source_commit() != remote_commit:
        fail(f"local HEAD must match origin/{repository['branch']} before publishing")


def authenticate() -> None:
    token = os.environ.get("CLAWHUB_TOKEN")
    if not token:
        return
    run(["npx", "--yes", CLAWHUB_PACKAGE, "login", "--token", token, "--label", "GitHub Actions"])


def semver_key(version: str) -> tuple[int, int, int]:
    match = SEMVER_PATTERN.fullmatch(version)
    if not match:
        fail(f"invalid semver: {version}")
    return tuple(int(value) for value in match.groups())


def inspect_published_skill(item: dict) -> dict | None:
    command = ["npx", "--yes", CLAWHUB_PACKAGE, "inspect", item["clawhub"]["package"], "--json"]
    result = subprocess.run(command, text=True, capture_output=True)
    if result.returncode:
        detail = f"{result.stdout}\n{result.stderr}".lower()
        if "not found" in detail or "unavailable" in detail:
            return None
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
        fail("could not inspect the canonical ClawHub package")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        fail("ClawHub inspect returned invalid JSON")
    return None


def ensure_newer_version(item: dict, published: dict | None) -> None:
    if not published:
        return
    remote_version = str((published.get("latestVersion") or {}).get("version") or "")
    if not remote_version:
        fail("canonical ClawHub package has no latest version")
    if semver_key(item["version"]) <= semver_key(remote_version):
        fail(
            f"registry version {item['version']} must be greater than "
            f"ClawHub version {remote_version} for {item['slug']}"
        )


def published_skill_error(item: dict, repository: dict, published: dict | None) -> str | None:
    if not published:
        return "published ClawHub package could not be read back"
    skill = published.get("skill") or {}
    latest = published.get("latestVersion") or {}
    owner = published.get("owner") or {}
    moderation = published.get("moderation") or {}
    expected = {
        "slug": item["slug"],
        "displayName": item["display_name"],
        "version": item["version"],
        "owner": repository["owner"],
    }
    actual = {
        "slug": skill.get("slug"),
        "displayName": skill.get("displayName"),
        "version": latest.get("version"),
        "owner": owner.get("handle"),
    }
    if actual != expected:
        return f"ClawHub read-back mismatch: expected {expected}, got {actual}"
    if moderation.get("isSuspicious") or moderation.get("isMalwareBlocked"):
        return f"ClawHub moderation blocked the release: {moderation}"
    return None


def verify_published_skill(item: dict, repository: dict, published: dict | None) -> None:
    error = published_skill_error(item, repository, published)
    if error:
        fail(error)
    moderation = (published or {}).get("moderation") or {}
    print(
        json.dumps(
            {
                "verified": True,
                "package": item["clawhub"]["package"],
                "version": item["version"],
                "moderation": moderation.get("verdict", "pending"),
            },
            ensure_ascii=False,
        )
    )


def publish_command(item: dict, repository: dict, changelog: str, dry_run: bool) -> list[str]:
    clawhub = item["clawhub"]
    command = [
        "npx",
        "--yes",
        CLAWHUB_PACKAGE,
        "skill",
        "publish",
        item["path"],
        "--owner",
        repository["owner"],
        "--slug",
        item["slug"],
        "--name",
        item["display_name"],
        "--version",
        item["version"],
        "--changelog",
        changelog,
        "--source-repo",
        f"{repository['owner']}/{repository['name']}",
        "--source-ref",
        repository["branch"],
        "--source-commit",
        source_commit(),
        "--source-path",
        item["path"],
        "--topics",
        ",".join(clawhub["topics"]),
        "--json",
    ]
    if dry_run:
        command.append("--dry-run")
    return command


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill", required=True, help="Registry slug to publish")
    parser.add_argument("--changelog", required=True, help="Public release summary")
    parser.add_argument("--dry-run", action="store_true", help="Validate with ClawHub without publishing")
    args = parser.parse_args()

    if not args.changelog.strip():
        fail("changelog must not be empty")

    validation = run([sys.executable, "tools/validate_repo.py"], capture_output=True)
    print(validation.stdout, end="")

    registry, repository = load_registry()
    item = select_skill(registry, args.skill)
    if not args.dry_run:
        ensure_pushed_main(repository)
    authenticate()
    published = inspect_published_skill(item)
    ensure_newer_version(item, published)
    command = publish_command(item, repository, args.changelog.strip(), args.dry_run)
    run(command)
    if not args.dry_run:
        for attempt in range(1, 6):
            published = inspect_published_skill(item)
            error = published_skill_error(item, repository, published)
            if not error:
                verify_published_skill(item, repository, published)
                break
            if attempt == 5:
                fail(error)
            time.sleep(2)


if __name__ == "__main__":
    main()
