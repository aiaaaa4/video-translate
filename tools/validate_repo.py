#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DISALLOWED_PARTS = {".env", "outputs", "runtime", ".DS_Store", "__pycache__", ".pytest_cache"}
DISALLOWED_SUFFIXES = {".mp4", ".mov", ".mkv", ".zip", ".log", ".pyc"}
LOCAL_ONLY_DIRS = {".git", "local-projects"}
SEMVER_PATTERN = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
ALLOWED_SKILL_FRONTMATTER_KEYS = {"name", "description", "permissions", "metadata"}
EXPECTED_PLATFORMS = {"github", "clawhub", "skills.sh", "skillhub", "skillsmp"}


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


def parse_frontmatter(path: Path) -> dict[str, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != "---":
        fail(f"{path.relative_to(ROOT)} must start with YAML frontmatter")

    fields: dict[str, str] = {}
    for line in lines[1:]:
        if line == "---":
            return fields
        if not line.strip():
            continue
        if line[0].isspace():
            # Nested YAML belongs to the preceding top-level metadata field.
            continue
        if ":" not in line:
            fail(f"{path.relative_to(ROOT)} has invalid frontmatter line: {line}")
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip()

    fail(f"{path.relative_to(ROOT)} frontmatter is not closed")
    return fields


def validate_registry() -> list[dict]:
    registry_path = ROOT / "registry.json"
    if not registry_path.exists():
        fail("registry.json is missing")

    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    if registry.get("namespace") != "aiaaaa4":
        fail("registry namespace must be aiaaaa4")

    repository = registry.get("repository")
    if not isinstance(repository, dict):
        fail("registry.repository must be an object")
    if repository.get("owner") != "aiaaaa4" or repository.get("name") != "video-translate":
        fail("registry repository identity is invalid")
    if repository.get("branch") != "main":
        fail("registry repository branch must be main")

    items = registry.get("items")
    if not isinstance(items, list) or not items:
        fail("registry.items must be a non-empty list")

    seen_ids: set[str] = set()
    seen_slugs: set[str] = set()
    for item in items:
        skill_id = item.get("skill_id")
        slug = item.get("slug")
        rel_path = item.get("path")
        kind = item.get("kind")
        display_name = item.get("display_name")
        version = item.get("version")
        platforms = item.get("platforms")
        if not all(isinstance(x, str) and x for x in [skill_id, slug, rel_path, kind, display_name, version]):
            fail(f"registry item has missing required fields: {item}")
        if skill_id != f"aiaaaa4.{slug}":
            fail(f"registry skill_id must match namespace and slug: {skill_id}")
        if not SEMVER_PATTERN.fullmatch(version):
            fail(f"registry version must be semver for {slug}: {version}")
        if not isinstance(platforms, list) or set(platforms) != EXPECTED_PLATFORMS:
            fail(f"registry platforms are incomplete for {slug}: {platforms}")
        if skill_id in seen_ids:
            fail(f"duplicate skill_id: {skill_id}")
        if slug in seen_slugs:
            fail(f"duplicate slug: {slug}")
        seen_ids.add(skill_id)
        seen_slugs.add(slug)

        item_path = ROOT / rel_path
        if not item_path.exists():
            fail(f"registry path does not exist: {rel_path}")
        if kind == "skill" and not (item_path / "SKILL.md").exists():
            fail(f"skill is missing SKILL.md: {rel_path}")
        if kind == "skill":
            clawhub = item.get("clawhub")
            if not isinstance(clawhub, dict):
                fail(f"registry clawhub metadata must be an object for {slug}")
            if "slug" in clawhub:
                fail(f"registry ClawHub slug aliases are not allowed for {slug}")
            if clawhub.get("package") != f"@aiaaaa4/{slug}":
                fail(f"registry ClawHub package is invalid for {slug}")
            topics = clawhub.get("topics")
            if not isinstance(topics, list) or not all(isinstance(topic, str) and topic for topic in topics):
                fail(f"registry ClawHub topics are invalid for {slug}")

    return items


def validate_readme(items: list[dict]) -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for item in items:
        identity = f"**`{item['skill_id']}` · v{item['version']} ·"
        if readme.count(identity) != 1:
            fail(f"README identity/version is missing or duplicated for {item['slug']}: {identity}")
        clawhub_url = f"[ClawHub](https://clawhub.ai/aiaaaa4/{item['slug']})"
        if readme.count(clawhub_url) != 1:
            fail(f"README ClawHub link is missing or duplicated for {item['slug']}")


def validate_skills(items: list[dict]) -> None:
    expected_directories: set[str] = set()
    for item in items:
        if item["kind"] != "skill":
            continue
        path = ROOT / item["path"] / "SKILL.md"
        fields = parse_frontmatter(path)
        keys = set(fields)
        if not {"name", "description"}.issubset(keys) or not keys.issubset(ALLOWED_SKILL_FRONTMATTER_KEYS):
            fail(
                f"{path.relative_to(ROOT)} frontmatter must contain name and description, "
                f"with only supported optional keys {sorted(ALLOWED_SKILL_FRONTMATTER_KEYS - {'name', 'description'})}; "
                f"got {sorted(keys)}"
            )
        if fields["name"] != item["slug"]:
            fail(f"{path.relative_to(ROOT)} name must match registry slug {item['slug']}")
        if not fields["description"]:
            fail(f"{path.relative_to(ROOT)} description is empty")

        expected_directories.add(item["slug"])
        agent_config = path.parent / "agents" / "openai.yaml"
        if not agent_config.exists():
            fail(f"{agent_config.relative_to(ROOT)} is missing")
        config_text = agent_config.read_text(encoding="utf-8")
        display_match = re.search(r'^\s*display_name:\s*"([^"]+)"\s*$', config_text, re.MULTILINE)
        if not display_match or display_match.group(1) != item["display_name"]:
            fail(f"{agent_config.relative_to(ROOT)} display_name must match registry")
        prompt_match = re.search(r'^\s*default_prompt:\s*"([^"]+)"\s*$', config_text, re.MULTILINE)
        if not prompt_match or f"${item['slug']}" not in prompt_match.group(1):
            fail(f"{agent_config.relative_to(ROOT)} default_prompt must mention ${item['slug']}")

    skill_root = ROOT / "skills"
    actual_directories = {path.name for path in skill_root.iterdir() if path.is_dir()}
    if actual_directories != expected_directories:
        fail(f"skills directory and registry disagree: expected {sorted(expected_directories)}, got {sorted(actual_directories)}")


def validate_scripts() -> None:
    for script in (ROOT / "skills").rglob("*.sh"):
        result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        if result.returncode:
            fail(f"shell syntax error in {script.relative_to(ROOT)}: {result.stderr.strip()}")
    for script in (ROOT / "skills").rglob("*.py"):
        try:
            compile(script.read_text(encoding="utf-8"), str(script), "exec")
        except SyntaxError as error:
            fail(f"Python syntax error in {script.relative_to(ROOT)}: {error}")


def validate_public_tree() -> None:
    tracked_paths = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True
    ).stdout.decode("utf-8").split("\0")
    for raw_path in tracked_paths:
        if not raw_path:
            continue
        rel = Path(raw_path)
        if any(part in LOCAL_ONLY_DIRS for part in rel.parts):
            fail(f"local-only path is tracked by Git: {rel}")
        if rel.name.startswith(".env"):
            fail(f"environment file is not allowed in repo: {rel}")
        if any(part in DISALLOWED_PARTS for part in rel.parts):
            fail(f"disallowed local/generated path in repo: {rel}")
        if rel.suffix.lower() in DISALLOWED_SUFFIXES:
            fail(f"disallowed generated/binary file in repo: {rel}")


def main() -> None:
    items = validate_registry()
    validate_readme(items)
    validate_skills(items)
    validate_scripts()
    validate_public_tree()
    print("repo validation passed")


if __name__ == "__main__":
    main()
