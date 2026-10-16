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
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "transcript_words.json"


def run_script(name: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / name), *args],
        cwd=SKILL_DIR,
        capture_output=True,
        text=True,
        check=False,
    )


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class AgentTranslationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source_dir = self.root / "source-analysis"
        prepared = run_script(
            "source_analysis.py",
            "prepare",
            "--transcript",
            str(FIXTURE),
            "--out-dir",
            str(self.source_dir),
            "--max-section-chunks",
            "1",
        )
        self.assertEqual(prepared.returncode, 0, prepared.stdout + prepared.stderr)
        receipt = json.loads((self.source_dir / "source-analysis-receipt.template.json").read_text(encoding="utf-8"))
        receipt.update(status="passed", model="test-agent")
        for review in receipt["section_reviews"]:
            review["status"] = "passed"
        receipt["analysis"] = {
            "outline": ["A complete test transcript."],
            "domain_summary": "Test subtitle translation.",
            "domains_prompt": "Translate the complete test transcript into natural Simplified Chinese.",
            "terminology": [{"source": "model", "target": "模型"}],
            "names_and_entities": {},
            "ambiguity_decisions": ["model means analytical model"],
            "style_rules": ["Use concise Simplified Chinese."],
            "tm_list": [],
        }
        source_receipt = self.source_dir / "source-analysis-receipt.json"
        source_receipt.write_text(json.dumps(receipt, ensure_ascii=False), encoding="utf-8")
        context = self.source_dir / "translation-context.json"
        validated = run_script(
            "source_analysis.py",
            "validate",
            "--manifest",
            str(self.source_dir / "manifest.json"),
            "--receipt",
            str(source_receipt),
            "--out",
            str(context),
        )
        self.assertEqual(validated.returncode, 0, validated.stdout + validated.stderr)
        self.context = context
        self.translation_dir = self.root / "agent-translation"
        prepared_translation = run_script(
            "agent_translation.py",
            "prepare",
            "--source-manifest",
            str(self.source_dir / "manifest.json"),
            "--translation-context",
            str(self.context),
            "--out-dir",
            str(self.translation_dir),
        )
        self.assertEqual(prepared_translation.returncode, 0, prepared_translation.stdout + prepared_translation.stderr)

    def tearDown(self):
        self.temp.cleanup()

    def complete_outputs(self, mutate=None) -> Path:
        manifest = json.loads((self.translation_dir / "manifest.json").read_text(encoding="utf-8"))
        receipt = json.loads((self.translation_dir / "agent-translation-receipt.template.json").read_text(encoding="utf-8"))
        receipt.update(status="passed", model="codex-test-model")
        translated = self.translation_dir / "translated"
        translated.mkdir(parents=True, exist_ok=True)
        for index, (section, review) in enumerate(zip(manifest["sections"], receipt["section_reviews"])):
            source = Path(section["path"]).read_text(encoding="utf-8")
            output = source.replace("__TRANSLATE_TO_SIMPLIFIED_CHINESE__", "这是经过全文理解的测试译文")
            if mutate and index == 0:
                output = mutate(output)
            target = translated / f"{section['id']}.txt"
            target.write_text(output, encoding="utf-8")
            review.update(
                status="passed",
                output_sha256=sha256(target),
                same_segment_alignment="passed",
                checked_segments=section["target_end"] - section["target_start"] + 1,
            )
        receipt_path = self.translation_dir / "agent-translation-receipt.json"
        receipt_path.write_text(json.dumps(receipt, ensure_ascii=False), encoding="utf-8")
        return receipt_path

    def validate(self, receipt: Path) -> subprocess.CompletedProcess[str]:
        return run_script(
            "agent_translation.py",
            "validate",
            "--manifest",
            str(self.translation_dir / "manifest.json"),
            "--receipt",
            str(receipt),
            "--translated-dir",
            str(self.translation_dir / "translated"),
            "--out",
            str(self.root / "segments.txt"),
            "--meta-out",
            str(self.root / "segment_generation_meta.json"),
        )

    def test_agent_translation_is_complete_hash_bound_and_resumable(self):
        result = self.validate(self.complete_outputs())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        meta = json.loads((self.root / "segment_generation_meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["translation_provider"], "agent")
        self.assertEqual(meta["model"], "codex-test-model")
        self.assertGreater(meta["segments"], 0)
        self.assertIn("ZH: 这是经过全文理解的测试译文", (self.root / "segments.txt").read_text(encoding="utf-8"))

    def test_agent_translation_cannot_modify_source_text(self):
        receipt = self.complete_outputs(lambda text: text.replace("SRC_DISPLAY:", "SRC_DISPLAY: changed ", 1))
        result = self.validate(receipt)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("modified source text", result.stdout)

    def test_agent_translation_requires_same_segment_alignment_check(self):
        receipt = self.complete_outputs()
        payload = json.loads(receipt.read_text(encoding="utf-8"))
        payload["section_reviews"][0]["same_segment_alignment"] = "replace-with-passed"
        receipt.write_text(json.dumps(payload), encoding="utf-8")
        result = self.validate(receipt)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("same-SEG semantic alignment", result.stdout)


if __name__ == "__main__":
    unittest.main()
