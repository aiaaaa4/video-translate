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

import common  # noqa: E402
import transcribe_api  # noqa: E402
from common import (  # noqa: E402
    apply_env_file,
    env_file_declares_credentials,
    prepare_env_file,
    search_env_file,
)

CREDENTIAL_BODY = "DASHSCOPE_API_KEY=sk-test-key\nALIYUN_WORKSPACE_ID=llm-test\nOKFILE_TOKEN=ok-test\n"


class EnvDiscoveryTest(unittest.TestCase):
    """The credentials may live in a workspace root above the Skill directory.

    The steps run with the Skill directory as cwd, so a `.env`-relative lookup
    could not see them and the run failed as if the provider were unreachable.
    """

    MANAGED_KEYS = ("DASHSCOPE_API_KEY", "ALIYUN_WORKSPACE_ID", "OKFILE_TOKEN", "ALIYUN_ENV_FILE")

    def setUp(self):
        self._saved = {key: os.environ.get(key) for key in self.MANAGED_KEYS}
        self._saved_cwd = Path.cwd()
        for key in self.MANAGED_KEYS:
            os.environ.pop(key, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()

    def tearDown(self):
        os.chdir(self._saved_cwd)
        for key, value in self._saved.items():
            os.environ.pop(key, None)
            if value is not None:
                os.environ[key] = value
        self.tmp.cleanup()

    def _workspace(self, *, with_credentials: bool = True):
        """A production-shaped layout: <ws>/skills/video-translate."""
        skill_dir = self.root / "daily-production" / "skills" / "video-translate"
        skill_dir.mkdir(parents=True)
        body = CREDENTIAL_BODY if with_credentials else "UNRELATED_SETTING=1\n"
        (self.root / "daily-production" / ".env").write_text(body, encoding="utf-8")
        return skill_dir

    def test_finds_env_in_the_workspace_root_from_the_skill_directory(self):
        skill_dir = self._workspace()
        search = search_env_file(None, start=skill_dir)
        self.assertEqual(search.path, self.root / "daily-production" / ".env")
        self.assertFalse(search.explicit)

    def test_explicit_path_wins_and_a_missing_one_is_reported(self):
        self._workspace()
        named = self.root / "custom.env"
        self.assertIsNone(search_env_file(named, start=self.root).path)
        self.assertTrue(search_env_file(named, start=self.root).explicit)

        named.write_text(CREDENTIAL_BODY, encoding="utf-8")
        self.assertEqual(search_env_file(named, start=self.root).path, named)

    def test_env_var_names_the_file(self):
        self._workspace()
        named = self.root / "from-env.env"
        named.write_text(CREDENTIAL_BODY, encoding="utf-8")
        os.environ["ALIYUN_ENV_FILE"] = str(named)
        self.assertEqual(search_env_file(None, start=self.root).path, named)

    def test_unrelated_env_file_is_not_adopted(self):
        skill_dir = self._workspace(with_credentials=False)
        search = search_env_file(None, start=skill_dir)
        self.assertIsNone(search.path, "a .env without credential keys must not be used")
        self.assertTrue(any(candidate.name == ".env" for candidate in search.candidates))
        self.assertTrue(search.candidates, "the searched paths must be reportable")

    def test_describe_env_search_names_every_searched_path(self):
        skill_dir = self._workspace(with_credentials=False)
        search = search_env_file(None, start=skill_dir)
        description = common.describe_env_search(search)
        for candidate in search.candidates[:3]:
            self.assertIn(str(candidate), description)

    def test_discovered_file_is_applied_but_real_environment_wins(self):
        skill_dir = self._workspace()
        os.environ["DASHSCOPE_API_KEY"] = "sk-already-exported"
        search = prepare_env_file(None, start=skill_dir, report=False)
        self.assertIsNotNone(search.path)
        self.assertEqual(os.environ["DASHSCOPE_API_KEY"], "sk-already-exported")
        self.assertEqual(os.environ["OKFILE_TOKEN"], "ok-test")

    def test_credential_detection_ignores_unrelated_files(self):
        other = self.root / "other.env"
        other.write_text("SOMETHING_ELSE=1\n", encoding="utf-8")
        self.assertFalse(env_file_declares_credentials(other))
        other.write_text("OKFILE_TOKEN=abc\n", encoding="utf-8")
        self.assertTrue(env_file_declares_credentials(other))

    def test_apply_env_file_does_not_override_the_environment(self):
        path = self.root / "apply.env"
        path.write_text("OKFILE_TOKEN=from-file\n", encoding="utf-8")
        os.environ["OKFILE_TOKEN"] = "from-environment"
        apply_env_file(path)
        self.assertEqual(os.environ["OKFILE_TOKEN"], "from-environment")


class MissingCredentialDiagnosisTest(unittest.TestCase):
    """A missing credential file must not read as a provider outage."""

    MANAGED_KEYS = ("DASHSCOPE_API_KEY", "ALIYUN_WORKSPACE_ID", "OKFILE_TOKEN", "ALIYUN_ASR_MODEL")

    def setUp(self):
        self._saved = {key: os.environ.get(key) for key in self.MANAGED_KEYS}
        self._saved_cwd = Path.cwd()
        for key in self.MANAGED_KEYS:
            os.environ.pop(key, None)

    def tearDown(self):
        os.chdir(self._saved_cwd)
        for key, value in self._saved.items():
            os.environ.pop(key, None)
            if value is not None:
                os.environ[key] = value

    def test_missing_credentials_are_listed_together(self):
        self.assertEqual(
            transcribe_api.missing_credentials(),
            ["DASHSCOPE_API_KEY", "ALIYUN_WORKSPACE_ID", "OKFILE_TOKEN"],
        )

    def test_transcribe_main_stops_before_work_and_names_the_searched_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp).resolve()
            os.chdir(workdir)
            argv = ["transcribe_api.py", "lesson.mp4", "--confirm-external-processing"]
            captured = {}
            import io
            import contextlib

            stderr = io.StringIO()
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(transcribe_api, "transcribe_aliyun_fun_asr") as transcribe_mock, \
                 contextlib.redirect_stderr(stderr):
                exit_code = transcribe_api.main()
            captured["stderr"] = stderr.getvalue()

            self.assertEqual(exit_code, 2)
            transcribe_mock.assert_not_called()
            self.assertIn("Missing provider credentials", captured["stderr"])
            for key in ("DASHSCOPE_API_KEY", "ALIYUN_WORKSPACE_ID", "OKFILE_TOKEN"):
                self.assertIn(key, captured["stderr"])
            self.assertIn(str(workdir / ".env"), captured["stderr"])

    def test_transcribe_main_rejects_an_explicit_missing_env_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp).resolve() / "nope.env"
            argv = [
                "transcribe_api.py",
                "lesson.mp4",
                "--confirm-external-processing",
                "--env-file",
                str(missing),
            ]
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(transcribe_api, "transcribe_aliyun_fun_asr") as transcribe_mock:
                exit_code = transcribe_api.main()
            self.assertEqual(exit_code, 2)
            transcribe_mock.assert_not_called()


