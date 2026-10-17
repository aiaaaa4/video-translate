import base64
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = ROOT / "skills" / "video-translate" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import asr_sync_chunked as sync  # noqa: E402
import transcribe_api  # noqa: E402

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def words_for(duration_s: float, *, count: int = 3) -> list[dict]:
    """Words covering a chunk from 0 to its duration, in milliseconds."""
    span = int(duration_s * 1000)
    step = span // count
    generated = []
    for index in range(count):
        begin = index * step
        end = span if index == count - 1 else (index + 1) * step
        generated.append({"text": f"w{index}", "begin_ms": begin, "end_ms": end, "probability": 1.0})
    return generated


class ChunkPlanningTest(unittest.TestCase):
    def test_short_audio_is_a_single_window(self):
        windows = sync.plan_chunk_windows(60.0, 270.0, [])
        self.assertEqual(len(windows), 1)
        self.assertEqual((windows[0].start, windows[0].end), (0.0, 60.0))

    def test_long_audio_is_contiguous_and_never_exceeds_the_limit(self):
        duration = 1260.0  # 21 minutes
        windows = sync.plan_chunk_windows(duration, 270.0, [])
        self.assertEqual(windows[0].start, 0.0)
        self.assertEqual(windows[-1].end, duration)
        for window in windows:
            self.assertLessEqual(window.duration, 270.0 + 1e-9)
        for previous, current in zip(windows, windows[1:]):
            self.assertEqual(previous.end, current.start, "windows must not overlap or leave gaps")
        self.assertEqual([window.index for window in windows], list(range(len(windows))))

    def test_cut_lands_on_a_silence_before_the_target(self):
        windows = sync.plan_chunk_windows(600.0, 270.0, [(250.0, 260.0)])
        self.assertEqual(windows[0].end, 255.0)

    def test_silence_after_the_target_is_ignored(self):
        windows = sync.plan_chunk_windows(600.0, 270.0, [(275.0, 285.0)])
        self.assertEqual(windows[0].end, 270.0, "cutting after the target would exceed the model limit")

    def test_silence_outside_the_window_is_ignored(self):
        windows = sync.plan_chunk_windows(600.0, 270.0, [(100.0, 110.0)])
        self.assertEqual(windows[0].end, 270.0)

    def test_a_silence_too_close_to_the_previous_cut_is_skipped(self):
        # A 2 s chunk would be wasteful and could return almost nothing.
        windows = sync.plan_chunk_windows(600.0, 270.0, [(268.0, 270.0), (538.0, 540.0)])
        self.assertTrue(all(window.duration >= sync.MIN_CHUNK_SECONDS for window in windows))

    def test_invalid_arguments_are_rejected(self):
        with self.assertRaises(ValueError):
            sync.plan_chunk_windows(0.0, 270.0, [])
        with self.assertRaises(ValueError):
            sync.plan_chunk_windows(10.0, 0.0, [])


class SyncRequestTest(unittest.TestCase):
    def test_request_uses_an_inline_data_url_and_no_choices(self):
        payload = sync.build_sync_request(model="qwen-audio-3.1-asr-flash", audio_bytes=b"abc")
        self.assertEqual(payload["model"], "qwen-audio-3.1-asr-flash")
        self.assertNotIn("choices", payload)
        content = payload["input"]["messages"][0]["content"][0]
        self.assertEqual(content["type"], "input_audio")
        expected = "data:audio/mpeg;base64," + base64.b64encode(b"abc").decode("ascii")
        self.assertEqual(content["input_audio"]["data"], expected)
        self.assertEqual(payload["parameters"], {"format": "mp3", "sample_rate": "16000"})

    def test_hotwords_are_inlined_when_present(self):
        payload = sync.build_sync_request(
            model="qwen-audio-3.1-asr-flash", audio_bytes=b"abc", hotwords={"GEX": 4, "极值位": 50}
        )
        self.assertEqual(payload["parameters"]["vocabulary"], {"GEX": 4, "极值位": 50})

    def test_empty_audio_is_rejected(self):
        with self.assertRaises(ValueError):
            sync.build_sync_request(model="qwen-audio-3.1-asr-flash", audio_bytes=b"")


