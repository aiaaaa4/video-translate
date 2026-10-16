import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = ROOT / "skills" / "video-translate"
SCRIPTS_DIR = SKILL_DIR / "scripts"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(SCRIPTS_DIR))

from common import parse_segments  # noqa: E402
from generate_segments_with_dashscope import load_cache, qwen_translation_options, save_cache  # noqa: E402


def run_script(name: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / name), *args],
        capture_output=True,
        text=True,
        cwd=SKILL_DIR,
    )


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def render(segment) -> str:
    return (
        f"[SEG {segment.index:04d}]\n"
        f"SRC_RAW: {segment.source_raw}\n"
        f"SRC_DISPLAY: {segment.source_display}\n"
        f"ZH: {segment.translation}\n"
        "[/SEG]\n"
    )


class GlobalReviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self.tmp.name)
        self.work = self.tmp_dir / "work"
        extract = run_script(
            "extract_word_stream.py",
            str(FIXTURES_DIR / "transcript_words.json"),
            "--out-dir",
            str(self.work),
        )
        self.assertEqual(extract.returncode, 0, extract.stdout + extract.stderr)
        self.initial = self.work / "segments.initial.txt"
        self.initial.write_text((FIXTURES_DIR / "segments.txt").read_text(encoding="utf-8"), encoding="utf-8")
        self.source_context = self.work / "translation-context.json"
        self.source_context.write_text(
            json.dumps({"stage": "translation-context", "domains": "trading", "terms": [], "tm_list": []}),
            encoding="utf-8",
        )
        self.semantic = self.work / "global_review" / "semantic"

    def tearDown(self):
        self.tmp.cleanup()

    def prepare_semantic(self):
        result = run_script(
            "global_review.py",
            "prepare-semantic",
            "--segments",
            str(self.initial),
            "--word-table",
            str(self.work / "word_table.json"),
            "--source-context",
            str(self.source_context),
            "--out-dir",
            str(self.semantic),
            "--max-section-segments",
            "1",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads((self.semantic / "manifest.json").read_text(encoding="utf-8"))

    def complete_semantic_review(self, manifest):
        segments = parse_segments(self.initial.read_text(encoding="utf-8"))
        reviewed_dir = self.semantic / "reviewed"
        reviews = []
        for section, segment in zip(manifest["sections"], segments):
            path = reviewed_dir / f"{section['id']}.txt"
            path.write_text(render(segment), encoding="utf-8")
            reviews.append(
                {
                    "id": section["id"],
                    "status": "passed",
                    "input_sha256": section["sha256"],
                    "output_sha256": sha256(path),
                    "same_segment_alignment": "passed",
                    "neighbor_window": 2,
                    "checked_segments": section["target_end"] - section["target_start"] + 1,
                    "shifted_segments": [],
                }
            )
        receipt = {
            "schema_version": 1,
            "stage": "semantic-review",
            "status": "passed",
            "model": "test-orchestrator",
            "initial_segments_sha256": manifest["initial_segments_sha256"],
            "source_context_sha256": manifest["source_context_sha256"],
            "manifest_sha256": manifest["manifest_sha256"],
            "section_reviews": reviews,
            "global_context": {
                "outline": ["Test outline"],
                "terminology": {"breakout": "突破"},
                "names_and_entities": {},
                "style_rules": ["Natural Chinese"],
                "cross_section_consistency_notes": [],
            },
            "unchanged_output_confirmation": {
                "confirmed": True,
                "reason": "Compared every target ZH with its own source and the two neighboring segments.",
            },
        }
        path = self.semantic / "semantic-review-receipt.json"
        path.write_text(json.dumps(receipt, ensure_ascii=False), encoding="utf-8")
        return path

    def test_semantic_review_requires_complete_hashed_coverage(self):
        manifest = self.prepare_semantic()
        receipt = self.complete_semantic_review(manifest)
        out = self.semantic / "segments.global-reviewed.txt"
        result = run_script(
            "global_review.py",
            "validate-semantic",
            "--manifest",
            str(self.semantic / "manifest.json"),
            "--receipt",
            str(receipt),
            "--reviewed-dir",
            str(self.semantic / "reviewed"),
            "--out",
            str(out),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            " ".join(s.source_raw for s in parse_segments(out.read_text(encoding="utf-8"))),
            " ".join(s.source_raw for s in parse_segments(self.initial.read_text(encoding="utf-8"))),
        )
        self.assertTrue((self.semantic / "semantic-review.validated.json").exists())

    def test_source_analysis_creates_qwen_domain_terms_and_memory(self):
        source_dir = self.work / "global_review" / "source-analysis"
        prepared = run_script(
            "source_analysis.py",
            "prepare",
            "--transcript",
            str(FIXTURES_DIR / "transcript_words.json"),
            "--out-dir",
            str(source_dir),
            "--max-section-chunks",
            "1",
        )
        self.assertEqual(prepared.returncode, 0, prepared.stdout + prepared.stderr)
        manifest = json.loads((source_dir / "manifest.json").read_text(encoding="utf-8"))
        receipt = json.loads((source_dir / "source-analysis-receipt.template.json").read_text(encoding="utf-8"))
        receipt.update(status="passed", model="test-orchestrator")
        for review in receipt["section_reviews"]:
            review["status"] = "passed"
        receipt["analysis"] = {
            "outline": ["A trading lesson about breakouts."],
            "domain_summary": "Finance and trading education.",
            "domains_prompt": "Translate as professional financial-market education in concise Simplified Chinese.",
            "terminology": [{"source": "model", "target": "模型"}, {"source": "breakout", "target": "突破"}],
            "names_and_entities": {},
            "ambiguity_decisions": ["model means analytical model, not fashion model"],
            "style_rules": ["Use concise Simplified Chinese."],
            "tm_list": [{"source": "do not chase", "target": "不要追涨"}],
        }
        receipt_path = source_dir / "source-analysis-receipt.json"
        receipt_path.write_text(json.dumps(receipt, ensure_ascii=False), encoding="utf-8")
        context_path = source_dir / "translation-context.json"
        validated = run_script(
            "source_analysis.py",
            "validate",
            "--manifest",
            str(source_dir / "manifest.json"),
            "--receipt",
            str(receipt_path),
            "--out",
            str(context_path),
        )
        self.assertEqual(validated.returncode, 0, validated.stdout + validated.stderr)
        context = json.loads(context_path.read_text(encoding="utf-8"))
        options = qwen_translation_options("English", context)
        self.assertIn("financial-market", options["domains"])
        self.assertIn({"source": "model", "target": "模型"}, options["terms"])
        self.assertEqual(options["tm_list"], [{"source": "do not chase", "target": "不要追涨"}])
        self.assertEqual(manifest["stage"], "source-analysis")

    def test_translation_cache_is_bound_to_source_context_hash(self):
        cache = self.tmp_dir / "translations.json"
        save_cache(cache, {"segment": "译文"}, "context-a")
        self.assertEqual(load_cache(cache, "context-a"), {"segment": "译文"})
        self.assertEqual(load_cache(cache, "context-b"), {})

    def test_semantic_review_rejects_file_changed_after_receipt(self):
        manifest = self.prepare_semantic()
        receipt = self.complete_semantic_review(manifest)
        changed = self.semantic / "reviewed" / "section-001.txt"
        changed.write_text(changed.read_text(encoding="utf-8").replace("ZH:", "ZH: 已更改 ", 1), encoding="utf-8")
        result = run_script(
            "global_review.py",
            "validate-semantic",
            "--manifest",
            str(self.semantic / "manifest.json"),
            "--receipt",
            str(receipt),
            "--reviewed-dir",
            str(self.semantic / "reviewed"),
            "--out",
            str(self.semantic / "out.txt"),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("output hash", result.stdout)

    def test_semantic_review_rejects_missing_neighbor_alignment_check(self):
        manifest = self.prepare_semantic()
        receipt = self.complete_semantic_review(manifest)
        payload = json.loads(receipt.read_text(encoding="utf-8"))
        payload["section_reviews"][0]["neighbor_window"] = 0
        receipt.write_text(json.dumps(payload), encoding="utf-8")
        result = run_script(
            "global_review.py",
            "validate-semantic",
            "--manifest",
            str(self.semantic / "manifest.json"),
            "--receipt",
            str(receipt),
            "--reviewed-dir",
            str(self.semantic / "reviewed"),
            "--out",
            str(self.semantic / "out.txt"),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("neighbor check", result.stdout)

    def test_semantic_review_rejects_reported_cross_segment_shift(self):
        manifest = self.prepare_semantic()
        receipt = self.complete_semantic_review(manifest)
        payload = json.loads(receipt.read_text(encoding="utf-8"))
        payload["section_reviews"][0]["shifted_segments"] = [
            {"seg": "0001", "better_match": "0000", "offset": -1}
        ]
        receipt.write_text(json.dumps(payload), encoding="utf-8")
        result = run_script(
            "global_review.py",
            "validate-semantic",
            "--manifest",
            str(self.semantic / "manifest.json"),
            "--receipt",
            str(receipt),
            "--reviewed-dir",
            str(self.semantic / "reviewed"),
            "--out",
            str(self.semantic / "out.txt"),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no unresolved shifts", result.stdout)

    def test_semantic_review_rejects_unconfirmed_all_unchanged_output(self):
        manifest = self.prepare_semantic()
        receipt = self.complete_semantic_review(manifest)
        payload = json.loads(receipt.read_text(encoding="utf-8"))
        payload["unchanged_output_confirmation"] = {"confirmed": False, "reason": ""}
        receipt.write_text(json.dumps(payload), encoding="utf-8")
        result = run_script(
            "global_review.py",
            "validate-semantic",
            "--manifest",
            str(self.semantic / "manifest.json"),
            "--receipt",
            str(receipt),
            "--reviewed-dir",
            str(self.semantic / "reviewed"),
            "--out",
            str(self.semantic / "out.txt"),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Every semantic-review section is unchanged", result.stdout)

    def test_final_qc_rejects_stale_or_incomplete_receipt(self):
        manifest = self.prepare_semantic()
        semantic_receipt = self.complete_semantic_review(manifest)
        reviewed = self.semantic / "segments.global-reviewed.txt"
        validated = run_script(
            "global_review.py",
            "validate-semantic",
            "--manifest",
            str(self.semantic / "manifest.json"),
            "--receipt",
            str(semantic_receipt),
            "--reviewed-dir",
            str(self.semantic / "reviewed"),
            "--out",
            str(reviewed),
        )
        self.assertEqual(validated.returncode, 0, validated.stdout + validated.stderr)
        qa_report = self.work / "final_qa_report.md"
        qa_report.write_text("# Final QA Report\n\n## Summary\n\n- Blockers: 0\n", encoding="utf-8")
        qc_dir = self.work / "global_review" / "final-qc"
        prepared = run_script(
            "global_review.py",
            "prepare-qc",
            "--segments",
            str(reviewed),
            "--qa-report",
            str(qa_report),
            "--global-context",
            str(self.semantic / "global-context.json"),
            "--out-dir",
            str(qc_dir),
        )
        self.assertEqual(prepared.returncode, 0, prepared.stdout + prepared.stderr)
        qc_manifest = json.loads((qc_dir / "manifest.json").read_text(encoding="utf-8"))
        receipt = json.loads((qc_dir / "final-qc-receipt.template.json").read_text(encoding="utf-8"))
        receipt.update(status="passed", model="test-orchestrator")
        for section_review in receipt["section_reviews"]:
            section_review["status"] = "passed"
        receipt["checks"] = {name: "passed" for name in receipt["checks"]}
        receipt["spot_checks"] = {
            name: {"status": "passed", "notes": f"Checked {name}."}
            for name in receipt["spot_checks"]
        }
        receipt_path = qc_dir / "final-qc-receipt.json"
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        passed = run_script(
            "global_review.py",
            "validate-qc",
            "--manifest",
            str(qc_dir / "manifest.json"),
            "--receipt",
            str(receipt_path),
        )
        self.assertEqual(passed.returncode, 0, passed.stdout + passed.stderr)

        receipt["segments_sha256"] = "stale"
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        stale = run_script(
            "global_review.py",
            "validate-qc",
            "--manifest",
            str(qc_dir / "manifest.json"),
            "--receipt",
            str(receipt_path),
        )
        self.assertNotEqual(stale.returncode, 0)
        self.assertEqual(qc_manifest["stage"], "final-qc")
        self.assertIn("does not match", stale.stdout)


if __name__ == "__main__":
    unittest.main()
