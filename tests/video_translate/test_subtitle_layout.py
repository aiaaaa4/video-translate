import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = ROOT / "skills" / "video-translate" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from common import split_chinese_line, split_source_line  # noqa: E402
from export_subtitles import ass_text, srt_text  # noqa: E402
from media_probe import VideoGeometry  # noqa: E402
from subtitle_layout import (  # noqa: E402
    PROFILES,
    bind_subtitle_layout,
    classify_layout,
    rebind_finalized_subtitle_layout,
    resolve_layout,
)


class SubtitleLayoutTest(unittest.TestCase):
    def test_classifies_display_geometry_after_rotation(self):
        self.assertEqual(classify_layout(VideoGeometry(1920, 1080, 90, 1080, 1920)), "portrait")
        self.assertEqual(classify_layout(VideoGeometry(1080, 1080, 0, 1080, 1080)), "square")
        self.assertEqual(classify_layout(VideoGeometry(1920, 1080, 0, 1920, 1080)), "landscape")

    @mock.patch("subtitle_layout.probe_video_geometry")
    def test_auto_layout_uses_displayed_orientation(self, probe):
        probe.return_value = VideoGeometry(1920, 1080, 90, 1080, 1920)
        profile, geometry = resolve_layout(Path("/tmp/video.mp4"))
        self.assertEqual(profile.name, "portrait")
        self.assertEqual(geometry.rotation, 90)

    @mock.patch("subtitle_layout.probe_video_geometry")
    def test_layout_binding_cannot_change_mid_run(self, probe):
        probe.return_value = VideoGeometry(1080, 1920, 0, 1080, 1920)
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            media = work_dir / "video.mp4"
            media.write_bytes(b"video")
            self.assertEqual(bind_subtitle_layout(work_dir, media).name, "portrait")
            with self.assertRaisesRegex(RuntimeError, "different subtitle layout"):
                bind_subtitle_layout(work_dir, media, "landscape")

    @mock.patch("subtitle_layout.probe_video_geometry")
    def test_finalized_media_path_rebind_requires_identical_geometry(self, probe):
        probe.return_value = VideoGeometry(1080, 1920, 0, 1080, 1920)
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            original = work_dir / "English title.mp4"
            original.write_bytes(b"video")
            bind_subtitle_layout(work_dir, original)
            localized = work_dir / "中文标题.mp4"
            original.rename(localized)

            rebound = rebind_finalized_subtitle_layout(work_dir, localized)

            self.assertEqual(rebound.name, "portrait")
            self.assertIn(str(localized.resolve()), (work_dir / "subtitle_layout.json").read_text(encoding="utf-8"))
            probe.return_value = VideoGeometry(1920, 1080, 0, 1920, 1080)
            with self.assertRaisesRegex(RuntimeError, "geometry or subtitle profile changed"):
                rebind_finalized_subtitle_layout(work_dir, localized)

    def test_portrait_chinese_wrap_prefers_clause_boundaries(self):
        lines = split_chinese_line(
            "你就会开始反思：究竟是我自身的什么问题造成了这种结果？",
            17.0,
            0.5,
            prefer_semantic_breaks=True,
        )
        self.assertEqual(lines, ["你就会开始反思：", "究竟是我自身的什么问题", "造成了这种结果？"])

    def test_portrait_source_wrap_is_deterministic(self):
        lines = split_source_line(
            "But once you have an objectively measurable edge and you still cannot realize its expected returns.",
            27.0,
        )
        self.assertGreaterEqual(len(lines), 2)
        self.assertEqual(" ".join(lines), "But once you have an objectively measurable edge and you still cannot realize its expected returns.")

    def test_portrait_ass_uses_fixed_top_anchor_and_final_profile(self):
        segments = [
            {
                "start": 0,
                "end": 4,
                "translation": "你就会开始反思：究竟是我自身的什么问题造成了这种结果？",
                "source_display": "You will begin to reflect on what issue in yourself caused this result.",
            }
        ]
        ass = ass_text(segments, False, PROFILES["portrait"])
        srt = srt_text(segments, False, PROFILES["portrait"])
        self.assertIn("PlayResX: 1080", ass)
        self.assertIn("PlayResY: 1920", ass)
        self.assertIn("Style: Default,Arial,64,", ass)
        self.assertIn(",3,1,8,80,80,1370,1", ass)
        self.assertIn(r"{\an8\pos(540,1370)}{\fs64}", ass)
        self.assertIn(r"{\fs34}", ass)
        self.assertIn("你就会开始反思：\n究竟是我自身的什么问题\n造成了这种结果？", srt)


if __name__ == "__main__":
    unittest.main()
