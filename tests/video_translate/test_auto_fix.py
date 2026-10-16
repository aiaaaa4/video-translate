import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = ROOT / "skills" / "video-translate" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from common import parse_segments  # noqa: E402


def make_word_table(words_with_times):
    return [
        {"id": i, "text": w, "norm": w, "start": s, "end": e}
        for i, (w, s, e) in enumerate(words_with_times)
    ]


def seg_block(index, raw, display, zh):
    return f"[SEG {index:04d}]\nSRC_RAW: {raw}\nSRC_DISPLAY: {display}\nZH: {zh}\n[/SEG]\n"


def run_auto_fix(word_table, segments_text, *extra):
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        table_path = tmp_dir / "word_table.json"
        table_path.write_text(json.dumps(word_table), encoding="utf-8")
        segments_path = tmp_dir / "segments.txt"
        segments_path.write_text(segments_text, encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "auto_fix_segments.py"), str(table_path), str(segments_path), *extra],
            capture_output=True,
            text=True,
        )
        return result, segments_path.read_text(encoding="utf-8")


class AutoFixTest(unittest.TestCase):
    def test_isolated_filler_absorbed(self):
        words = [
            ("we", 0.0, 0.3), ("wait", 0.3, 0.6), ("for", 0.6, 0.8), ("the", 0.8, 0.9), ("open", 0.9, 1.3),
            ("um", 1.35, 1.5),
            ("then", 1.6, 1.9), ("we", 1.9, 2.1), ("mark", 2.1, 2.5), ("the", 2.5, 2.6), ("levels", 2.6, 3.1),
        ]
        text = (
            seg_block(1, "we wait for the open", "We wait for the open.", "我们等待开盘。")
            + "\n" + seg_block(2, "um", "Um.", "嗯。")
            + "\n" + seg_block(3, "then we mark the levels", "Then we mark the levels.", "然后标出关键位。")
        )
        result, fixed = run_auto_fix(make_word_table(words), text)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("isolated_filler", result.stdout)
        segments = parse_segments(fixed)
        self.assertEqual(len(segments), 2)
        # Filler words absorbed into the previous SRC_RAW span; Chinese unchanged.
        self.assertEqual(segments[0].source_raw, "we wait for the open um")
        self.assertEqual(segments[0].translation, "我们等待开盘。")
        self.assertNotIn("嗯", fixed)

    def test_continuation_fragment_merged(self):
        words = [
            ("he", 0.0, 0.2), ("shared", 0.2, 0.6), ("the", 0.6, 0.7), ("details", 0.7, 1.2),
            ("of", 1.3, 1.4), ("his", 1.4, 1.6), ("trading", 1.6, 2.0), ("models", 2.0, 2.5),
        ]
        text = (
            seg_block(1, "he shared the details", "He shared the details", "他分享了细节，")
            + "\n" + seg_block(2, "of his trading models", "of his trading models.", "他的交易模型的。")
        )
        result, fixed = run_auto_fix(make_word_table(words), text)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("continuation_fragment", result.stdout)
        segments = parse_segments(fixed)
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0].source_raw, "he shared the details of his trading models")

    def test_flash_subtitle_merged(self):
        words = [
            ("price", 0.0, 0.4), ("rejected", 0.4, 0.9), ("the", 0.9, 1.0), ("level", 1.0, 1.5),
            ("hard", 1.6, 2.0),
            ("so", 2.1, 2.3), ("we", 2.3, 2.5), ("exit", 2.5, 3.0),
        ]
        text = (
            seg_block(1, "price rejected the level", "Price rejected the level", "价格在该位置受阻，")
            + "\n" + seg_block(2, "hard", "hard.", "很强烈。")
            + "\n" + seg_block(3, "so we exit", "So we exit.", "所以我们离场。")
        )
        result, fixed = run_auto_fix(make_word_table(words), text)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("flash_subtitle", result.stdout)
        segments = parse_segments(fixed)
        self.assertLess(len(segments), 3)

    def test_hanging_boundary_merged_with_short_next(self):
        words = [
            ("we", 0.0, 0.2), ("scale", 0.2, 0.6), ("out", 0.6, 0.9), ("half", 0.9, 1.3), ("and", 1.3, 1.5),
            ("hold", 1.6, 2.0), ("the", 2.0, 2.1), ("rest", 2.1, 2.6),
        ]
        text = (
            seg_block(1, "we scale out half and", "We scale out half and", "我们先减一半仓，")
            + "\n" + seg_block(2, "hold the rest", "hold the rest.", "剩下的继续持有。")
        )
        result, fixed = run_auto_fix(make_word_table(words), text)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("hanging_source_boundary", result.stdout)
        segments = parse_segments(fixed)
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0].source_raw, "we scale out half and hold the rest")

    def test_guardrails_prevent_heavy_merge(self):
        # Next cue is long: merging would exceed 24 words, so the hanging
        # boundary must be left for AI review instead of merged.
        long_words = [(f"w{i}", 2.0 + i * 0.3, 2.3 + i * 0.3) for i in range(23)]
        words = [
            ("we", 0.0, 0.2), ("wait", 0.2, 0.5), ("and", 0.5, 0.7),
            *long_words,
        ]
        raw_long = " ".join(w for w, _s, _e in long_words)
        text = (
            seg_block(1, "we wait and", "We wait and", "我们等待，")
            + "\n" + seg_block(2, raw_long, raw_long, "这里是一段很长的内容翻译。")
        )
        result, fixed = run_auto_fix(make_word_table(words), text)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        segments = parse_segments(fixed)
        self.assertEqual(len(segments), 2)
        self.assertIn("nothing to merge", result.stdout)

    def test_dry_run_does_not_modify(self):
        words = [
            ("he", 0.0, 0.2), ("shared", 0.2, 0.6), ("the", 0.6, 0.7), ("details", 0.7, 1.2),
            ("of", 1.3, 1.4), ("his", 1.4, 1.6), ("models", 1.6, 2.1),
        ]
        text = (
            seg_block(1, "he shared the details", "He shared the details", "他分享了细节，")
            + "\n" + seg_block(2, "of his models", "of his models.", "模型的。")
        )
        result, fixed = run_auto_fix(make_word_table(words), text, "--dry-run")
        self.assertEqual(result.returncode, 0)
        self.assertIn("Dry run only", result.stdout)
        self.assertEqual(len(parse_segments(fixed)), 2)


if __name__ == "__main__":
    unittest.main()
