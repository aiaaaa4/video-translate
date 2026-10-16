#!/usr/bin/env python3
"""Read-only audit of local, GitHub, and canonical ClawHub release state."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import release_skill


ROOT = Path(__file__).resolve().parents[1]


def command_output(command: list[str]) -> str:
    return subprocess.run(
        command,
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


def repository_links(repository: dict, item: dict) -> dict[str, str]:
    owner = repository["owner"]
    name = repository["name"]
    branch = repository["branch"]
    return {
        "github": f"https://github.com/{owner}/{name}/tree/{branch}/{item['path']}",
        "skills_sh": f"https://www.skills.sh/{owner}/{name}/{item['slug']}",
    }


def evaluate_clawhub(item: dict, published: dict | None) -> dict:
    skill = (published or {}).get("skill") or {}
    latest = (published or {}).get("latestVersion") or {}
    owner = (published or {}).get("owner") or {}
    moderation = (published or {}).get("moderation") or {}
    actual_version = str(latest.get("version") or "")
    scan_complete = moderation.get("legacyReason") != "pending.scan"
    aligned = bool(
        published
        and skill.get("slug") == item["slug"]
        and skill.get("displayName") == item["display_name"]
        and owner.get("handle") == "aiaaaa4"
        and actual_version == item["version"]
        and moderation.get("verdict") == "clean"
        and scan_complete
        and not moderation.get("isSuspicious")
        and not moderation.get("isMalwareBlocked")
    )
    return {
        "package": item["clawhub"]["package"],
        "expected_version": item["version"],
        "actual_version": actual_version or None,
        "moderation": moderation.get("verdict") or None,
        "scan_complete": scan_complete,
        "aligned": aligned,
    }


def audit() -> tuple[dict, bool]:
    registry, repository = release_skill.load_registry()
    local_head = command_output(["git", "rev-parse", "HEAD"])
    remote_line = command_output(
        ["git", "ls-remote", "origin", f"refs/heads/{repository['branch']}"]
    )
    github_head = remote_line.split()[0] if remote_line else ""
    worktree_clean = not command_output(["git", "status", "--porcelain"])
    core_aligned = worktree_clean and bool(github_head) and local_head == github_head

    skills: list[dict] = []
    clawhub_aligned = True
    for item in registry["items"]:
        if item.get("kind") != "skill":
            continue
        links = repository_links(repository, item)
        clawhub = evaluate_clawhub(item, release_skill.inspect_published_skill(item))
        clawhub_aligned = clawhub_aligned and clawhub["aligned"]
        skills.append(
            {
                "slug": item["slug"],
                "stable_id": item["skill_id"],
                "github_path": links["github"],
                "clawhub": clawhub,
                "skills_sh": {
                    "mode": "asynchronous GitHub snapshot and audits",
                    "url": links["skills_sh"],
                },
                "skillhub": {
                    "mode": "manual maintenance or uncontrolled ClawHub mirror",
                    "url": f"https://skillhub.cloud.tencent.com/skills/{item['slug']}",
                },
            }
        )

    report = {
        "core": {
            "worktree_clean": worktree_clean,
            "local_head": local_head,
            "github_main": github_head,
            "aligned": core_aligned,
        },
        "skills": skills,
        "healthy": core_aligned and clawhub_aligned,
    }
    return report, report["healthy"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    args = parser.parse_args()
    report, healthy = audit()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        core = report["core"]
        print(f"core: {'OK' if core['aligned'] else 'MISMATCH'} {core['local_head'][:8]}")
        for item in report["skills"]:
            clawhub = item["clawhub"]
            print(
                f"{item['slug']}: ClawHub "
                f"{'OK' if clawhub['aligned'] else 'MISMATCH'} "
                f"{clawhub['actual_version'] or 'missing'} ({clawhub['moderation'] or 'unknown'})"
            )
        print("skills.sh: asynchronous; inspect snapshot/audit dates separately")
        print("SkillHub: manual maintenance; mirror freshness is not a core health gate")
    raise SystemExit(0 if healthy else 1)


if __name__ == "__main__":
    main()
