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

    One asynchronous filetrans job per video, fed from Model Studio's own
    temporary storage: no chunking and no third-party file host. Stale wording
    here would misstate what the user is consenting to.
    """

    STALE_PHRASES = (
        "uploaded to `https://www.okfile.com`",
        "uploaded only to `https://www.okfile.com`",
        "按 5 分钟上限切块",
        "逐块以内联 Base64",
        "OKFILE_TOKEN",
        "转写固定为 OkFile +",
    )

    def test_questionnaire_consent_matches_the_filetrans_transport(self) -> None:
        result = subprocess.run(
            [sys.executable, str(ROOT / "skills/video-translate/scripts/preflight.py")],
            capture_output=True,
            text=True,
            check=True,
        )
        text = result.stdout
        self.assertIn("临时存储", text)
        self.assertIn("48 小时", text)
        self.assertIn("文件转写", text)
        self.assertNotIn("切块", text, "the transcript is one job, not chunks")
        self.assertNotIn("Fun-ASR", text, "the questionnaire must not name a retired model")

    def test_first_use_intro_names_only_the_alibaba_credentials(self) -> None:
        # The intro is printed only before initialization, so assert the constant.
        sys.path.insert(0, str(ROOT / "skills/video-translate/scripts"))
        import preflight

        intro = preflight.FIRST_USE_INTRO
        self.assertIn("阿里百炼（必需）", intro)
        self.assertIn("临时存储", intro)
        self.assertNotIn("OkFile", intro)
        self.assertNotIn("Fun-ASR", intro)

    def test_docs_do_not_describe_a_removed_transport(self) -> None:
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
                    self.assertNotIn(phrase, body, f"{path.name} still describes a removed transport")


class RemovedTransportGuardTest(unittest.TestCase):
    """OkFile is gone for good: the audio goes straight into Model Studio storage.

    Nothing shipped may mention the removed file host again, so a regression here
    fails the suite instead of quietly reintroducing a second transport.
    """

    TEXT_SUFFIXES = {".md", ".py", ".sh", ".yaml", ".yml", ".json", ".txt"}

    def test_shipped_skill_and_readme_never_mention_okfile(self):
        package = ROOT / "skills" / "video-translate"
        targets = [p for p in package.rglob("*") if p.is_file() and p.suffix in self.TEXT_SUFFIXES]
        targets.append(ROOT / "README.md")
        self.assertTrue(targets, "expected files to scan")
        for path in targets:
            body = path.read_text(encoding="utf-8", errors="ignore").lower()
            with self.subTest(path=str(path.relative_to(ROOT))):
                self.assertNotIn("okfile", body, f"{path.relative_to(ROOT)} still references the removed file host")

    def test_credentials_are_the_two_alibaba_values(self):
        sys.path.insert(0, str(ROOT / "skills/video-translate/scripts"))
        import common

        self.assertEqual(common.CREDENTIAL_KEYS, ("DASHSCOPE_API_KEY", "ALIYUN_WORKSPACE_ID"))


if __name__ == "__main__":
    unittest.main()