class SyncResponseTest(unittest.TestCase):
    def test_sentence_words_are_read_with_millisecond_times(self):
        payload = {
            "output": {
                "sentence": {
                    "begin_time": 200,
                    "end_time": 1000,
                    "text": "hello world",
                    "words": [
                        {"begin_time": 200, "end_time": 500, "text": "hello", "confidence": 0.9},
                        {"begin_time": 500, "end_time": 1000, "text": "world", "punctuation": "."},
                    ],
                },
                "text": "hello world",
            },
            "usage": {"duration": 1.2},
        }
        text, words, reported = sync.parse_sync_response(payload)
        self.assertEqual(text, "hello world")
        self.assertEqual([word["text"] for word in words], ["hello", "world"])
        self.assertEqual((words[0]["begin_ms"], words[1]["end_ms"]), (200, 1000))
        self.assertAlmostEqual(words[0]["probability"], 0.9)
        self.assertAlmostEqual(reported, 1.2)

    def test_a_sentences_list_is_also_supported(self):
        payload = {
            "output": {
                "sentences": [
                    {"text": "one", "words": [{"begin_time": 0, "end_time": 100, "text": "one"}]},
                    {"text": "two", "words": [{"begin_time": 100, "end_time": 200, "text": "two"}]},
                ]
            }
        }
        text, words, _reported = sync.parse_sync_response(payload)
        self.assertEqual(text, "one two")
        self.assertEqual(len(words), 2)

    def test_a_word_without_timestamps_is_an_error(self):
        payload = {"output": {"sentence": {"words": [{"text": "hello"}]}}}
        with self.assertRaises(RuntimeError):
            sync.parse_sync_response(payload)


class StitchingTest(unittest.TestCase):
    def test_offsets_are_applied_in_chunk_order(self):
        windows = [
            sync.ChunkWindow(0, 0.0, 10.0),
            sync.ChunkWindow(1, 10.0, 20.0),
        ]
        chunk_words = [
            [{"text": "a", "begin_ms": 0, "end_ms": 5000, "probability": 1.0},
             {"text": "b", "begin_ms": 5000, "end_ms": 10000, "probability": 1.0}],
            [{"text": "c", "begin_ms": 0, "end_ms": 10000, "probability": 1.0}],
        ]
        stitched = sync.stitch_words(chunk_words, windows)
        self.assertEqual([word["word"] for word in stitched], ["a", "b", "c"])
        self.assertEqual(stitched[2]["start"], 10.0)
        self.assertEqual(stitched[2]["end"], 20.0)

    def test_misaligned_inputs_are_rejected(self):
        with self.assertRaises(ValueError):
            sync.stitch_words([[{"text": "a", "begin_ms": 0, "end_ms": 1}]], [])


