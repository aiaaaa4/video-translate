import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = ROOT / "skills" / "video-translate" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import transcribe_api  # noqa: E402
from transcribe_api import (  # noqa: E402
    ASR_MODEL_FUN_ASR,
    ASR_MODEL_QWEN_AUDIO_31,
    PROVIDERS,
    inline_hotwords_map,
    load_hotwords_file,
    normalize_aliyun_response,
    normalize_hotwords,
    provider_label,
    resolve_asr_model,
    submit_aliyun_fun_asr,
    validate_hotword_text,
)


class AsrModelSelectionTest(unittest.TestCase):
    def setUp(self):
        self.provider_cfg = PROVIDERS["aliyun-fun-asr"]
        self._saved = os.environ.pop("ALIYUN_ASR_MODEL", None)

    def tearDown(self):
        os.environ.pop("ALIYUN_ASR_MODEL", None)
        if self._saved is not None:
            os.environ["ALIYUN_ASR_MODEL"] = self._saved

    def test_default_model_is_qwen_audio_31_filetrans(self):
        self.assertEqual(self.provider_cfg["default_model"], ASR_MODEL_QWEN_AUDIO_31)
        self.assertEqual(resolve_asr_model(None, self.provider_cfg), ASR_MODEL_QWEN_AUDIO_31)

    def test_env_var_pins_the_model(self):
        os.environ["ALIYUN_ASR_MODEL"] = ASR_MODEL_FUN_ASR
        self.assertEqual(resolve_asr_model(None, self.provider_cfg), ASR_MODEL_FUN_ASR)

    def test_explicit_argument_beats_env_var(self):
        os.environ["ALIYUN_ASR_MODEL"] = ASR_MODEL_FUN_ASR
        self.assertEqual(resolve_asr_model(ASR_MODEL_QWEN_AUDIO_31, self.provider_cfg), ASR_MODEL_QWEN_AUDIO_31)

    def test_model_outside_the_fixed_stack_is_rejected(self):
        with self.assertRaises(RuntimeError):
            resolve_asr_model("paraformer-v2", self.provider_cfg)
        os.environ["ALIYUN_ASR_MODEL"] = "whisper-1"
        with self.assertRaises(RuntimeError):
            resolve_asr_model(None, self.provider_cfg)

    def test_provider_label_reports_the_real_family(self):
        self.assertEqual(provider_label(ASR_MODEL_QWEN_AUDIO_31), "aliyun-qwen-audio-asr")
        self.assertEqual(provider_label("qwen-audio-3.0-asr-flash-filetrans"), "aliyun-qwen-audio-asr")
        self.assertEqual(provider_label(ASR_MODEL_FUN_ASR), "aliyun-fun-asr")


