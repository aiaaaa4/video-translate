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
        self.assertIn("视频翻译工作流说明书", text)
        self.assertIn("GPT-5.6", text)
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


if __name__ == "__main__":
    unittest.main()