class WordStreamValidationTest(unittest.TestCase):
    def _words(self, spans, duration):
        return [
            {"word": f"w{index}", "start": start, "end": end, "probability": 1.0}
            for index, (start, end) in enumerate(spans)
        ]

    def test_a_clean_stream_passes(self):
        sync.validate_word_stream(self._words([(0.0, 5.0), (5.0, 10.0)], 10.0), 10.0)

    def test_out_of_order_words_are_rejected(self):
        with self.assertRaises(RuntimeError):
            sync.validate_word_stream(self._words([(5.0, 6.0), (1.0, 2.0)], 10.0), 10.0)

    def test_overlapping_words_are_rejected(self):
        with self.assertRaises(RuntimeError):
            sync.validate_word_stream(self._words([(0.0, 6.0), (5.0, 10.0)], 10.0), 10.0)

    def test_duplicate_words_are_rejected(self):
        with self.assertRaises(RuntimeError):
            sync.validate_word_stream(self._words([(0.0, 1.0), (0.0, 1.0)], 1.0), 1.0)

    def test_a_missing_tail_is_rejected(self):
        with self.assertRaises(RuntimeError):
            sync.validate_word_stream(self._words([(0.0, 5.0)], 30.0), 30.0)

    def test_a_missing_head_is_rejected(self):
        with self.assertRaises(RuntimeError):
            sync.validate_word_stream(self._words([(5.0, 10.0)], 10.0), 10.0)

    def test_an_empty_stream_is_rejected(self):
        with self.assertRaises(RuntimeError):
            sync.validate_word_stream([], 10.0)


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg and ffprobe are required")
class ChunkedSyncIntegrationTest(unittest.TestCase):
    """End-to-end over real ffmpeg with an injected HTTP layer."""

    def _tone_with_silence(self, target: Path) -> None:
        command = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=5",
            "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono:d=1.5",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=5.5",
            "-filter_complex", "[0:a][1:a][2:a]concat=n=3:v=0:a=1[out]",
            "-map", "[out]", "-ac", "1", "-ar", "16000", str(target),
        ]
        subprocess.run(command, check=True, capture_output=True)

    def test_chunked_transcription_stitches_a_complete_word_stream(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp).resolve()
            source = workdir / "source.mp3"
            self._tone_with_silence(source)
            duration = sync.probe_duration(source)
            windows = sync.plan_chunk_windows(duration, 5.0, [])
            self.assertGreater(len(windows), 1, "the fixture should need several chunks")

            calls: list[dict] = []

            def poster(url: str, api_key: str, payload: dict) -> dict:
                calls.append({"url": url, "api_key": api_key, "payload": payload})
                window = windows[len(calls) - 1]
                return {
                    "output": {
                        "sentence": {
                            "text": "sentence",
                            "words": [
                                {
                                    "begin_time": word["begin_ms"],
                                    "end_time": word["end_ms"],
                                    "text": word["text"],
                                    "confidence": 1.0,
                                }
                                for word in words_for(window.duration)
                            ],
                        }
                    },
                    "usage": {"duration": window.duration},
                }

            out_dir = workdir / "out"
            transcript = sync.transcribe_chunked_sync(
                media=source,
                out_dir=out_dir,
                model="qwen-audio-3.1-asr-flash",
                language="en",
                api_key="sk-test",
                base_url="https://example.test",
                hotwords={"GEX": 4},
                max_chunk_seconds=5.0,
                concurrency=1,
                retries=1,
                poster=poster,
            )

            self.assertEqual(len(calls), len(windows))
            self.assertTrue(all(call["url"].endswith(sync.SYNC_ENDPOINT_SUFFIX) for call in calls))
            self.assertEqual(calls[0]["payload"]["parameters"]["vocabulary"], {"GEX": 4})
            self.assertIn("data:audio/mpeg;base64,", calls[0]["payload"]["input"]["messages"][0]["content"][0]["input_audio"]["data"])

            self.assertEqual(transcript["provider"], "aliyun-qwen-audio-asr-sync")
            self.assertAlmostEqual(transcript["duration"], duration, places=1)
            self.assertEqual(len(transcript["segments"]), len(windows))
            # Stitching is validated inside the call; assert the observable result too.
            words = transcript["words"]
            self.assertTrue(words)
            for previous, current in zip(words, words[1:]):
                self.assertGreaterEqual(current["start"], previous["end"] - 0.05)
            self.assertLessEqual(words[0]["start"], 1.0)
            self.assertGreaterEqual(words[-1]["end"], duration - 1.0)

            self.assertFalse(
                (out_dir / "okfile_upload_response.json").exists(),
                "the sync path must not touch OkFile",
            )

    def test_retries_report_the_chunk_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp).resolve()
            source = workdir / "source.mp3"
            self._tone_with_silence(source)

            def failing(url: str, api_key: str, payload: dict) -> dict:
                raise RuntimeError("boom")

            with self.assertRaises(RuntimeError) as caught:
                sync.transcribe_chunked_sync(
                    media=source,
                    out_dir=workdir / "out",
                    model="qwen-audio-3.1-asr-flash",
                    language="en",
                    api_key="sk-test",
                    base_url="https://example.test",
                    max_chunk_seconds=20.0,
                    concurrency=1,
                    retries=2,
                    poster=failing,
                    sleep=lambda _seconds: None,
                )
            message = str(caught.exception)
            self.assertIn("Chunk 1/1", message)
            self.assertIn("boom", message)

    def test_a_short_file_is_sent_as_one_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp).resolve()
            source = workdir / "short.mp3"
            subprocess.run(
                ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
                 "-ac", "1", "-ar", "16000", str(source)],
                check=True, capture_output=True,
            )
            calls = []

            def poster(url: str, api_key: str, payload: dict) -> dict:
                calls.append(payload)
                return {
                    "output": {"sentence": {"text": "x", "words": [
                        {"begin_time": 0, "end_time": 4000, "text": "x"}]}},
                    "usage": {"duration": 4},
                }

            sync.transcribe_chunked_sync(
                media=source,
                out_dir=workdir / "out",
                model="qwen-audio-3.1-asr-flash",
                language="en",
                api_key="sk-test",
                base_url="https://example.test",
                max_chunk_seconds=270.0,
                concurrency=1,
                retries=1,
                poster=poster,
            )
            self.assertEqual(len(calls), 1, "a single sub-limit file must not be chunked")


