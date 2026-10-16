from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "migrate_asr_hotwords", ROOT / "tools" / "migrate_asr_hotwords.py"
)
assert SPEC and SPEC.loader
migrate = importlib.util.module_from_spec(SPEC)
sys.modules["migrate_asr_hotwords"] = migrate
SPEC.loader.exec_module(migrate)

CUSTOMIZATION_URL = (
    "https://workspace123.cn-beijing.maas.aliyuncs.com"
    "/api/v1/services/audio/asr/customization"
)
SECRET = "sk-do-not-print-this-value-0001"


class RecordingPoster:
    """Test double: records calls and replays queued responses. No network."""

    def __init__(self, responses: list[dict[str, Any]] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def __call__(self, url: str, api_key: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((url, api_key, payload))
        if not self.responses:
            raise AssertionError(f"unexpected HTTP call to {url}")
        return self.responses.pop(0)


def forbidden_poster(url: str, api_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    raise AssertionError(f"this test must not perform HTTP calls: {url}")


class MigrationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env_file = self.root / "missing.env"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_cli(
        self,
        argv: list[str],
        *,
        poster: Any = None,
        env: Mapping[str, str] | None = None,
    ) -> tuple[int, str, str]:
        out = io.StringIO()
        err = io.StringIO()
        # Pin the tool's own environment inputs so an ambient .env or shell export
        # can never change what these tests exercise.
        base_env = {
            migrate.API_KEY_ENV: "",
            migrate.WORKSPACE_ID_ENV: "",
            migrate.VOCABULARY_ID_ENV: "",
            migrate.REGION_ENV: "",
            migrate.DEFAULT_TARGET_MODEL_ENV: "",
        }
        base_env.update(env or {})
        with mock.patch.dict(os.environ, base_env, clear=False):
            with contextlib.redirect_stderr(err):
                code = migrate.run(
                    migrate.parse_args(["--env-file", str(self.env_file), *argv]),
                    poster=poster or forbidden_poster,
                    out=out,
                )
        return code, out.getvalue(), err.getvalue()


class PayloadConstructionTests(MigrationTestCase):
    def test_create_vocabulary_body_is_exact_json(self) -> None:
        body = migrate.create_vocabulary_body(
            target_model="qwen-audio-3.1-asr-flash-filetrans",
            prefix="vt01",
            vocabulary=[
                {"text": "柯基", "weight": 4, "lang": "zh"},
                {"text": "B-roll", "weight": 50, "lang": ""},
            ],
        )
        self.assertEqual(
            body,
            {
                "model": "speech-biasing",
                "input": {
                    "action": "create_vocabulary",
                    "target_model": "qwen-audio-3.1-asr-flash-filetrans",
                    "prefix": "vt01",
                    "vocabulary": [
                        {"text": "柯基", "weight": 4, "lang": "zh"},
                        {"text": "B-roll", "weight": 50},
                    ],
                },
            },
        )

    def test_list_vocabulary_body_includes_only_requested_options(self) -> None:
        self.assertEqual(
            migrate.list_vocabulary_body(),
            {"model": "speech-biasing", "input": {"action": "list_vocabulary"}},
        )
        self.assertEqual(
            migrate.list_vocabulary_body(prefix="vt01", page_index=0, page_size=50),
            {
                "model": "speech-biasing",
                "input": {
                    "action": "list_vocabulary",
                    "prefix": "vt01",
                    "page_index": 0,
                    "page_size": 50,
                },
            },
        )

    def test_query_and_delete_bodies_are_exact_json(self) -> None:
        self.assertEqual(
            migrate.query_vocabulary_body("voc-old"),
            {
                "model": "speech-biasing",
                "input": {"action": "query_vocabulary", "vocabulary_id": "voc-old"},
            },
        )
        self.assertEqual(
            migrate.delete_vocabulary_body("voc-old"),
            {
                "model": "speech-biasing",
                "input": {"action": "delete_vocabulary", "vocabulary_id": "voc-old"},
            },
        )

    def test_update_vocabulary_body_never_carries_target_model(self) -> None:
        body = migrate.update_vocabulary_body(
            vocabulary_id="voc-old", vocabulary=[{"text": "柯基", "weight": 4}]
        )
        self.assertEqual(
            body,
            {
                "model": "speech-biasing",
                "input": {
                    "action": "update_vocabulary",
                    "vocabulary_id": "voc-old",
                    "vocabulary": [{"text": "柯基", "weight": 4}],
                },
            },
        )
        self.assertNotIn("target_model", body["input"])

    def test_create_vocabulary_body_rejects_bad_prefix(self) -> None:
        for prefix in ("", "Upper", "has-dash", "x" * 11, "空格"):
            with self.subTest(prefix=prefix):
                with self.assertRaisesRegex(migrate.MigrationError, "prefix"):
                    migrate.create_vocabulary_body(
                        target_model="qwen-audio-3.1-asr-flash-filetrans",
                        prefix=prefix,
                        vocabulary=[],
                    )

    def test_create_vocabulary_body_validates_hotwords_before_sending(self) -> None:
        with self.assertRaisesRegex(migrate.MigrationError, "fragments"):
            migrate.create_vocabulary_body(
                target_model="qwen-audio-3.1-asr-flash-filetrans",
                prefix="vt01",
                vocabulary=[{"text": "one two three four five six seven eight", "weight": 4}],
            )


class ResponseParsingTests(MigrationTestCase):
    def test_parse_query_response_builds_export_document_with_inline_map(self) -> None:
        document = migrate.parse_query_response(
            {
                "output": {
                    "target_model": "fun-asr",
                    "status": "OK",
                    "vocabulary": [
                        {"text": "柯基", "weight": 4, "lang": "zh"},
                        {"text": "B-roll", "weight": 50, "lang": "en"},
                    ],
                }
            }
        )
        self.assertEqual(
            document,
            {
                "target_model": "fun-asr",
                "vocabulary": [
                    {"text": "柯基", "weight": 4, "lang": "zh"},
                    {"text": "B-roll", "weight": 50, "lang": "en"},
                ],
                "inline": {"柯基": 4, "B-roll": 50},
            },
        )

    def test_parse_query_response_dedupes_and_keeps_highest_weight(self) -> None:
        document = migrate.parse_query_response(
            {
                "output": {
                    "target_model": "fun-asr",
                    "vocabulary": [
                        {"text": "Corgi", "weight": 2, "lang": "en"},
                        {"text": "corgi", "weight": 50},
                    ],
                }
            }
        )
        self.assertEqual(
            document["vocabulary"], [{"text": "Corgi", "weight": 50, "lang": "en"}]
        )
        self.assertEqual(document["inline"], {"Corgi": 50})

    def test_parse_query_response_is_strict_about_required_fields(self) -> None:
        with self.assertRaisesRegex(migrate.RemoteError, "output.target_model"):
            migrate.parse_query_response({"output": {"vocabulary": []}})
        with self.assertRaisesRegex(migrate.RemoteError, "output.vocabulary"):
            migrate.parse_query_response({"output": {"target_model": "fun-asr"}})
        with self.assertRaisesRegex(migrate.RemoteError, "output"):
            migrate.parse_query_response({"request_id": "abc"})

    def test_created_vocabulary_id_requires_the_response_field(self) -> None:
        self.assertEqual(
            migrate.created_vocabulary_id({"output": {"vocabulary_id": "voc-new"}}), "voc-new"
        )
        with self.assertRaisesRegex(migrate.RemoteError, "vocabulary_id"):
            migrate.created_vocabulary_id({"output": {}})


class NormalizationTests(MigrationTestCase):
    def test_case_insensitive_dedupe_keeps_first_spelling_and_highest_weight(self) -> None:
        vocabulary = migrate.normalize_vocabulary(
            [
                {"text": "Corgi", "weight": 1, "lang": "en"},
                {"text": "corgi", "weight": 5},
                {"text": "CORGI", "weight": 4},
                {"text": "柯基", "weight": 4, "lang": "zh"},
            ]
        )
        self.assertEqual(
            vocabulary,
            [
                {"text": "Corgi", "weight": 5, "lang": "en"},
                {"text": "柯基", "weight": 4, "lang": "zh"},
            ],
        )

    def test_weight_must_be_one_to_five_or_fifty(self) -> None:
        for weight in (0, 6, 49, -1, 4.5, "4", None, True):
            with self.subTest(weight=weight):
                with self.assertRaisesRegex(migrate.MigrationError, "weight"):
                    migrate.normalize_vocabulary([{"text": "柯基", "weight": weight}])
        for weight in (1, 2, 3, 4, 5, 50, 4.0):
            with self.subTest(weight=weight):
                self.assertEqual(
                    migrate.normalize_vocabulary([{"text": "柯基", "weight": weight}])[0]["weight"],
                    int(weight),
                )

    def test_super_hotword_cap_is_enforced(self) -> None:
        allowed = [{"text": f"term{i}", "weight": 50} for i in range(migrate.MAX_SUPER_HOTWORDS)]
        self.assertEqual(len(migrate.normalize_vocabulary(allowed)), migrate.MAX_SUPER_HOTWORDS)
        oversized = allowed + [{"text": "one-more", "weight": 50}]
        with self.assertRaisesRegex(migrate.MigrationError, "super hotwords"):
            migrate.normalize_vocabulary(oversized)

    def test_total_hotword_cap_is_enforced(self) -> None:
        at_limit = [{"text": f"term{i}", "weight": 4} for i in range(migrate.MAX_HOTWORDS)]
        self.assertEqual(len(migrate.normalize_vocabulary(at_limit)), migrate.MAX_HOTWORDS)
        over_limit = at_limit + [{"text": "one-more", "weight": 4}]
        with self.assertRaisesRegex(migrate.MigrationError, "hotword limit"):
            migrate.normalize_vocabulary(over_limit)

    def test_case_variant_duplicates_do_not_count_toward_the_total_cap(self) -> None:
        entries = [{"text": f"term{i}", "weight": 4} for i in range(migrate.MAX_HOTWORDS)]
        entries.append({"text": "TERM0", "weight": 5})
        vocabulary = migrate.normalize_vocabulary(entries)
        self.assertEqual(len(vocabulary), migrate.MAX_HOTWORDS)
        self.assertEqual(vocabulary[0], {"text": "term0", "weight": 5, "lang": ""})

    def test_invalid_entries_are_named_in_the_error(self) -> None:
        with self.assertRaisesRegex(migrate.MigrationError, "hotword #1 'ok'"):
            migrate.normalize_vocabulary(
                [{"text": "fine", "weight": 4}, {"text": "ok", "weight": 7}]
            )
        with self.assertRaisesRegex(migrate.MigrationError, "hotword #0"):
            migrate.normalize_vocabulary([{"text": "   ", "weight": 4}])
        with self.assertRaisesRegex(migrate.MigrationError, "hotword #0"):
            migrate.normalize_vocabulary(["not-an-object"])
        with self.assertRaisesRegex(migrate.MigrationError, "lang"):
            migrate.normalize_vocabulary([{"text": "柯基", "weight": 4, "lang": "zh_CN!"}])


class HotwordTextRuleTests(MigrationTestCase):
    def test_non_ascii_sixteen_characters_rejected_and_fifteen_accepted(self) -> None:
        sixteen = "中文" * 8
        fifteen = "中文" * 7 + "甲"
        self.assertEqual(len(sixteen), 16)
        self.assertEqual(len(fifteen), 15)
        with self.assertRaisesRegex(migrate.MigrationError, "at most 15 characters"):
            migrate.normalize_vocabulary([{"text": sixteen, "weight": 4}])
        self.assertEqual(
            migrate.normalize_vocabulary([{"text": fifteen, "weight": 4}])[0]["text"], fifteen
        )

    def test_non_ascii_limit_counts_the_ascii_characters_too(self) -> None:
        accepted = "x" * 14 + "中"
        rejected = "x" * 15 + "中"
        self.assertIsNone(migrate.hotword_text_problem(accepted))
        self.assertIn("16", str(migrate.hotword_text_problem(rejected)))
        with self.assertRaisesRegex(migrate.MigrationError, "counted over the whole text"):
            migrate.normalize_vocabulary([{"text": rejected, "weight": 4}])

    def test_pure_ascii_fragment_limit(self) -> None:
        seven = "one two three four five six seven"
        eight = "one two three four five six seven eight"
        self.assertEqual(len(seven.split()), 7)
        self.assertIsNone(migrate.hotword_text_problem(seven))
        with self.assertRaisesRegex(migrate.MigrationError, "space-separated fragments"):
            migrate.normalize_vocabulary([{"text": eight, "weight": 4}])
        self.assertEqual(
            migrate.normalize_vocabulary([{"text": seven, "weight": 4}])[0]["text"], seven
        )


class DryRunSafetyTests(MigrationTestCase):
    def test_dry_run_never_posts_and_never_writes_unless_asked(self) -> None:
        export_path = self.root / "export.json"
        code, out, err = self.run_cli(
            [
                "--workspace-id",
                "workspace123",
                "--list",
                "--source-vocabulary-id",
                "voc-old",
                "--create-new",
                "--prefix",
                "vt01",
                "--delete",
                "voc-old",
                "--export-json",
                str(export_path),
            ],
            poster=forbidden_poster,
        )
        self.assertEqual(code, 0, err)
        self.assertIn('"model": "speech-biasing"', out)
        self.assertIn('"action": "create_vocabulary"', out)
        self.assertIn('"target_model": "qwen-audio-3.1-asr-flash-filetrans"', out)
        self.assertIn('"action": "delete_vocabulary"', out)
        self.assertIn("Authorization: Bearer <redacted>", out)
        self.assertNotIn(SECRET, out)
        self.assertFalse(export_path.exists())

    def test_dry_run_needs_no_workspace_id_or_api_key_for_offline_conversion(self) -> None:
        source = self.root / "hotwords.json"
        source.write_text(
            json.dumps({"vocabulary": [{"text": "柯基", "weight": 4, "lang": "zh"}]}),
            encoding="utf-8",
        )
        destination = self.root / "normalized.json"
        code, out, err = self.run_cli(
            ["--hotwords-json", str(source), "--export-json", str(destination)],
            poster=forbidden_poster,
        )
        self.assertEqual(code, 0, err)
        self.assertTrue(destination.exists())
        document = json.loads(destination.read_text(encoding="utf-8"))
        self.assertEqual(
            document,
            {
                "target_model": migrate.DEFAULT_TARGET_MODEL,
                "vocabulary": [{"text": "柯基", "weight": 4, "lang": "zh"}],
                "inline": {"柯基": 4},
            },
        )
        self.assertIn("wrote", out)

    def test_missing_workspace_id_is_a_usage_error(self) -> None:
        code, _, err = self.run_cli(["--list"], poster=forbidden_poster)
        self.assertEqual(code, 2)
        self.assertIn("workspace ID", err)

    def test_export_json_requires_a_vocabulary_source(self) -> None:
        code, _, err = self.run_cli(
            ["--workspace-id", "workspace123", "--export-json", str(self.root / "x.json")],
            poster=forbidden_poster,
        )
        self.assertEqual(code, 2)
        self.assertIn("--export-json needs", err)

    def test_nothing_to_do_is_a_usage_error(self) -> None:
        code, _, err = self.run_cli(["--workspace-id", "workspace123"], poster=forbidden_poster)
        self.assertEqual(code, 2)
        self.assertIn("nothing to do", err)

    def test_prefix_is_rejected_without_create_new(self) -> None:
        code, _, err = self.run_cli(
            ["--workspace-id", "workspace123", "--list", "--prefix", "vt01"],
            poster=forbidden_poster,
        )
        self.assertEqual(code, 2)
        self.assertIn("--create-new", err)

    def test_apply_without_api_key_names_only_the_env_var(self) -> None:
        code, out, err = self.run_cli(
            ["--workspace-id", "workspace123", "--list", "--apply"],
            poster=forbidden_poster,
            env={migrate.API_KEY_ENV: ""},
        )
        self.assertEqual(code, 2)
        self.assertIn(migrate.API_KEY_ENV, err)
        self.assertNotIn(SECRET, out + err)


class SecretHandlingTests(MigrationTestCase):
    def test_api_key_value_never_appears_in_any_output(self) -> None:
        poster = RecordingPoster(
            [
                {
                    "output": {
                        "target_model": "fun-asr",
                        "status": "OK",
                        "vocabulary": [{"text": "柯基", "weight": 4, "lang": "zh"}],
                    }
                },
                {"output": {"vocabulary_id": "voc-new", "status": "OK"}},
            ]
        )
        export_path = self.root / "export.json"
        code, out, err = self.run_cli(
            [
                "--workspace-id",
                "workspace123",
                "--source-vocabulary-id",
                "voc-old",
                "--export-json",
                str(export_path),
                "--create-new",
                "--prefix",
                "vt01",
                "--apply",
            ],
            poster=poster,
            env={migrate.API_KEY_ENV: SECRET},
        )
        self.assertEqual(code, 0, err)
        self.assertNotIn(SECRET, out)
        self.assertNotIn(SECRET, err)
        self.assertNotIn(SECRET, export_path.read_text(encoding="utf-8"))
        # The key is threaded to the poster (which owns the Authorization header)
        # and never echoed anywhere else.
        self.assertEqual([call[1] for call in poster.calls], [SECRET, SECRET])
        self.assertNotIn("Bearer", out)

    def test_dry_run_prints_payloads_without_reading_the_key(self) -> None:
        code, out, err = self.run_cli(
            ["--workspace-id", "workspace123", "--delete", "voc-old"],
            poster=forbidden_poster,
            env={migrate.API_KEY_ENV: SECRET},
        )
        self.assertEqual(code, 0, err)
        self.assertNotIn(SECRET, out + err)


class ApplyFlowTests(MigrationTestCase):
    def test_apply_exports_source_list_and_creates_the_new_model_list(self) -> None:
        poster = RecordingPoster(
            [
                {
                    "output": {
                        "target_model": "fun-asr",
                        "status": "OK",
                        "vocabulary": [
                            {"text": "柯基", "weight": 4, "lang": "zh"},
                            {"text": "B-roll", "weight": 50, "lang": "en"},
                        ],
                    }
                },
                {"output": {"vocabulary_id": "voc-new", "status": "OK"}},
            ]
        )
        export_path = self.root / "export.json"
        code, out, err = self.run_cli(
            [
                "--workspace-id",
                "workspace123",
                "--source-vocabulary-id",
                "voc-old",
                "--export-json",
                str(export_path),
                "--create-new",
                "--prefix",
                "vt01",
                "--apply",
            ],
            poster=poster,
            env={migrate.API_KEY_ENV: SECRET},
        )
        self.assertEqual(code, 0, err)

        document = json.loads(export_path.read_text(encoding="utf-8"))
        self.assertEqual(document["target_model"], "fun-asr")
        self.assertEqual(document["inline"], {"柯基": 4, "B-roll": 50})
        self.assertEqual(len(document["vocabulary"]), 2)

        self.assertEqual([call[0] for call in poster.calls], [CUSTOMIZATION_URL, CUSTOMIZATION_URL])
        self.assertEqual(poster.calls[0][2], migrate.query_vocabulary_body("voc-old"))
        self.assertEqual(
            poster.calls[1][2],
            migrate.create_vocabulary_body(
                target_model=migrate.DEFAULT_TARGET_MODEL,
                prefix="vt01",
                vocabulary=document["vocabulary"],
            ),
        )
        self.assertEqual(
            poster.calls[1][2]["input"]["target_model"], "qwen-audio-3.1-asr-flash-filetrans"
        )
        self.assertIn("voc-new", out)

    def test_apply_lists_vocabularies_for_the_region_endpoint(self) -> None:
        poster = RecordingPoster(
            [
                {
                    "output": {
                        "vocabulary_list": [
                            {
                                "vocabulary_id": "voc-1",
                                "gmt_create": "2026-01-01T00:00:00Z",
                                "gmt_modified": "2026-02-02T00:00:00Z",
                                "status": "OK",
                            }
                        ]
                    }
                }
            ]
        )
        code, out, err = self.run_cli(
            ["--workspace-id", "workspace123", "--list", "--list-prefix", "vt01", "--apply"],
            poster=poster,
            env={migrate.API_KEY_ENV: SECRET},
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(poster.calls[0][0], CUSTOMIZATION_URL)
        self.assertEqual(poster.calls[0][2], migrate.list_vocabulary_body(prefix="vt01"))
        self.assertIn("voc-1", out)
        self.assertIn("2026-02-02T00:00:00Z", out)
        self.assertIn("OK", out)

    def test_delete_only_calls_the_api_under_apply(self) -> None:
        dry_code, dry_out, dry_err = self.run_cli(
            ["--workspace-id", "workspace123", "--delete", "voc-old"],
            poster=forbidden_poster,
            env={migrate.API_KEY_ENV: SECRET},
        )
        self.assertEqual(dry_code, 0, dry_err)
        self.assertIn("[dry-run] POST", dry_out)
        self.assertIn('"action": "delete_vocabulary"', dry_out)
        self.assertNotIn("[apply]", dry_out)

        poster = RecordingPoster([{"output": {"status": "OK"}}])
        code, out, err = self.run_cli(
            ["--workspace-id", "workspace123", "--delete", "voc-old", "--apply"],
            poster=poster,
            env={migrate.API_KEY_ENV: SECRET},
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(poster.calls[0][2], migrate.delete_vocabulary_body("voc-old"))
        self.assertIn("OK", out)

    def test_update_reports_that_target_model_cannot_change(self) -> None:
        poster = RecordingPoster([{"output": {"status": "OK"}}])
        code, out, err = self.run_cli(
            [
                "--workspace-id",
                "workspace123",
                "--hotwords-json",
                str(self.write_hotwords()),
                "--update",
                "voc-old",
                "--apply",
            ],
            poster=poster,
            env={migrate.API_KEY_ENV: SECRET},
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(poster.calls[0][2]["input"]["action"], "update_vocabulary")
        self.assertNotIn("target_model", poster.calls[0][2]["input"])
        self.assertIn("target_model is unchanged", out)

    def test_hotwords_json_can_create_a_list_without_a_source_list(self) -> None:
        poster = RecordingPoster([{"output": {"vocabulary_id": "voc-new"}}])
        code, out, err = self.run_cli(
            [
                "--workspace-id",
                "workspace123",
                "--hotwords-json",
                str(self.write_hotwords()),
                "--create-new",
                "--prefix",
                "vt02",
                "--apply",
            ],
            poster=poster,
            env={migrate.API_KEY_ENV: SECRET},
        )
        self.assertEqual(code, 0, err)
        body = poster.calls[0][2]
        self.assertEqual(body["input"]["vocabulary"], [{"text": "柯基", "weight": 4, "lang": "zh"}])
        self.assertIn("voc-new", out)

    def test_remote_failures_return_exit_code_one(self) -> None:
        def failing_poster(url: str, api_key: str, payload: dict[str, Any]) -> dict[str, Any]:
            raise migrate.RemoteError(f"POST {url} failed: connection refused")

        code, _, err = self.run_cli(
            ["--workspace-id", "workspace123", "--list", "--apply"],
            poster=failing_poster,
            env={migrate.API_KEY_ENV: SECRET},
        )
        self.assertEqual(code, 1)
        self.assertIn("connection refused", err)

    def write_hotwords(self) -> Path:
        path = self.root / "hotwords.json"
        path.write_text(
            json.dumps(
                {
                    "target_model": "fun-asr",
                    "vocabulary": [{"text": "柯基", "weight": 4, "lang": "zh"}],
                    "inline": {"柯基": 4},
                }
            ),
            encoding="utf-8",
        )
        return path


class EndpointTests(MigrationTestCase):
    def test_endpoint_is_the_fixed_beijing_customization_url(self) -> None:
        self.assertEqual(
            migrate.customization_url(migrate.customization_base_url("workspace123", "cn-beijing")),
            CUSTOMIZATION_URL,
        )

    def test_endpoint_rejects_unusable_identifiers(self) -> None:
        for workspace_id, region in (
            ("", "cn-beijing"),
            ("bad id", "cn-beijing"),
            ("workspace123", ""),
            ("workspace123", "cn beijing"),
        ):
            with self.subTest(workspace_id=workspace_id, region=region):
                with self.assertRaises(migrate.MigrationError):
                    migrate.customization_base_url(workspace_id, region)


if __name__ == "__main__":
    unittest.main()
