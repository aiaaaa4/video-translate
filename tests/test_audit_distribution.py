from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
SPEC = importlib.util.spec_from_file_location(
    "audit_distribution", ROOT / "tools" / "audit_distribution.py"
)
assert SPEC and SPEC.loader
audit_distribution = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit_distribution)
sys.path.pop(0)


class AuditDistributionTests(unittest.TestCase):
    def test_distribution_links_follow_registry_repository(self) -> None:
        repository = {"owner": "example-owner", "name": "example-skill", "branch": "stable"}
        item = {"slug": "demo", "path": "skills/demo"}

        self.assertEqual(
            audit_distribution.repository_links(repository, item),
            {
                "github": "https://github.com/example-owner/example-skill/tree/stable/skills/demo",
                "skills_sh": "https://www.skills.sh/example-owner/example-skill/demo",
            },
        )

    def test_clawhub_alignment_requires_identity_version_and_clean_moderation(self) -> None:
        item = {
            "slug": "demo",
            "skill_id": "aiaaaa4.demo",
            "display_name": "Demo",
            "version": "1.2.3",
            "clawhub": {"package": "@aiaaaa4/demo"},
        }
        published = {
            "skill": {"slug": "demo", "displayName": "Demo"},
            "latestVersion": {"version": "1.2.3"},
            "owner": {"handle": "aiaaaa4"},
            "moderation": {
                "verdict": "clean",
                "legacyReason": "scanner.vt.clean",
                "isSuspicious": False,
                "isMalwareBlocked": False,
            },
        }
        self.assertTrue(audit_distribution.evaluate_clawhub(item, published)["aligned"])
        published["latestVersion"]["version"] = "1.2.2"
        self.assertFalse(audit_distribution.evaluate_clawhub(item, published)["aligned"])


if __name__ == "__main__":
    unittest.main()
