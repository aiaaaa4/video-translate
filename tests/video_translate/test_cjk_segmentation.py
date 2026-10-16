import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = ROOT / "skills" / "video-translate" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from generate_segments_with_dashscope import build_chunks, token_norm  # noqa: E402


def chunks_for(text: str, words: list[str], limit: int = 18):
    segment = {"text": text, "words": [{"word": word} for word in words]}
    return build_chunks({"segments": [segment]}, limit)


def covered_words(chunks) -> list[str]:
    return " ".join(chunk.source_raw for chunk in chunks).split()


def covered_display(chunks) -> str:
    return "".join(chunk.source_display for chunk in chunks)


def strip_punctuation(text: str) -> str:
    return "".join(character for character in text if character not in " ,;:，。！？；：、")


class CjkSegmentationTest(unittest.TestCase):
    """Regression coverage for CJK source text.

    The chunker used a Latin-only token whitelist. Any segment that contained a
    single Latin term (GEX, SPX, Volume ...) had every CJK word before or after
    that term silently dropped from the delivered subtitles, and its display
    text collapsed to the bare Latin token.
    """

    def test_token_norm_keeps_cjk_text(self):
        self.assertEqual(token_norm("极值位"), "极值位")
        self.assertEqual(token_norm("GEX"), "gex")
        self.assertEqual(token_norm("，"), "")

    def test_trailing_cjk_words_after_latin_term_are_kept(self):
        words = ["这个", "GEX", "的", "极值位", "就是", "最大", "的值"]
        chunks = chunks_for("这个GEX的极值位就是最大的值", words)
        self.assertEqual(covered_words(chunks), [word.lower() for word in words])
        self.assertIn("极值位", covered_display(chunks))

    def test_leading_latin_term_does_not_swallow_the_segment(self):
        words = ["SPX", "今天", "走势", "很强"]
        chunks = chunks_for("SPX今天走势很强", words)
        self.assertEqual(covered_words(chunks), [word.lower() for word in words])
        self.assertEqual(covered_display(chunks), "SPX今天走势很强")

    def test_mixed_segment_splits_on_cjk_punctuation_without_losing_words(self):
        words = [
            "第一", "我", "建议", "大家", "用", "Volume", "和", "GEX", "一起", "对照", "着", "看",
            "这样", "能", "解释", "很多", "显著", "的", "GEX", "为什么", "会", "失效",
        ]
        text = "第一，我建议大家用Volume和GEX一起对照着看，这样能解释很多显著的GEX为什么会失效。"
        chunks = chunks_for(text, words)
        self.assertGreater(len(chunks), 1)
        self.assertEqual(covered_words(chunks), [word.lower() for word in words])
        # Split points trim their own leading/trailing punctuation, as the Latin
        # path always did, so compare the text ignoring boundary punctuation.
        self.assertEqual(strip_punctuation(covered_display(chunks)), strip_punctuation(text))

    def test_pure_cjk_run_wraps_at_the_display_token_limit(self):
        text = "我们今天来讲一下极值位和行权价的关系因为很多做现货的兄弟都知道不同的标的之间是互相有影响的"
        words = [
            "我们", "今天", "来讲", "一下", "极值位", "和", "行权价", "的", "关系", "因为",
            "很多", "做", "现货", "的", "兄弟", "都", "知道", "不同", "的", "标的",
            "之间", "是", "互相", "有", "影响", "的",
        ]
        chunks = chunks_for(text, words)
        self.assertGreater(len(chunks), 1)
        self.assertEqual(covered_words(chunks), [word.lower() for word in words])
        for chunk in chunks:
            self.assertLessEqual(len(chunk.source_display), 18)

    def test_pure_cjk_segment_stays_intact(self):
        words = ["我们", "今天", "来讲", "期权"]
        chunks = chunks_for("我们今天来讲期权", words)
        self.assertEqual(covered_words(chunks), [word.lower() for word in words])
        self.assertEqual(covered_display(chunks), "我们今天来讲期权")

    def test_latin_segments_keep_their_existing_split(self):
        text = "If price breaks above the prior day high, we do not chase the move; the setup is simple."
        words = [word for word in text.replace(",", " ,").replace(";", " ;").replace(".", " .").split() if word]
        chunks = chunks_for(text, words)
        self.assertEqual(
            [(chunk.source_raw, chunk.source_display) for chunk in chunks],
            [
                ("if price breaks above the prior day high", "If price breaks above the prior day high"),
                ("we do not chase the move", "we do not chase the move"),
                ("the setup is simple", "the setup is simple"),
            ],
        )

    def test_no_word_is_lost_across_a_mixed_language_corpus(self):
        corpus = [
            (["这个", "GEX", "的", "极值位", "就是", "最大", "的值"], "这个GEX的极值位就是最大的值"),
            (["Volume", "就是", "日内", "的", "期权", "流量"], "Volume就是日内的期权流量"),
            (["所有", "GEX", "的", "计算"], "所有GEX的计算"),
            (["比如", "GEX", "的", "极值位"], "比如GEX的极值位"),
            (["美东", "时间", "8", "点半", "公布", "一次"], "美东时间8点半公布一次"),
            (["是", "滞后", "的", "不准", "的"], "是滞后的不准的"),
            (["GAMMA", "比较", "狂暴", "所以", "它", "的", "变化", "幅度", "很大"], "GAMMA比较狂暴所以它的变化幅度很大"),
            (["当然", "有些", "数据", "很多", "标签", "是", "没有", "的"], "当然有些数据很多标签是没有的"),
        ]
        for words, text in corpus:
            with self.subTest(text=text):
                chunks = chunks_for(text, words)
                self.assertEqual(covered_words(chunks), [word.lower() for word in words])
                self.assertTrue(covered_display(chunks))

    def test_unalignable_segment_falls_back_to_whole_segment_instead_of_truncating(self):
        # Display text shares nothing with the word table: every word must still
        # be owned by exactly one chunk.
        words = ["alpha", "beta", "gamma"]
        chunks = chunks_for("完全无关的显示文本", words)
        self.assertEqual(covered_words(chunks), words)


if __name__ == "__main__":
    unittest.main()
