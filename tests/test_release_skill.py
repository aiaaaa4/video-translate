from __future__ import annotations

import contextlib
import importlib.util
import io
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("release_skill", ROOT / "tools" / "release_skill.py")
assert SPEC and SPEC.loader
release_skill = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_skill)


class ReleaseSkillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry, self.repository = release_skill.load_registry()
        self.item = self.registry["items"][0]

    def test_registry_exposes_only_video_translate(self) -> None:
        self.assertEqual([item["slug"] for item in self.registry["items"]], ["video-translate"])
        self.assertEqual(
            set(self.item["platforms"]),
            {"github", "clawhub", "skills.sh", "skillhub", "skillsmp"},
        )

    def test_publish_command_uses_canonical_metadata(self) -> None:
        command = release_skill.publish_command(self.item, self.repository, "Test release", dry_run=True)
        self.assertIn("人工级视频字幕翻译", command)
        self.assertIn(self.item["version"], command)
        self.assertIn("subtitle,translation,video,aiaaaa4", command)
        self.assertEqual(command[command.index("--slug") + 1], "video-translate")
        self.assertIn("--dry-run", command)

    def test_semver_and_existing_version_guards(self) -> None:
        self.assertLess(release_skill.semver_key("1.0.9"), release_skill.semver_key("1.1.0"))
        published = {"latestVersion": {"version": self.item["version"]}}
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                release_skill.ensure_newer_version(self.item, published)

    def test_read_back_verifies_canonical_identity(self) -> None:
        published = {
            "skill": {"slug": self.item["slug"], "displayName": self.item["display_name"]},
            "latestVersion": {"version": self.item["version"]},
            "owner": {"handle": self.repository["owner"]},
            "moderation": {"isSuspicious": False, "isMalwareBlocked": False, "verdict": "clean"},
        }
        with contextlib.redirect_stdout(io.StringIO()):
            release_skill.verify_published_skill(self.item, self.repository, published)


if __name__ == "__main__":
    unittest.main()
