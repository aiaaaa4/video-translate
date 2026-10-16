import sys
import tempfile
import unittest
import hashlib
import json
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = ROOT / "skills" / "video-translate" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from transcribe_api import extract_audio, should_delete_prepared_audio, validate_workflow_audio  # noqa: E402
from generate_segments_with_dashscope import build_chunks  # noqa: E402
from source_subtitle_reference import load_source_subtitle, references_by_asr_segment  # noqa: E402
from video_to_subtitles import (  # noqa: E402
    bind_output_naming,
    bind_translation_provider,
    clean_video_title,
    cleanup_workflow_inputs,
    default_subtitle_tag,
    resolve_localized_output_title,
    resolve_asr_media,
    inspect_media_input,
    resolve_media_input,
    resolve_source_subtitle,
    finalize_visible_assets,
    validate_delivery_plan,
    ensure_ai_segments,
    main,
    validate_subtitle_tag,
)
from finalize_run import rebind_finalized_media  # noqa: E402


class MediaInputTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_reuses_direct_audio_without_ffmpeg(self):
        audio = self.tmp_dir / "lesson.m4a"
        audio.write_bytes(b"audio")
        self.assertEqual(extract_audio(audio, self.tmp_dir / "out"), audio)

    def test_never_deletes_caller_supplied_audio(self):
        audio = self.tmp_dir / "lesson.m4a"
        audio.write_bytes(b"audio")
        self.assertFalse(should_delete_prepared_audio(audio, audio, keep_audio=False))

    def test_deletes_only_audio_generated_from_video(self):
        video = self.tmp_dir / "lesson.mp4"
        generated = self.tmp_dir / "transcript" / "api_upload_audio.mp3"
        video.write_bytes(b"video")
        generated.parent.mkdir()
        generated.write_bytes(b"audio")
        self.assertTrue(should_delete_prepared_audio(video, generated, keep_audio=False))
        self.assertFalse(should_delete_prepared_audio(video, generated, keep_audio=True))

    def test_user_facing_workflow_rejects_direct_audio(self):
        audio = self.tmp_dir / "lesson.m4a"
        audio.write_bytes(b"audio")
        old_argv = sys.argv
        try:
            sys.argv = ["video_to_subtitles.py", str(audio), "--confirm-external-processing"]
            self.assertEqual(main(), 2)
        finally:
            sys.argv = old_argv

    def test_local_input_inspection_does_not_require_external_consent(self):
        video = self.tmp_dir / "lesson.mp4"
        video.write_bytes(b"video")
        old_argv = sys.argv
        try:
            with mock.patch("video_to_subtitles.resolve_layout") as resolve_layout_mock:
                from media_probe import VideoGeometry
                from subtitle_layout import PROFILES

                resolve_layout_mock.return_value = (PROFILES["landscape"], VideoGeometry(1920, 1080, 0, 1920, 1080))
                sys.argv = ["video_to_subtitles.py", str(video), "--inspect-inputs"]
                self.assertEqual(main(), 0)
        finally:
            sys.argv = old_argv

    def test_directory_input_selects_its_only_video(self):
        project = self.tmp_dir / "English Project"
        project.mkdir()
        video = project / "lesson.mp4"
        video.write_bytes(b"video")
        selected, selected_project, was_directory = resolve_media_input(project)
        self.assertEqual(selected, video.resolve())
        self.assertEqual(selected_project, project.resolve())
        self.assertTrue(was_directory)

    def test_directory_input_rejects_multiple_videos(self):
        project = self.tmp_dir / "English Project"
        project.mkdir()
        (project / "one.mp4").write_bytes(b"video")
        (project / "two.mkv").write_bytes(b"video")
        with self.assertRaisesRegex(RuntimeError, "Multiple video files"):
            resolve_media_input(project)

    def test_transcribe_helper_rejects_direct_audio_without_internal_marker(self):
        from transcribe_api import main as transcribe_main

        audio = self.tmp_dir / "lesson.m4a"
        audio.write_bytes(b"audio")
        old_argv = sys.argv
        try:
            sys.argv = ["transcribe_api.py", str(audio), "--confirm-external-processing"]
            self.assertEqual(transcribe_main(), 2)
        finally:
            sys.argv = old_argv

    def test_workflow_audio_must_match_video_name_and_project(self):
        video = self.tmp_dir / "lesson.mp4"
        video.write_bytes(b"video")
        hidden = self.tmp_dir / ".work" / "input" / "lesson.m4a"
        hidden.parent.mkdir(parents=True)
        hidden.write_bytes(b"audio")
        self.assertEqual(validate_workflow_audio(hidden, video), hidden.resolve())

        unrelated = self.tmp_dir / ".work" / "input" / "other.m4a"
        unrelated.write_bytes(b"audio")
        with self.assertRaisesRegex(RuntimeError, "sole audio"):
            validate_workflow_audio(unrelated, video)

    def test_unique_different_basename_project_audio_is_allowed(self):
        project = self.tmp_dir / "project"
        project.mkdir()
        video = project / "lesson.mp4"
        audio = project / "source-audio.m4a"
        video.write_bytes(b"video")
        audio.write_bytes(b"audio")
        self.assertEqual(resolve_asr_media(video), audio)
        self.assertEqual(validate_workflow_audio(audio, video), audio.resolve())

    def test_prefers_same_basename_downloaded_audio(self):
        video = self.tmp_dir / "lesson.mp4"
        audio = self.tmp_dir / "lesson.m4a"
        video.write_bytes(b"video")
        audio.write_bytes(b"audio")
        self.assertEqual(resolve_asr_media(video), audio)

    def test_prefers_hidden_combined_workflow_audio(self):
        video = self.tmp_dir / "lesson.mp4"
        hidden = self.tmp_dir / ".work" / "input" / "lesson.m4a"
        hidden.parent.mkdir(parents=True)
        video.write_bytes(b"video")
        hidden.write_bytes(b"audio")
        self.assertEqual(resolve_asr_media(video), hidden)

    @mock.patch("media_probe.media_stream_types", return_value={"audio"})
    @mock.patch("video_to_subtitles.resolve_layout")
    def test_discovers_audio_only_webm_and_source_subtitle_in_hidden_input(self, layout_probe, _probe):
        video = self.tmp_dir / "Trading Edge.mp4"
        hidden_dir = self.tmp_dir / ".work" / "input"
        hidden_dir.mkdir(parents=True)
        audio = hidden_dir / "Trading Edge.ASR校对音频.webm"
        subtitle = hidden_dir / "Trading Edge.原语言字幕.en-orig.srt"
        video.write_bytes(b"video")
        audio.write_bytes(b"audio-only-webm")
        subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")

        from media_probe import VideoGeometry
        from subtitle_layout import PROFILES

        layout_probe.return_value = (PROFILES["landscape"], VideoGeometry(1920, 1080, 0, 1920, 1080))
        discovered = inspect_media_input(self.tmp_dir)

        self.assertEqual(discovered["video"], str(video.resolve()))
        self.assertEqual(discovered["audio"], str(audio.resolve()))
        self.assertEqual(discovered["audio_action"], "reuse_project_audio")
        self.assertEqual(discovered["source_subtitle"], str(subtitle.resolve()))
        self.assertEqual(extract_audio(audio, self.tmp_dir / "transcript"), audio)

    @mock.patch("media_probe.media_stream_types", return_value={"audio"})
    def test_audio_only_webm_is_rejected_as_direct_video_input(self, _probe):
        audio = self.tmp_dir / "audio-only.webm"
        audio.write_bytes(b"audio")
        with self.assertRaisesRegex(RuntimeError, "direct audio input"):
            resolve_media_input(audio)

    @mock.patch("media_probe.media_stream_types", return_value={"audio", "video"})
    def test_webm_with_video_stream_is_accepted_as_video_input(self, _probe):
        video = self.tmp_dir / "lesson.webm"
        video.write_bytes(b"video")
        selected, project, was_directory = resolve_media_input(video)
        self.assertEqual(selected, video.resolve())
        self.assertEqual(project, self.tmp_dir.resolve())
        self.assertFalse(was_directory)

    def test_discovers_and_cleans_hidden_source_inputs(self):
        video = self.tmp_dir / "lesson.mp4"
        hidden_dir = self.tmp_dir / ".work" / "input"
        hidden_dir.mkdir(parents=True)
        audio = hidden_dir / "lesson.m4a"
        subtitle = hidden_dir / "lesson.原语言字幕.srt"
        video.write_bytes(b"video")
        audio.write_bytes(b"audio")
        subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
        self.assertEqual(resolve_source_subtitle(video, None), subtitle.resolve())
        removed = cleanup_workflow_inputs(video, [audio, subtitle])
        self.assertEqual(set(removed), {audio.resolve(), subtitle.resolve()})
        self.assertFalse(hidden_dir.exists())

    def test_discovers_same_basename_source_subtitle_beside_video(self):
        video = self.tmp_dir / "lesson.mp4"
        subtitle = self.tmp_dir / "lesson.srt"
        video.write_bytes(b"video")
        subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
        self.assertEqual(resolve_source_subtitle(video, None), subtitle.resolve())

    def test_discovers_unique_different_basename_project_subtitle(self):
        video = self.tmp_dir / "lesson.mp4"
        subtitle = self.tmp_dir / "platform-source.en.srt"
        video.write_bytes(b"video")
        subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
        self.assertEqual(resolve_source_subtitle(video, None), subtitle.resolve())

    def test_matching_audio_candidates_must_be_unambiguous(self):
        video = self.tmp_dir / "lesson.mp4"
        video.write_bytes(b"video")
        (self.tmp_dir / "lesson.m4a").write_bytes(b"audio")
        (self.tmp_dir / "lesson.mp3").write_bytes(b"audio")
        with self.assertRaisesRegex(RuntimeError, "Multiple matching audio"):
            resolve_asr_media(video)

    def test_cleanup_never_deletes_external_user_inputs(self):
        video = self.tmp_dir / "lesson.mp4"
        external_audio = self.tmp_dir / "lesson.m4a"
        external_subtitle = self.tmp_dir / "lesson.srt"
        video.write_bytes(b"video")
        external_audio.write_bytes(b"audio")
        external_subtitle.write_text("subtitle", encoding="utf-8")
        self.assertEqual(cleanup_workflow_inputs(video, [external_audio, external_subtitle]), [])
        self.assertTrue(external_audio.exists())
        self.assertTrue(external_subtitle.exists())

    def test_source_subtitle_corrects_display_but_preserves_asr_raw_words(self):
        subtitle = self.tmp_dir / "source.srt"
        subtitle.write_text(
            "1\n00:00:00,000 --> 00:00:04,500\n"
            "If price breaks above the prior day high, we do not chase the move.\n",
            encoding="utf-8",
        )
        transcript = {
            "segments": [
                {
                    "start": 0.0,
                    "end": 4.5,
                    "text": "If price breaks above the prior day hi we do not chase the move",
                    "words": [
                        {"word": word}
                        for word in "If price breaks above the prior day hi we do not chase the move".split()
                    ],
                }
            ]
        }
        cues = load_source_subtitle(subtitle)
        references = references_by_asr_segment(transcript, cues)
        chunks = build_chunks(transcript, 18, references)
        self.assertTrue(any(chunk.reference_used for chunk in chunks))
        self.assertIn("high", " ".join(chunk.source_display for chunk in chunks))
        self.assertEqual(
            " ".join(chunk.source_raw for chunk in chunks),
            "if price breaks above the prior day hi we do not chase the move",
        )

    def test_webvtt_minute_timestamp_is_supported(self):
        subtitle = self.tmp_dir / "source.vtt"
        subtitle.write_text("WEBVTT\n\n00:01.500 --> 00:03.000\nHello world\n", encoding="utf-8")
        cues = load_source_subtitle(subtitle)
        self.assertEqual(len(cues), 1)
        self.assertEqual((cues[0].start, cues[0].end, cues[0].text), (1.5, 3.0, "Hello world"))

    def test_unrelated_source_subtitle_falls_back_to_asr_text(self):
        transcript = {
            "segments": [
                {
                    "start": 0.0,
                    "end": 2.0,
                    "text": "Price broke above resistance",
                    "words": [{"word": word} for word in "Price broke above resistance".split()],
                }
            ]
        }
        chunks = build_chunks(transcript, 18, {0: "Completely unrelated cooking instructions"})
        self.assertFalse(any(chunk.reference_used for chunk in chunks))
        self.assertIn("Price broke above resistance", chunks[0].source_display)

    def test_reference_cue_is_cropped_to_each_fun_asr_segment(self):
        transcript = {
            "segments": [
                {
                    "start": 0.0,
                    "end": 2.0,
                    "text": "with this strategy correct",
                    "words": [{"word": word} for word in "with this strategy correct".split()],
                },
                {
                    "start": 2.0,
                    "end": 6.0,
                    "text": "there's been plenty of times that the first red day setup comes",
                    "words": [
                        {"word": word}
                        for word in "there's been plenty of times that the first red day setup comes".split()
                    ],
                },
                {
                    "start": 6.0,
                    "end": 8.0,
                    "text": "I'm ready for it",
                    "words": [{"word": word} for word in "I'm ready for it".split()],
                },
            ]
        }
        cue = load_source_subtitle(
            self._write_subtitle(
                "polluted.srt",
                "1\n00:00:00,000 --> 00:00:08,000\n"
                "with this strategy correct there's been plenty of times "
                "that the first red day setup comes I'm\n",
            )
        )
        references = references_by_asr_segment(transcript, cue)
        chunks = build_chunks(transcript, 60, references)
        middle = next(chunk for chunk in chunks if chunk.source_raw.startswith("there's been plenty"))
        self.assertEqual(
            middle.source_display,
            "there's been plenty of times that the first red day setup comes",
        )
        self.assertNotIn("with this strategy", middle.source_display)
        self.assertNotIn("I'm", middle.source_display)

    def test_anomalous_reference_source_is_disabled_globally(self):
        transcript = {
            "segments": [
                {
                    "start": float(index),
                    "end": float(index + 1),
                    "text": f"source words {index}",
                    "words": [{"word": word} for word in f"source words {index}".split()],
                }
                for index in range(4)
            ]
        }
        stats = {}
        chunks = build_chunks(
            transcript,
            18,
            {index: f"unrelated cooking reference {index}" for index in range(4)},
            stats,
        )
        self.assertTrue(stats["reference_disabled"])
        self.assertEqual(stats["status"], "disabled_as_anomalous")
        self.assertFalse(any(chunk.reference_used for chunk in chunks))

    def test_reference_requires_sufficient_temporal_overlap(self):
        transcript = {
            "segments": [
                {
                    "start": 0.0,
                    "end": 4.0,
                    "text": "hello world",
                    "words": [{"word": "hello"}, {"word": "world"}],
                }
            ]
        }
        cues = load_source_subtitle(
            self._write_subtitle(
                "weak-overlap.srt",
                "1\n00:00:03,900 --> 00:00:05,000\nHello world\n",
            )
        )
        self.assertEqual(references_by_asr_segment(transcript, cues), {})

    def test_existing_segments_reject_changed_source_reference(self):
        work_dir = self.tmp_dir / "work"
        transcript_dir = self.tmp_dir / "transcript"
        work_dir.mkdir()
        transcript_dir.mkdir()
        subtitle = self.tmp_dir / "source.srt"
        subtitle.write_text("reference v1", encoding="utf-8")
        translation_context = self.tmp_dir / "translation-context.json"
        translation_context.write_text(
            json.dumps({"stage": "translation-context", "domains": "test", "terms": [], "tm_list": []}),
            encoding="utf-8",
        )
        (work_dir / "segments.txt").write_text("segments", encoding="utf-8")
        (work_dir / "segment_generation_meta.json").write_text(
            json.dumps(
                {
                    "source_subtitle": str(subtitle),
                    "source_subtitle_sha256": hashlib.sha256(subtitle.read_bytes()).hexdigest(),
                    "translation_context_sha256": hashlib.sha256(translation_context.read_bytes()).hexdigest(),
                }
            ),
            encoding="utf-8",
        )
        ensure_ai_segments(work_dir, transcript_dir, "en", "test", subtitle, translation_context)
        subtitle.write_text("reference v2", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "different source subtitle"):
            ensure_ai_segments(work_dir, transcript_dir, "en", "test", subtitle, translation_context)

    def test_run_cannot_mix_translation_providers(self):
        work_dir = self.tmp_dir / "work"
        work_dir.mkdir()
        bind_translation_provider(work_dir, "agent")
        with self.assertRaisesRegex(RuntimeError, "already bound"):
            bind_translation_provider(work_dir, "qwen-mt-plus")

    def test_legacy_qwen_metadata_prevents_agent_switch(self):
        work_dir = self.tmp_dir / "work"
        work_dir.mkdir()
        (work_dir / "segment_generation_meta.json").write_text(
            json.dumps({"model": "qwen-mt-plus"}),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(RuntimeError, "translation_provider=qwen-mt-plus"):
            bind_translation_provider(work_dir, "agent")

    def test_keeps_video_when_no_downloaded_audio_exists(self):
        video = self.tmp_dir / "lesson.mp4"
        video.write_bytes(b"video")
        self.assertEqual(resolve_asr_media(video), video)

    def test_final_delivery_renames_project_video_and_records_outputs(self):
        project = self.tmp_dir / "2026-03-15 - English title [_abc123]"
        project.mkdir()
        video = project / "2026-03-15 - English title [_abc123].mp4"
        video.write_bytes(b"video")
        run_dir = project / ".work" / "run"
        (run_dir / "work").mkdir(parents=True)
        output_base = "中文标题.中英双语字幕"
        validate_delivery_plan(video, project, "中文标题", project, output_base, False)
        for extension in ("ass", "srt"):
            (project / f"{output_base}.{extension}").write_text("subtitle", encoding="utf-8")
        finalized = finalize_visible_assets(
            video,
            project,
            "中文标题",
            project,
            output_base,
            run_dir,
        )
        final_project = (self.tmp_dir / "中文标题").resolve()
        self.assertEqual(finalized["project_dir"], final_project)
        self.assertTrue((final_project / "中文标题.mp4").is_file())
        self.assertTrue((final_project / f"{output_base}.ass").is_file())
        self.assertTrue((final_project / f"{output_base}.srt").is_file())
        manifest = json.loads(
            (final_project / ".work" / "run" / "work" / "delivery_manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["project_directory"], str(final_project))
        self.assertEqual(manifest["media"], str(final_project / "中文标题.mp4"))

    def test_finalized_run_can_rebind_to_recorded_localized_media(self):
        work_dir = self.tmp_dir / "project" / ".work" / "run" / "work"
        work_dir.mkdir(parents=True)
        final_project = self.tmp_dir / "中文标题"
        final_project.mkdir()
        media = final_project / "中文标题.mp4"
        media.write_bytes(b"video")
        output_base = "中文标题.中英双语字幕"
        for extension in ("ass", "srt"):
            (final_project / f"{output_base}.{extension}").write_text("subtitle", encoding="utf-8")
        naming = {
            "schema_version": 1,
            "media": str(self.tmp_dir / "old-project" / "English title.mp4"),
            "localized_title": "中文标题",
            "title_source": "explicit_localized_title",
            "output_tag": "中英双语字幕",
            "output_base": output_base,
        }
        (work_dir / "output_naming.json").write_text(json.dumps(naming), encoding="utf-8")
        (work_dir / "delivery_manifest.json").write_text(
            json.dumps(
                {
                    "localized_title": "中文标题",
                    "project_directory": str(final_project),
                    "media": str(media),
                    "ass": str(final_project / f"{output_base}.ass"),
                    "srt": str(final_project / f"{output_base}.srt"),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        rebound = rebind_finalized_media(work_dir, media, naming)
        self.assertEqual(rebound["media"], str(media))
        self.assertEqual(json.loads((work_dir / "output_naming.json").read_text(encoding="utf-8"))["media"], str(media))

    def test_delivery_refuses_shared_or_multi_video_directory(self):
        project = self.tmp_dir / "Downloads"
        project.mkdir()
        video = project / "lesson.mp4"
        video.write_bytes(b"video")
        with self.assertRaisesRegex(RuntimeError, "shared"):
            validate_delivery_plan(video, project, "中文标题", project, "中文标题.中英双语字幕", False)
        (project / "other.mp4").write_bytes(b"video")
        with self.assertRaisesRegex(RuntimeError, "single-video"):
            validate_delivery_plan(video, project, "中文标题", project, "中文标题.中英双语字幕", False)

    def test_delivery_requires_explicit_subtitle_overwrite(self):
        project = self.tmp_dir / "English project"
        project.mkdir()
        video = project / "lesson.mp4"
        video.write_bytes(b"video")
        output_base = "中文标题.中英双语字幕"
        (project / f"{output_base}.srt").write_text("existing", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "overwrite was not confirmed"):
            validate_delivery_plan(video, project, "中文标题", project, output_base, False)
        validate_delivery_plan(video, project, "中文标题", project, output_base, True)

    def test_uses_localized_subtitle_tags(self):
        self.assertEqual(default_subtitle_tag("en"), "中英双语字幕")
        self.assertEqual(default_subtitle_tag("fr"), "中法双语字幕")
        self.assertEqual(validate_subtitle_tag("中英双语字幕"), "中英双语字幕")
        with self.assertRaisesRegex(RuntimeError, "unsafe"):
            validate_subtitle_tag("../../原版视频")

    def test_cleans_download_date_and_platform_id_from_title(self):
        self.assertEqual(
            clean_video_title(
                "2026-03-15 - $100+ Million Trader - His BEST Trading Strategy "
                "(Market Wizard) [_qeFh1ADss8].mp4"
            ),
            "$100+ Million Trader - His BEST Trading Strategy (Market Wizard)",
        )
        self.assertEqual(clean_video_title("2026-03-15 - 最佳交易策略 [_abc123].发布版.mp4"), "最佳交易策略")

    def test_prefers_localized_project_directory_for_english_media_title(self):
        project = self.tmp_dir / "1亿+美元交易者：他最好的交易策略（市场奇才） [_qeFh1ADss8]"
        project.mkdir()
        media = project / (
            "2026-03-15 - $100+ Million Trader - His BEST Trading Strategy "
            "(Market Wizard) [_qeFh1ADss8].mp4"
        )
        media.write_bytes(b"video")
        self.assertEqual(
            resolve_localized_output_title(media, None),
            ("1亿+美元交易者：他最好的交易策略（市场奇才）", "project_directory"),
        )

    def test_generic_media_name_uses_real_localized_project_title(self):
        project = self.tmp_dir / "订单流交易策略 [abcDEF12345]"
        project.mkdir()
        media = project / "原版视频.mp4"
        media.write_bytes(b"video")
        self.assertEqual(
            resolve_localized_output_title(media, None),
            ("订单流交易策略", "project_directory"),
        )

    def test_english_title_requires_agent_localized_title(self):
        media = self.tmp_dir / "2026-03-15 - Best Trading Strategy [_qeFh1ADss8].mp4"
        media.write_bytes(b"video")
        with self.assertRaisesRegex(RuntimeError, "--localized-title"):
            resolve_localized_output_title(media, None)
        self.assertEqual(
            resolve_localized_output_title(media, "最佳交易策略"),
            ("最佳交易策略", "explicit_localized_title"),
        )

    def test_generic_localized_title_is_rejected(self):
        media = self.tmp_dir / "lesson.mp4"
        media.write_bytes(b"video")
        with self.assertRaisesRegex(RuntimeError, "generic label"):
            resolve_localized_output_title(media, "原版视频")

    def test_run_binds_output_naming(self):
        work_dir = self.tmp_dir / "work"
        work_dir.mkdir()
        media = self.tmp_dir / "lesson.mp4"
        media.write_bytes(b"video")
        bind_output_naming(
            work_dir,
            media.resolve(),
            "最佳交易策略",
            "explicit_localized_title",
            "中英双语字幕",
            "最佳交易策略.中英双语字幕",
        )
        with self.assertRaisesRegex(RuntimeError, "different output naming"):
            bind_output_naming(
                work_dir,
                media.resolve(),
                "另一标题",
                "explicit_localized_title",
                "中英双语字幕",
                "另一标题.中英双语字幕",
            )

    def _write_subtitle(self, name: str, text: str) -> Path:
        path = self.tmp_dir / name
        path.write_text(text, encoding="utf-8")
        return path


if __name__ == "__main__":
    unittest.main()
