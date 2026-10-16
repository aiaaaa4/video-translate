from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "bump_skill_version", ROOT / "tools" / "bump_skill_version.py"
)
assert SPEC and SPEC.loader
bump_skill_version = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bump_skill_version)


class BumpSkillVersionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.registry = self.root / "registry.json"
        self.readme = self.root / "README.md"
        self.registry.write_text(
            json.dumps(
                {
                    "items": [
                        {
                            "slug": "demo",
                            "skill_id": "aiaaaa4.demo",
                            "version": "1.0.9",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        self.readme.write_text("**`aiaaaa4.demo` · v1.0.9 · link**\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_bump_updates_registry_and_readme(self) -> None:
        result = bump_skill_version.bump(self.registry, self.readme, "demo", "1.1.0")
        self.assertEqual(result, ("1.0.9", "1.1.0"))
        registry = json.loads(self.registry.read_text(encoding="utf-8"))
        self.assertEqual(registry["items"][0]["version"], "1.1.0")
        self.assertIn("v1.1.0", self.readme.read_text(encoding="utf-8"))

    def test_bump_rejects_reuse_or_downgrade(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be greater"):
            bump_skill_version.bump(self.registry, self.readme, "demo", "1.0.9")


if __name__ == "__main__":
    unittest.main()