class WordConfidenceTest(unittest.TestCase):
    def test_confidence_is_carried_into_the_word_table(self):
        raw = {
            "transcripts": [
                {
                    "sentences": [
                        {
                            "begin_time": 0,
                            "end_time": 1000,
                            "text": "hello world",
                            "words": [
                                {"begin_time": 0, "end_time": 500, "text": "hello", "confidence": 0.93},
                                {"begin_time": 500, "end_time": 1000, "text": "world", "confidence": 1.0},
                            ],
                        }
                    ]
                }
            ]
        }
        transcript = transcribe_api.normalize_aliyun_response(
            Path("a.wav"), transcribe_api.ASR_MODEL_QWEN_AUDIO_31, "en", raw
        )
        self.assertEqual([word["probability"] for word in transcript["words"]], [0.93, 1.0])

    def test_absent_confidence_keeps_the_field_type(self):
        self.assertEqual(transcribe_api.word_confidence({"text": "hello"}), 0.0)
        self.assertEqual(transcribe_api.word_confidence({"text": "hello", "confidence": None}), 0.0)
        self.assertEqual(transcribe_api.word_confidence({"text": "hello", "confidence": "bad"}), 0.0)


class WrapperForwardsEnvTest(unittest.TestCase):
    """The wrapper must hand the resolved file to every credential-using step."""

    def setUp(self):
        import video_to_subtitles

        self.wrapper = video_to_subtitles

    def _capture(self, func, *args, **kwargs):
        commands = []
        with mock.patch.object(self.wrapper, "run_step", side_effect=lambda cmd, **kw: commands.append(cmd)):
            func(*args, **kwargs)
        return commands

    def test_transcribe_command_carries_the_env_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript_dir = Path(tmp).resolve()
            commands = self._capture(
                self.wrapper.ensure_transcript,
                Path("/tmp/lesson.mp4"),
                Path("/tmp/lesson.mp4"),
                transcript_dir,
                "en",
                True,
                Path("/ws/.env"),
            )
        self.assertTrue(commands, "the transcription step should have run")
        command = commands[0]
        self.assertIn("--env-file", command)
        self.assertEqual(command[command.index("--env-file") + 1], "/ws/.env")

    def test_ai_segments_command_carries_the_env_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp).resolve()
            commands = self._capture(
                self.wrapper.ensure_ai_segments,
                work_dir,
                work_dir / "transcript",
                "en",
                "trading",
                None,
                work_dir / "translation-context.json",
                Path("/ws/.env"),
            )
        self.assertTrue(commands, "the segment-generation step should have run")
        command = commands[0]
        self.assertIn("--env", command)
        self.assertEqual(command[command.index("--env") + 1], "/ws/.env")

    def test_commands_omit_the_flag_when_no_env_file_was_resolved(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript_dir = Path(tmp).resolve()
            commands = self._capture(
                self.wrapper.ensure_transcript,
                Path("/tmp/lesson.mp4"),
                Path("/tmp/lesson.mp4"),
                transcript_dir,
                "en",
                True,
                None,
            )
        self.assertTrue(commands)
        self.assertNotIn("--env-file", commands[0])


if __name__ == "__main__":
    unittest.main()