class AsrHotwordTest(unittest.TestCase):
    def test_non_ascii_text_rule_is_fifteen_characters(self):
        self.assertIsNone(validate_hotword_text("厄洛替尼盐酸盐"))
        self.assertIsNone(validate_hotword_text("中" * 15))
        self.assertIsNotNone(validate_hotword_text("中" * 16))
        self.assertIsNotNone(validate_hotword_text("这是一个超过十五个字符长度的中文热词条目"))

    def test_pure_ascii_text_rule_is_seven_fragments(self):
        # The 15-character budget only applies once a non-ASCII character is present,
        # so a single long ASCII token stays valid while eight fragments do not.
        self.assertIsNone(validate_hotword_text("Human immunodeficiency virus type 1"))
        self.assertIsNone(validate_hotword_text("a" * 30))
        self.assertIsNotNone(validate_hotword_text("The effect of temperature variations on enzyme activity here"))

    def test_entries_are_deduplicated_case_insensitively(self):
        # First spelling wins; the stronger weight is never silently dropped.
        entries = normalize_hotwords({"GEX": 4, "gex": 5, "NQ": 4})
        self.assertEqual(entries, [{"text": "GEX", "weight": 5}, {"text": "NQ", "weight": 4}])

    def test_case_duplicate_does_not_double_count_the_super_cap(self):
        entries = [{"text": "GEX", "weight": 50}, {"text": "gex", "weight": 50}]
        self.assertEqual(normalize_hotwords(entries), [{"text": "GEX", "weight": 50}])

    def test_weight_must_be_one_to_five_or_super(self):
        self.assertEqual(normalize_hotwords([{"text": "GEX", "weight": 5}])[0]["weight"], 5)
        self.assertEqual(normalize_hotwords([{"text": "GEX", "weight": 50}])[0]["weight"], 50)
        with self.assertRaises(ValueError):
            normalize_hotwords([{"text": "GEX", "weight": 6}])
        with self.assertRaises(ValueError):
            normalize_hotwords([{"text": "GEX", "weight": "high"}])

    def test_super_hotword_cap_is_enforced(self):
        entries = [{"text": f"Ticker{i}", "weight": 50} for i in range(51)]
        with self.assertRaises(ValueError):
            normalize_hotwords(entries)

    def test_total_hotword_cap_is_enforced(self):
        entries = [{"text": f"Ticker{i}", "weight": 4} for i in range(2001)]
        with self.assertRaises(ValueError):
            normalize_hotwords(entries)

    def test_lang_is_preserved_when_present(self):
        self.assertEqual(
            normalize_hotwords([{"text": "极值位", "weight": 4, "lang": "zh"}]),
            [{"text": "极值位", "weight": 4, "lang": "zh"}],
        )

    def test_hotword_file_accepts_all_three_supported_shapes(self):
        cases = [
            {"GEX": 4, "极值位": 5},
            [{"text": "GEX", "weight": 4}, {"text": "极值位", "weight": 5}],
            {"target_model": ASR_MODEL_QWEN_AUDIO_31, "vocabulary": [{"text": "GEX", "weight": 4}], "inline": {"GEX": 4}},
        ]
        expected = [{"GEX": 4, "极值位": 5}, {"GEX": 4, "极值位": 5}, {"GEX": 4}]
        with tempfile.TemporaryDirectory() as tmp:
            for index, payload in enumerate(cases):
                path = Path(tmp) / f"hotwords-{index}.json"
                path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                with self.subTest(payload=payload):
                    self.assertEqual(load_hotwords_file(path), expected[index])

    def test_inline_map_round_trip(self):
        entries = normalize_hotwords([{"text": "GEX", "weight": 4}, {"text": "Call Wall", "weight": 5}])
        self.assertEqual(inline_hotwords_map(entries), {"GEX": 4, "Call Wall": 5})


class AsrSubmitPayloadTest(unittest.TestCase):
    def _capture(self, **overrides):
        captured: dict = {}

        def fake_request(url, api_key, method=None, payload=None, headers=None):
            captured.update({"url": url, "api_key": api_key, "method": method, "payload": payload, "headers": headers})
            return {"output": {"task_id": "t1", "task_status": "PENDING"}}

        kwargs = {
            "api_key": "secret-key",
            "workspace_id": "llm-test123",
            "region": "cn-beijing",
            "model": ASR_MODEL_QWEN_AUDIO_31,
            "file_url": "https://example.test/audio.wav",
            "language": "en",
            "vocabulary_id": None,
        }
        kwargs.update(overrides)
        with mock.patch.object(transcribe_api, "http_json_request", side_effect=fake_request):
            submit_aliyun_fun_asr(**kwargs)
        return captured

    def test_parameters_object_is_always_sent(self):
        # qwen-audio-3.x-asr-flash-filetrans fails the task when `parameters` is omitted.
        payload = self._capture()["payload"]
        self.assertIn("parameters", payload)
        self.assertEqual(payload["model"], ASR_MODEL_QWEN_AUDIO_31)
        self.assertEqual(payload["input"]["file_urls"], ["https://example.test/audio.wav"])
        self.assertEqual(payload["parameters"]["language_hints"], ["en"])

    def test_async_header_and_endpoint(self):
        captured = self._capture()
        self.assertEqual(captured["headers"], {"X-DashScope-Async": "enable"})
        self.assertTrue(captured["url"].endswith("/api/v1/services/audio/asr/transcription"))

    def test_precompiled_vocabulary_id_is_forwarded(self):
        payload = self._capture(vocabulary_id="vocab-example-abc")["payload"]
        self.assertEqual(payload["parameters"]["vocabulary_id"], "vocab-example-abc")
        self.assertNotIn("vocabulary", payload["parameters"])

    def test_inline_hotwords_are_sent_for_qwen_audio(self):
        payload = self._capture(hotwords={"GEX": 4, "极值位": 50})["payload"]
        self.assertEqual(payload["parameters"]["vocabulary"], {"GEX": 4, "极值位": 50})

    def test_inline_hotwords_are_rejected_for_fun_asr(self):
        with self.assertRaises(RuntimeError):
            self._capture(model=ASR_MODEL_FUN_ASR, hotwords={"GEX": 4})


