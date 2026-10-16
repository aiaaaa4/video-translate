import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
WORK_DIR = ROOT / "skills" / "video-translate"
SCRIPTS_DIR = WORK_DIR / "scripts"
EXAMPLES_DIR = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(SCRIPTS_DIR))
import finalize_run  # noqa: E402
from subtitle_layout import PROFILES  # noqa: E402


def run_script(name: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / name), *args],
        capture_output=True,
        text=True,
        cwd=WORK_DIR,
    )


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PipelineScriptsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def build_aligned(self) -> Path:
        out_dir = self.tmp_dir / "work"
        result = run_script(
            "extract_word_stream.py",
            str(EXAMPLES_DIR / "transcript_words.json"),
            "--out-dir",
            str(out_dir),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        segments = out_dir / "segments.txt"
        segments.write_text((EXAMPLES_DIR / "segments.txt").read_text(encoding="utf-8"), encoding="utf-8")
        aligned = out_dir / "aligned_segments.json"
        result = run_script(
            "align_segments.py",
            str(out_dir / "word_table.json"),
            str(segments),
            "--out",
            str(aligned),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return aligned

    def prepare_delivery_evidence(self, aligned: Path) -> Path:
        work = aligned.parent
        segments = work / "segments.txt"
        (work / "output_naming.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "media": str(self.tmp_dir / "交易示例.mp4"),
                    "localized_title": "交易示例",
                    "title_source": "explicit_localized_title",
                    "output_tag": "中英双语字幕",
                    "output_base": "交易示例.中英双语字幕",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (work / "workflow_config.json").write_text(
            json.dumps({"schema_version": 1, "translation_provider": "qwen-mt-plus"}),
            encoding="utf-8",
        )
        profile = PROFILES["landscape"]
        layout_path = work / "subtitle_layout.json"
        layout_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "media": str(self.tmp_dir / "交易示例.mp4"),
                    "requested": "auto",
                    "resolved": "landscape",
                    "source_geometry": {
                        "width": 1920,
                        "height": 1080,
                        "rotation": 0,
                        "display_width": 1920,
                        "display_height": 1080,
                    },
                    "profile": asdict(profile),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        source_dir = work / "global_review" / "source-analysis"
        source_dir.mkdir(parents=True)
        context = source_dir / "translation-context.json"
        context.write_text(json.dumps({"stage": "translation-context"}), encoding="utf-8")
        (work / "segment_generation_meta.json").write_text(
            json.dumps(
                {
                    "translation_provider": "qwen-mt-plus",
                    "translation_context_sha256": sha256(context),
                }
            ),
            encoding="utf-8",
        )
        (source_dir / "source-analysis.validated.json").write_text(
            json.dumps(
                {
                    "stage": "source-analysis",
                    "status": "passed",
                    "translation_context_sha256": sha256(context),
                }
            ),
            encoding="utf-8",
        )

        initial_segments = work / "segments.initial.txt"
        initial_segments.write_bytes(segments.read_bytes())
        semantic_dir = work / "global_review" / "semantic"
        semantic_dir.mkdir(parents=True)
        semantic_prepared = run_script(
            "global_review.py",
            "prepare-semantic",
            "--segments",
            str(initial_segments),
            "--word-table",
            str(work / "word_table.json"),
            "--source-context",
            str(context),
            "--out-dir",
            str(semantic_dir),
        )
        self.assertEqual(semantic_prepared.returncode, 0, semantic_prepared.stdout + semantic_prepared.stderr)
        reviewed = semantic_dir / "segments.global-reviewed.txt"
        reviewed.write_bytes(segments.read_bytes())
        global_context = semantic_dir / "global-context.json"
        global_context.write_text(json.dumps({"outline": ["test"]}), encoding="utf-8")
        (semantic_dir / "semantic-review.validated.json").write_text(
            json.dumps(
                {
                    "stage": "semantic-review",
                    "status": "passed",
                    "reviewed_segments": str(reviewed.resolve()),
                    "reviewed_segments_sha256": sha256(reviewed),
                    "all_content_unchanged": True,
                }
            ),
            encoding="utf-8",
        )

        qa = run_script(
            "final_qa.py",
            str(aligned),
            str(segments),
            "--subtitle-layout-record",
            str(layout_path),
        )
        self.assertEqual(qa.returncode, 0, qa.stdout + qa.stderr)

        qc_dir = work / "global_review" / "final-qc"
        prepared = run_script(
            "global_review.py",
            "prepare-qc",
            "--segments",
            str(segments),
            "--qa-report",
            str(work / "final_qa_report.md"),
            "--global-context",
            str(global_context),
            "--out-dir",
            str(qc_dir),
        )
        self.assertEqual(prepared.returncode, 0, prepared.stdout + prepared.stderr)
        receipt = json.loads((qc_dir / "final-qc-receipt.template.json").read_text(encoding="utf-8"))
        receipt.update(status="passed", model="test-orchestrator")
        for review in receipt["section_reviews"]:
            review["status"] = "passed"
        receipt["checks"] = {name: "passed" for name in receipt["checks"]}
        receipt["spot_checks"] = {
            name: {"status": "passed", "notes": f"Checked {name}."}
            for name in receipt["spot_checks"]
        }
        receipt_path = qc_dir / "final-qc-receipt.json"
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        validated = run_script(
            "global_review.py",
            "validate-qc",
            "--manifest",
            str(qc_dir / "manifest.json"),
            "--receipt",
            str(receipt_path),
        )
        self.assertEqual(validated.returncode, 0, validated.stdout + validated.stderr)
        (work / "workflow_status.json").write_text(
            json.dumps(
                {
                    "steps": {
                        name: {"status": "done"}
                        for name in ("source_analysis", "semantic_review", "deterministic_qa", "global_qc")
                    }
                }
            ),
            encoding="utf-8",
        )
        return work

    def test_validate_example_segments(self):
        out_dir = self.tmp_dir / "work"
        run_script(
            "extract_word_stream.py",
            str(EXAMPLES_DIR / "transcript_words.json"),
            "--out-dir",
            str(out_dir),
        )
        result = run_script(
            "validate_segments.py",
            str(out_dir / "word_table.json"),
            str(EXAMPLES_DIR / "segments.txt"),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Full word coverage: yes", result.stdout)

    def test_export_subtitles_shapes(self):
        aligned = self.build_aligned()
        work = self.prepare_delivery_evidence(aligned)
        out_dir = self.tmp_dir / "subtitles"
        result = run_script(
            "export_subtitles.py",
            str(aligned),
            "--work-dir",
            str(work),
            "--out-dir",
            str(out_dir),
            "--basename",
            "交易示例.中英双语字幕",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

        srt = (out_dir / "交易示例.中英双语字幕.srt").read_text(encoding="utf-8")
        self.assertIn("00:00:00,000 -->", srt)
        self.assertIn("如果价格突破前一日高点", srt)
        # Soft trailing punctuation must be stripped in display text.
        self.assertNotIn("追涨。", srt)
        first_block = srt.split("\n\n", 1)[0]
        self.assertRegex(first_block, r"\n[^\n]*[\u4e00-\u9fff][^\n]*\n[^\n]*[A-Za-z]")
        self.assertNotIn(r"\n", first_block)
        raw_srt = (out_dir / "交易示例.中英双语字幕.srt").read_bytes()
        self.assertIn(b"\r\n", raw_srt)
        self.assertNotIn(b"\n", raw_srt.replace(b"\r\n", b""))
        self.assertEqual(
            sorted(path.suffix for path in out_dir.iterdir()),
            [".ass", ".srt"],
        )

        ass = (out_dir / "交易示例.中英双语字幕.ass").read_text(encoding="utf-8")
        self.assertIn("Style: Default,Arial,42,", ass)
        for line in ass.splitlines():
            if line.startswith("Dialogue:"):
                self.assertIn(r"{\fs42}", line)
                self.assertIn(r"{\fs24}", line)
        # Chinese first by default: fs42 tag precedes fs24 within each event.
        first_dialogue = next(line for line in ass.splitlines() if line.startswith("Dialogue:"))
        self.assertLess(first_dialogue.index(r"{\fs42}"), first_dialogue.index(r"{\fs24}"))

    def test_export_subtitles_rejects_unfinished_run(self):
        aligned = self.build_aligned()
        result = run_script(
            "export_subtitles.py",
            str(aligned),
            "--work-dir",
            str(aligned.parent),
            "--out-dir",
            str(self.tmp_dir / "blocked"),
            "--basename",
            "blocked",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Export blocked", result.stdout + result.stderr)
        self.assertFalse((self.tmp_dir / "blocked").exists())

    def test_export_subtitles_rejects_waiting_workflow_status(self):
        aligned = self.build_aligned()
        work = self.prepare_delivery_evidence(aligned)
        status_path = work / "workflow_status.json"
        status = json.loads(status_path.read_text(encoding="utf-8"))
        status["steps"]["semantic_review"] = {"status": "waiting"}
        status_path.write_text(json.dumps(status), encoding="utf-8")
        result = run_script(
            "export_subtitles.py",
            str(aligned),
            "--work-dir",
            str(work),
            "--out-dir",
            str(self.tmp_dir / "blocked-waiting"),
            "--basename",
            "blocked",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("semantic_review", result.stdout + result.stderr)

    def _run_mocked_finalize(self, source_analysis_ready: bool) -> tuple[int, Path]:
        project = self.tmp_dir / "中文项目"
        project.mkdir(exist_ok=True)
        media = project / "中文标题.mp4"
        media.write_bytes(b"video")
        run_dir = project / ".work" / "run"
        work_dir = run_dir / "work"
        work_dir.mkdir(parents=True, exist_ok=True)
        (work_dir / "segments.txt").write_text("segments", encoding="utf-8")
        (work_dir / "word_table.json").write_text("{}", encoding="utf-8")
        finalized = {
            "work_dir": work_dir,
            "run_dir": run_dir,
            "media": media,
            "project_dir": project,
            "outputs_dir": project,
        }
        argv = [
            "finalize_run.py",
            str(run_dir),
            "--media",
            str(media),
            "--localized-title",
            "中文标题",
        ]
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(finalize_run, "validate_delivery_plan"),
            mock.patch.object(finalize_run, "bind_subtitle_layout"),
            mock.patch.object(finalize_run, "source_analysis_gate", return_value=source_analysis_ready),
            mock.patch.object(finalize_run, "semantic_review_gate", return_value=True),
            mock.patch.object(finalize_run, "run_deterministic_qa"),
            mock.patch.object(finalize_run, "final_qc_gate", return_value=True),
            mock.patch.object(finalize_run, "export_subtitle_files"),
            mock.patch.object(finalize_run, "finalize_visible_assets", return_value=finalized),
            mock.patch.object(finalize_run, "write_run_summary"),
        ):
            return finalize_run.main(), work_dir

    def test_finalize_marks_completed_run_done(self):
        result, work_dir = self._run_mocked_finalize(source_analysis_ready=True)
        self.assertEqual(result, 0)
        steps = json.loads((work_dir / "workflow_status.json").read_text(encoding="utf-8"))["steps"]
        self.assertEqual(steps["export"]["status"], "done")
        self.assertEqual(steps["visible_asset_naming"]["status"], "done")
        self.assertEqual(steps["start"]["status"], "done")

    def test_finalize_waiting_gate_keeps_run_running(self):
        result, work_dir = self._run_mocked_finalize(source_analysis_ready=False)
        self.assertEqual(result, 3)
        steps = json.loads((work_dir / "workflow_status.json").read_text(encoding="utf-8"))["steps"]
        self.assertEqual(steps["source_analysis"]["status"], "waiting")
        self.assertEqual(steps["start"]["status"], "running")
        self.assertNotIn("export", steps)

    def test_export_subtitles_rejects_layout_changed_after_qa(self):
        aligned = self.build_aligned()
        work = self.prepare_delivery_evidence(aligned)
        layout_path = work / "subtitle_layout.json"
        layout = json.loads(layout_path.read_text(encoding="utf-8"))
        layout["requested"] = "portrait"
        layout_path.write_text(json.dumps(layout), encoding="utf-8")
        result = run_script(
            "export_subtitles.py",
            str(aligned),
            "--work-dir",
            str(work),
            "--out-dir",
            str(self.tmp_dir / "blocked-layout"),
            "--basename",
            "交易示例.中英双语字幕",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("subtitle_layout", result.stdout + result.stderr)

    def test_export_allows_revalidated_semantic_review_after_workflow_input_cleanup(self):
        aligned = self.build_aligned()
        work = self.prepare_delivery_evidence(aligned)
        generation_meta = work / "segment_generation_meta.json"
        metadata = json.loads(generation_meta.read_text(encoding="utf-8"))
        metadata["translation_context_sha256"] = "stale-after-cleanup"
        generation_meta.write_text(json.dumps(metadata), encoding="utf-8")
        result = run_script(
            "export_subtitles.py",
            str(aligned),
            "--work-dir",
            str(work),
            "--out-dir",
            str(self.tmp_dir / "revalidated"),
            "--basename",
            "交易示例.中英双语字幕",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_export_subtitles_rejects_generic_or_changed_basename(self):
        aligned = self.build_aligned()
        work = self.prepare_delivery_evidence(aligned)
        result = run_script(
            "export_subtitles.py",
            str(aligned),
            "--work-dir",
            str(work),
            "--out-dir",
            str(self.tmp_dir / "blocked-generic"),
            "--basename",
            "原版视频.中英双语字幕",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must match the run-bound output name", result.stdout + result.stderr)
        self.assertFalse((self.tmp_dir / "blocked-generic").exists())

    def test_repair_terms(self):
        segments = self.tmp_dir / "segments.txt"
        segments.write_text(
            "[SEG 0001]\n"
            "SRC_RAW: welcome to trade pro academy on trading view\n"
            "SRC_DISPLAY: Welcome to Trade Pro Academy on Trading View.\n"
            "ZH: 欢迎来到 Trade Pro Academy，这里有黄牛、剥头皮交易员党和越狱的业绩。\n"
            "[/SEG]\n",
            encoding="utf-8",
        )
        result = run_script("repair_segments_terms.py", str(segments))
        self.assertEqual(result.returncode, 0, result.stderr)
        repaired = segments.read_text(encoding="utf-8")
        self.assertIn("TradePro Academy on TradingView", repaired)
        self.assertIn("剥头皮交易者", repaired)
        self.assertNotIn("剥头皮交易员党", repaired)
        self.assertIn("突破", repaired)
        self.assertNotIn("越狱", repaired)
        self.assertIn("交易表现", repaired)
        # SRC_RAW must stay untouched.
        self.assertIn("SRC_RAW: welcome to trade pro academy on trading view", repaired)

    def test_final_qa_clean_example(self):
        aligned = self.build_aligned()
        report = self.tmp_dir / "report.md"
        prompt = self.tmp_dir / "prompt.txt"
        result = run_script(
            "final_qa.py",
            str(aligned),
            str(EXAMPLES_DIR / "segments.txt"),
            "--out-report",
            str(report),
            "--out-prompt",
            str(prompt),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("blockers=0", result.stdout)
        self.assertTrue((report.parent / "final_qa.validated.json").exists())

    def test_final_qa_flags_coverage_gap(self):
        aligned_path = self.build_aligned()
        payload = json.loads(aligned_path.read_text(encoding="utf-8"))
        dropped = payload["segments"].pop(1)
        gapped = self.tmp_dir / "gapped.json"
        gapped.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        result = run_script(
            "final_qa.py",
            str(gapped),
            str(EXAMPLES_DIR / "segments.txt"),
            "--out-report",
            str(self.tmp_dir / "r.md"),
            "--out-prompt",
            str(self.tmp_dir / "p.txt"),
        )
        self.assertEqual(result.returncode, 1, result.stdout)
        report = (self.tmp_dir / "r.md").read_text(encoding="utf-8")
        self.assertIn("word_coverage_gap", report)
        self.assertIn(dropped["source_raw"].split()[0], report.lower())

    def test_external_processing_requires_explicit_confirmation(self):
        result = run_script(
            "generate_segments_with_dashscope.py",
            str(EXAMPLES_DIR / "transcript_words.json"),
            "--out",
            str(self.tmp_dir / "segments.txt"),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--confirm-external-processing", result.stdout + result.stderr)

    def test_cleanup_is_limited_to_a_marked_workflow_run(self):
        run_dir = self.tmp_dir / "project" / ".work" / "safe-run"
        work_dir = run_dir / "work"
        work_dir.mkdir(parents=True)
        (work_dir / "workflow_status.json").write_text("{}", encoding="utf-8")
        cache = work_dir / "dashscope_translation_cache.json"
        cache.write_text("{}", encoding="utf-8")

        missing_ack = run_script("clean_run.py", str(run_dir), "--confirm")
        self.assertNotEqual(missing_ack.returncode, 0)
        self.assertTrue(cache.exists())

        confirmed = run_script(
            "clean_run.py",
            str(run_dir),
            "--confirm",
            "--acknowledge-run-id",
            "safe-run",
        )
        self.assertEqual(confirmed.returncode, 0, confirmed.stderr)
        self.assertFalse(cache.exists())


if __name__ == "__main__":
    unittest.main()
