import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PreflightQuestionnaireTest(unittest.TestCase):
    def test_questionnaire_separates_target_from_bilingual_output(self) -> None:
        result = subprocess.run(
            [sys.executable, str(ROOT / "skills/video-translate/scripts/preflight.py")],
            capture_output=True,
            text=True,
            check=True,
        )
        text = result.stdout
        self.assertIn("默认翻译为简体中文", text)
        self.assertIn("固定输出中文在上、原文在下的双语 ASS 和 SRT", text)
        self.assertIn("qwen-mt-plus", text)
        self.assertIn("Agent 大模型翻译", text)
        self.assertNotIn("说明书", text, "the product guide was removed from the repository")
        self.assertNotIn("GPT-5.6", text, "no model recommendation: it goes stale quickly")
        self.assertIn("请提供本地视频文件路径或存有视频的文件夹路径", text)
        self.assertIn("字幕文件命名：默认从原视频文件名或媒体项目目录提取真实标题", text)
        self.assertNotIn("项目根目录与 `.work/input/`", text)
        self.assertNotIn("音频型 WebM", text)
        self.assertNotIn("下载质量", text)

    def test_questionnaire_acknowledges_an_already_resolved_video(self) -> None:
        video = "/tmp/Exactly How To Trade.mp4"
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "skills/video-translate/scripts/preflight.py"),
                "--resolved-video",
                video,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        text = result.stdout
        self.assertIn(f"已经识别到该路径中的视频文件：`{video}`", text)
        self.assertNotIn("请提供本地视频文件路径或存有视频的文件夹路径", text)


class TransportWordingTest(unittest.TestCase):
    """The questionnaire and the docs must describe the transport that actually runs.

    The default path sends audio inline in five-minute chunks; OkFile only serves
    the explicit filetrans fallback. Stale wording here would misstate what the
    user is consenting to.
    """

    STALE_PHRASES = (
        "the selected audio is uploaded to `https://www.okfile.com`",
        "audio is uploaded only to `https://www.okfile.com`",
        "转写固定为 OkFile +",
        "当前生产主线固定为 OkFile +",
        "OkFile + Fun-ASR",
    )

    def test_questionnaire_consent_matches_the_inline_transport(self) -> None:
        result = subprocess.run(
            [sys.executable, str(ROOT / "skills/video-translate/scripts/preflight.py")],
            capture_output=True,
            text=True,
            check=True,
        )
        text = result.stdout
        self.assertIn("内联", text)
        self.assertIn("切块", text)
        self.assertIn("不会上传到任何公网中转", text)
        self.assertNotIn("Fun-ASR", text, "the questionnaire must not name a retired model")

    def test_first_use_intro_marks_okfile_as_optional(self) -> None:
        # The intro is printed only before initialization, so assert the constant.
        sys.path.insert(0, str(ROOT / "skills/video-translate/scripts"))
        import preflight

        intro = preflight.FIRST_USE_INTRO
        self.assertIn("内联", intro)
        self.assertIn("切块", intro)
        self.assertIn("OkFile（可选，仅兜底）", intro)
        self.assertNotIn("Fun-ASR", intro, "the intro must not name a retired model")

    def test_docs_do_not_claim_the_default_uploads_to_okfile(self) -> None:
        targets = [
            ROOT / "skills/video-translate/SKILL.md",
            ROOT / "skills/video-translate/references/execution-contract.md",
            ROOT / "README.md",
            ROOT / "skills/video-translate/scripts/preflight.py",
        ]
        for path in targets:
            body = path.read_text(encoding="utf-8")
            for phrase in self.STALE_PHRASES:
                with self.subTest(path=path.name, phrase=phrase):
                    self.assertNotIn(phrase, body, f"{path.name} still describes the old transport")


if __name__ == "__main__":
    unittest.main()
