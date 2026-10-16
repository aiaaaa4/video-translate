import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = ROOT / "skills" / "video-translate" / "scripts"
EXAMPLES_DIR = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(SCRIPTS_DIR))

from common import (  # noqa: E402
    build_word_table,
    find_fuzzy_match,
    read_json,
    token_edit_distance,
    words_from_transcript,
)


def example_word_table():
    transcript = read_json(EXAMPLES_DIR / "transcript_words.json")
    return build_word_table(words_from_transcript(transcript))


class TokenEditDistanceTest(unittest.TestCase):
    def test_identical(self):
        self.assertEqual(token_edit_distance(["a", "b"], ["a", "b"], 2), 0)

    def test_substitution(self):
        self.assertEqual(token_edit_distance(["a", "b", "c"], ["a", "x", "c"], 2), 1)

    def test_insertion_deletion(self):
        self.assertEqual(token_edit_distance(["a", "b", "c"], ["a", "c"], 2), 1)
        self.assertEqual(token_edit_distance(["a", "c"], ["a", "b", "c"], 2), 1)

    def test_limit_early_exit(self):
        self.assertGreater(token_edit_distance(list("abcdef"), list("uvwxyz"), 2), 2)


class FuzzyMatchTest(unittest.TestCase):
    def test_exact_span_found(self):
        table = example_word_table()
        result = find_fuzzy_match(table, "if price breaks above", 0)
        self.assertEqual(result, (0, 3, 0))

    def test_one_substitution_found(self):
        table = example_word_table()
        # "breaks" rewritten to "break" by a careless model
        result = find_fuzzy_match(table, "if price break above the prior day high", 0)
        self.assertIsNotNone(result)
        start_i, end_i, edits = result
        self.assertEqual((start_i, end_i), (0, 7))
        self.assertEqual(edits, 1)

    def test_unrelated_not_found(self):
        table = example_word_table()
        self.assertIsNone(find_fuzzy_match(table, "totally different words entirely", 0))


class AutoRepairScriptTest(unittest.TestCase):
    def test_validate_auto_repairs_light_rewrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            out_dir = tmp_dir / "work"
            subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "extract_word_stream.py"),
                    str(EXAMPLES_DIR / "transcript_words.json"),
                    "--out-dir",
                    str(out_dir),
                ],
                check=True,
                capture_output=True,
            )

            original = (EXAMPLES_DIR / "segments.txt").read_text(encoding="utf-8")
            # Simulate a model dropping a word in SEG 0001's SRC_RAW.
            broken = original.replace(
                "SRC_RAW: if price breaks above the prior day high",
                "SRC_RAW: if price breaks above prior day high",
            )
            self.assertNotEqual(original, broken)
            segments_path = tmp_dir / "segments.txt"
            segments_path.write_text(broken, encoding="utf-8")

            # Without --auto-repair the validation fails.
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "validate_segments.py"),
                    str(out_dir / "word_table.json"),
                    str(segments_path),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("closest word-stream window", result.stdout)

            # With --auto-repair the SRC_RAW is restored and validation passes.
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "validate_segments.py"),
                    str(out_dir / "word_table.json"),
                    str(segments_path),
                    "--auto-repair",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("Auto-repaired 1 SRC_RAW", result.stdout)
            repaired = segments_path.read_text(encoding="utf-8")
            self.assertIn("SRC_RAW: if price breaks above the prior day high", repaired)
            self.assertTrue(segments_path.with_suffix(".txt.bak").exists())
            # Other fields untouched.
            self.assertIn("ZH: 如果价格突破前一日高点，", repaired)


if __name__ == "__main__":
    unittest.main()