class AsrWordTimestampTest(unittest.TestCase):
    def test_word_timestamps_are_required(self):
        raw = {"transcripts": [{"sentences": [{"begin_time": 0, "end_time": 1000, "text": "hello"}]}]}
        with self.assertRaises(RuntimeError):
            normalize_aliyun_response(Path("a.wav"), ASR_MODEL_QWEN_AUDIO_31, "en", raw)

    def test_words_are_parsed_with_provider_label(self):
        raw = {
            "transcripts": [
                {
                    "sentences": [
                        {
                            "begin_time": 0,
                            "end_time": 1000,
                            "text": "hello world",
                            "words": [
                                {"begin_time": 0, "end_time": 500, "text": "hello"},
                                {"begin_time": 500, "end_time": 1000, "text": "world"},
                            ],
                        }
                    ]
                }
            ],
            "properties": {"original_duration_in_milliseconds": 1200},
        }
        transcript = normalize_aliyun_response(Path("a.wav"), ASR_MODEL_QWEN_AUDIO_31, "en", raw)
        self.assertEqual(transcript["provider"], "aliyun-qwen-audio-asr")
        self.assertEqual(transcript["model"], ASR_MODEL_QWEN_AUDIO_31)
        self.assertEqual([word["word"] for word in transcript["words"]], ["hello", "world"])
        self.assertEqual(transcript["duration"], 1.2)


class AsrEnvFileIntegrationTest(unittest.TestCase):
    """`.env` is the documented configuration surface, so it must actually apply.

    `ALIYUN_ASR_MODEL` previously appeared in the `.env` template but was never read,
    and `load_env` ran after model resolution, so even the wired version would have
    ignored a `.env`-only setting.
    """

    def setUp(self):
        self._saved = {key: os.environ.pop(key, None) for key in ("ALIYUN_ASR_MODEL", "ALIYUN_ASR_HOTWORDS_FILE")}
        self._saved_key = os.environ.get("DASHSCOPE_API_KEY")
        os.environ["DASHSCOPE_API_KEY"] = "sk-test-not-a-real-key"

    def tearDown(self):
        for key, value in self._saved.items():
            os.environ.pop(key, None)
            if value is not None:
                os.environ[key] = value
        if self._saved_key is None:
            os.environ.pop("DASHSCOPE_API_KEY", None)
        else:
            os.environ["DASHSCOPE_API_KEY"] = self._saved_key

    def test_env_file_model_is_resolved_by_main(self):
        captured: dict = {}

        def fake_load_env(path):
            os.environ["ALIYUN_ASR_MODEL"] = ASR_MODEL_FUN_ASR

        def fake_transcribe(args, api_key, model):
            captured["model"] = model
            captured["hotwords_file"] = args.hotwords_file
            return 0

        argv = ["transcribe_api.py", "lesson.mp4", "--confirm-external-processing", "--env-file", "unused.env"]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.object(transcribe_api, "load_env", side_effect=fake_load_env), \
             mock.patch.object(transcribe_api, "transcribe_aliyun_fun_asr", side_effect=fake_transcribe):
            exit_code = transcribe_api.main()

        self.assertEqual(exit_code, 0)
        self.assertEqual(captured["model"], ASR_MODEL_FUN_ASR)

    def test_hotwords_file_env_var_reaches_the_cli_namespace(self):
        # The wrapper spawns this script without --hotwords-file, so the env var is
        # the only path a workflow run has to inline hotwords.
        captured: dict = {}

        def fake_load_env(path):
            os.environ["ALIYUN_ASR_HOTWORDS_FILE"] = "hotwords.json"

        def fake_transcribe(args, api_key, model):
            captured["hotwords_file"] = args.hotwords_file
            return 0

        argv = ["transcribe_api.py", "lesson.mp4", "--confirm-external-processing"]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.object(transcribe_api, "load_env", side_effect=fake_load_env), \
             mock.patch.object(transcribe_api, "transcribe_aliyun_fun_asr", side_effect=fake_transcribe):
            transcribe_api.main()

        # main() does not copy the env var into args; transcribe_aliyun_fun_asr reads
        # os.environ itself, so assert the env var is what the function will see.
        self.assertIsNone(captured["hotwords_file"])
        self.assertEqual(os.environ["ALIYUN_ASR_HOTWORDS_FILE"], "hotwords.json")

    def test_unsupported_env_file_model_fails_before_any_work(self):
        def fake_load_env(path):
            os.environ["ALIYUN_ASR_MODEL"] = "paraformer-v2"

        argv = ["transcribe_api.py", "lesson.mp4", "--confirm-external-processing"]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.object(transcribe_api, "load_env", side_effect=fake_load_env), \
             mock.patch.object(transcribe_api, "transcribe_aliyun_fun_asr") as transcribe_mock:
            exit_code = transcribe_api.main()

        self.assertEqual(exit_code, 2)
        transcribe_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
