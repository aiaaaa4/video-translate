import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = ROOT / "skills" / "video-translate" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from stage_directory import MARKER_NAME, reset_stage_directory  # noqa: E402


class StageDirectoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_refuses_to_clear_nonempty_unmarked_directory(self):
        target = self.root / "semantic"
        target.mkdir()
        important = target / "important.txt"
        important.write_text("keep", encoding="utf-8")

        with self.assertRaisesRegex(RuntimeError, "unmarked directory"):
            reset_stage_directory(target, "semantic-review")
        self.assertEqual(important.read_text(encoding="utf-8"), "keep")

    def test_resets_only_matching_marked_stage(self):
        target = self.root / "semantic"
        reset_stage_directory(target, "semantic-review")
        generated = target / "generated.txt"
        generated.write_text("remove", encoding="utf-8")

        reset_stage_directory(target, "semantic-review")
        self.assertFalse(generated.exists())
        self.assertTrue((target / MARKER_NAME).is_file())

        with self.assertRaisesRegex(RuntimeError, "marked for"):
            reset_stage_directory(target, "final-qc")


if __name__ == "__main__":
    unittest.main()
