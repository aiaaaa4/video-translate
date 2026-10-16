import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = ROOT / "skills" / "video-translate" / "scripts"
EXAMPLES_DIR = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(SCRIPTS_DIR))

from common import (  # noqa: E402
    align_segments,
    ass_time,
    build_word_table,
    display_chinese_subtitle_text,
    find_contiguous_match,
    normalize_word,
    parse_segments,
    read_json,
    split_chinese_line,
    srt_time,
    text_width,
    tokenize_raw,
    visible_len,
    word_stream,
    words_from_transcript,
)


def example_word_table():
    transcript = read_json(EXAMPLES_DIR / "transcript_words.json")
    return build_word_table(words_from_transcript(transcript))


class NormalizeTest(unittest.TestCase):
    def test_normalize_word(self):
        self.assertEqual(normalize_word("Hello,"), "hello")
        self.assertEqual(normalize_word("don’t"), "don't")
        self.assertEqual(normalize_word("  Price. "), "price")
        self.assertEqual(normalize_word("---"), "")

    def test_tokenize_raw(self):
        self.assertEqual(tokenize_raw("If price, breaks above!"), ["if", "price", "breaks", "above"])


class ParseSegmentsTest(unittest.TestCase):
    def test_parse_example_segments(self):
        text = (EXAMPLES_DIR / "segments.txt").read_text(encoding="utf-8")
        segments = parse_segments(text)
        self.assertEqual([s.index for s in segments], list(range(1, len(segments) + 1)))
        self.assertEqual(segments[0].source_raw, "if price breaks above the prior day high")
        self.assertEqual(segments[1].translation, "我们不要追涨。")

    def test_missing_field_raises(self):
        bad = "[SEG 0001]\nSRC_RAW: hello world\nZH: 你好\n[/SEG]\n"
        with self.assertRaises(ValueError):
            parse_segments(bad)

    def test_no_segments_raises(self):
        with self.assertRaises(ValueError):
            parse_segments("no segments here")


class AlignTest(unittest.TestCase):
    def test_align_full_coverage(self):
        table = example_word_table()
        segments = parse_segments((EXAMPLES_DIR / "segments.txt").read_text(encoding="utf-8"))
        aligned, failures = align_segments(table, segments)
        self.assertEqual(failures, [])
        self.assertEqual(len(aligned), len(segments))
        self.assertEqual(aligned[0]["word_start_id"], 0)
        for prev, cur in zip(aligned, aligned[1:]):
            self.assertEqual(cur["word_start_id"], prev["word_end_id"] + 1)
        self.assertEqual(aligned[-1]["word_end_id"], len(table) - 1)
        for seg in aligned:
            self.assertLess(seg["start"], seg["end"])

    def test_find_contiguous_match_respects_cursor(self):
        table = example_word_table()
        stream = word_stream(table).split()
        span = " ".join(stream[:3])
        self.assertEqual(find_contiguous_match(table, span, 0), (0, 2))
        self.assertIsNone(find_contiguous_match(table, span, 1))

    def test_align_failure_reported(self):
        table = example_word_table()
        segments = parse_segments(
            "[SEG 0001]\nSRC_RAW: totally unrelated words here\nSRC_DISPLAY: x\nZH: 无\n[/SEG]\n"
        )
        aligned, failures = align_segments(table, segments)
        self.assertEqual(aligned, [])
        self.assertEqual(len(failures), 1)


class ChineseLayoutTest(unittest.TestCase):
    def test_short_line_not_wrapped(self):
        self.assertEqual(split_chinese_line("这是一个短句"), ["这是一个短句"])

    def test_long_line_wrapped_at_punctuation(self):
        text = "这是一段非常长的中文字幕内容，用来测试手动换行逻辑是否正常工作，" "并且验证标点优先切分的行为是否符合预期设定"
        lines = split_chinese_line(text, 36.0)
        self.assertGreater(len(lines), 1)
        for line in lines:
            self.assertLessEqual(text_width(line), 36.0 + 3.0)

    def test_short_tail_merged(self):
        lines = split_chinese_line("一二三四五六七八九十" * 4, 36.0)
        self.assertGreaterEqual(visible_len(lines[-1]), 4)

    def test_display_strips_soft_punctuation(self):
        self.assertEqual(display_chinese_subtitle_text("我们不要追涨。"), "我们不要追涨")
        self.assertEqual(display_chinese_subtitle_text("先等回调，"), "先等回调")
        self.assertEqual(display_chinese_subtitle_text("真的吗？"), "真的吗？")
        self.assertEqual(display_chinese_subtitle_text("快跑！"), "快跑！")


class TimeFormatTest(unittest.TestCase):
    def test_srt_time(self):
        self.assertEqual(srt_time(0.0), "00:00:00,000")
        self.assertEqual(srt_time(3661.5), "01:01:01,500")

    def test_ass_time(self):
        self.assertEqual(ass_time(0.0), "0:00:00.00")
        self.assertEqual(ass_time(3661.55), "1:01:01.55")


if __name__ == "__main__":
    unittest.main()
