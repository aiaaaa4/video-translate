import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = ROOT / "skills" / "video-translate"
LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
EXTERNAL_PREFIXES = ("http://", "https://", "#", "mailto:")


def markdown_links(path: Path):
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        for target in LINK_RE.findall(line):
            yield lineno, target


def relative_targets():
    """Yield (path, lineno, target, resolved) for every non-external markdown link."""
    for md in sorted(SKILL_DIR.rglob("*.md")):
        for lineno, target in markdown_links(md):
            if target.startswith(EXTERNAL_PREFIXES):
                continue
            path_part = target.split("#")[0]
            if not path_part:
                continue
            yield md, lineno, target, (md.parent / path_part).resolve()


class SkillPackageLinkTest(unittest.TestCase):
    """Only `skills/video-translate/` is exported and installed.

    A link like `../../docs/...` resolves inside the development repository but
    breaks in the production copy, where the skill subtree is exported on its
    own. Documentation that lives at the repository root must therefore be
    referenced by URL, not by a path that climbs out of the package.
    """

    def test_no_relative_link_escapes_the_skill_package(self):
        escaping = []
        for md, lineno, target, resolved in relative_targets():
            try:
                resolved.relative_to(SKILL_DIR.resolve())
            except ValueError:
                escaping.append(f"{md.relative_to(ROOT)}:{lineno} -> {target}")
        self.assertEqual(
            escaping,
            [],
            "These links climb out of the installable skill package: " + "; ".join(escaping),
        )

    def test_in_package_relative_links_resolve(self):
        missing = []
        for md, lineno, target, resolved in relative_targets():
            try:
                resolved.relative_to(SKILL_DIR.resolve())
            except ValueError:
                continue
            if not resolved.exists():
                missing.append(f"{md.relative_to(ROOT)}:{lineno} -> {target}")
        self.assertEqual(
            missing,
            [],
            "These in-package links do not resolve: " + "; ".join(missing),
        )


if __name__ == "__main__":
    unittest.main()