class SyncRoutingTest(unittest.TestCase):
    def test_sync_path_never_uploads_to_okfile(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp).resolve()
            args = mock.Mock()
            args.media = out_dir / "source.mp4"
            args.out_dir = out_dir
            args.language = "en"
            args.keep_audio = False

            transcript = {
                "media": str(args.media),
                "provider": "aliyun-qwen-audio-asr-sync",
                "model": "qwen-audio-3.1-asr-flash",
                "language": "en",
                "duration": 1.0,
                "segments": [],
                "words": [{"word": "a", "start": 0.0, "end": 1.0, "probability": 1.0}],
            }
            with mock.patch.object(transcribe_api, "transcribe_chunked_sync", return_value=transcript), \
                 mock.patch.object(transcribe_api, "upload_audio_to_okfile") as upload_mock, \
                 mock.patch.object(transcribe_api, "write_srt"):
                exit_code = transcribe_api.transcribe_with_sync_model(
                    args, "sk-test", "qwen-audio-3.1-asr-flash", "llm-test", "cn-beijing", {"GEX": 4}
                )
            self.assertEqual(exit_code, 0)
            upload_mock.assert_not_called()
            self.assertTrue((out_dir / "transcript_words.json").exists())

    def test_the_sync_path_does_not_require_an_okfile_token(self):
        self.assertEqual(
            transcribe_api.required_credentials("qwen-audio-3.1-asr-flash"),
            ("DASHSCOPE_API_KEY", "ALIYUN_WORKSPACE_ID"),
        )
        self.assertIn("OKFILE_TOKEN", transcribe_api.required_credentials("fun-asr"))

    def test_the_default_model_is_the_synchronous_one(self):
        self.assertEqual(
            transcribe_api.PROVIDERS["aliyun-fun-asr"]["default_model"],
            "qwen-audio-3.1-asr-flash",
        )

    def test_the_chunk_limit_cannot_exceed_the_model_cap(self):
        args = mock.Mock(out_dir=Path("/tmp"), media=Path("/tmp/x.mp4"), language="en", keep_audio=False)
        with mock.patch.dict("os.environ", {"ALIYUN_ASR_SYNC_MAX_SECONDS": "600"}):
            with self.assertRaises(RuntimeError):
                transcribe_api.transcribe_with_sync_model(
                    args, "sk-test", "qwen-audio-3.1-asr-flash", "llm-test", "cn-beijing", None
                )


if __name__ == "__main__":
    unittest.main()
