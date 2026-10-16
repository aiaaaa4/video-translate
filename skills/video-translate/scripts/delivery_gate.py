#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


REQUIRED_WORKFLOW_STEPS = {
    "source_analysis",
    "semantic_review",
    "deterministic_qa",
    "global_qc",
}


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json_object(path: Path, label: str) -> dict:
    if not path.is_file():
        raise RuntimeError(f"Export blocked: missing {label}: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Export blocked: invalid {label}: {path}") from error
    if not isinstance(payload, dict):
        raise RuntimeError(f"Export blocked: {label} must be a JSON object: {path}")
    return payload


def require_passed(payload: dict, stage: str, path: Path) -> None:
    if payload.get("stage") != stage or payload.get("status") != "passed":
        raise RuntimeError(f"Export blocked: {path.name} is not a passed {stage} validation.")


def manifest_digest(manifest: dict) -> str:
    canonical = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def assert_export_ready(work_dir: Path, aligned_segments: Path) -> dict[str, object]:
    work_dir = work_dir.resolve()
    aligned_segments = aligned_segments.resolve()
    expected_aligned = (work_dir / "aligned_segments.json").resolve()
    if aligned_segments != expected_aligned:
        raise RuntimeError(
            "Export blocked: aligned input must be the current run's work/aligned_segments.json."
        )
    segments_path = work_dir / "segments.txt"
    report_path = work_dir / "final_qa_report.md"
    layout_path = work_dir / "subtitle_layout.json"
    if not segments_path.is_file() or not aligned_segments.is_file() or not report_path.is_file():
        raise RuntimeError("Export blocked: current segments, aligned segments, and final QA report are required.")
    if not layout_path.is_file():
        raise RuntimeError("Export blocked: missing immutable subtitle layout binding.")

    source_path = work_dir / "global_review" / "source-analysis" / "source-analysis.validated.json"
    source = read_json_object(source_path, "source-analysis validation")
    require_passed(source, "source-analysis", source_path)
    translation_context = work_dir / "global_review" / "source-analysis" / "translation-context.json"
    if (
        not translation_context.is_file()
        or source.get("translation_context_sha256") != sha256_file(translation_context)
    ):
        raise RuntimeError("Export blocked: source-analysis validation is stale for translation-context.json.")

    workflow_config_path = work_dir / "workflow_config.json"
    workflow_config = read_json_object(workflow_config_path, "workflow configuration")
    translation_provider = str(workflow_config.get("translation_provider") or "")
    if translation_provider not in {"qwen-mt-plus", "agent"}:
        raise RuntimeError("Export blocked: run has no valid bound translation provider.")
    generation_meta_path = work_dir / "segment_generation_meta.json"
    generation_meta = read_json_object(generation_meta_path, "segment-generation metadata")
    if generation_meta.get("translation_provider") != translation_provider:
        raise RuntimeError("Export blocked: segment-generation metadata does not match the bound provider.")
    initial_translation_context_stale = generation_meta.get("translation_context_sha256") != sha256_file(translation_context)
    if translation_provider == "agent":
        agent_path = work_dir / "global_review" / "agent-translation" / "agent-translation.validated.json"
        agent = read_json_object(agent_path, "agent-translation validation")
        require_passed(agent, "agent-translation", agent_path)
        initial_segments = work_dir / "segments.initial.txt"
        if not initial_segments.is_file() or agent.get("segments_sha256") != sha256_file(initial_segments):
            raise RuntimeError("Export blocked: Agent-translation validation is stale for the semantic-review input.")

    semantic_path = work_dir / "global_review" / "semantic" / "semantic-review.validated.json"
    semantic = read_json_object(semantic_path, "semantic-review validation")
    require_passed(semantic, "semantic-review", semantic_path)
    if not isinstance(semantic.get("all_content_unchanged"), bool):
        raise RuntimeError("Export blocked: semantic-review validation lacks the no-change audit result.")
    reviewed_path = Path(str(semantic.get("reviewed_segments") or ""))
    if (
        not reviewed_path.is_file()
        or semantic.get("reviewed_segments_sha256") != sha256_file(reviewed_path)
    ):
        raise RuntimeError("Export blocked: semantic-review validation is stale for its reviewed segments.")
    if initial_translation_context_stale:
        semantic_manifest_path = work_dir / "global_review" / "semantic" / "manifest.json"
        semantic_manifest = read_json_object(semantic_manifest_path, "semantic-review manifest")
        if (
            semantic_manifest.get("manifest_sha256") != manifest_digest(semantic_manifest)
            or Path(str(semantic_manifest.get("source_context") or "")).resolve() != translation_context.resolve()
            or semantic_manifest.get("source_context_sha256") != sha256_file(translation_context)
            or semantic_manifest.get("initial_segments_sha256") != sha256_file(work_dir / "segments.initial.txt")
        ):
            raise RuntimeError("Export blocked: initial translation is stale for the whole-source context.")

    qa_path = work_dir / "final_qa.validated.json"
    qa = read_json_object(qa_path, "deterministic final-QA validation")
    require_passed(qa, "deterministic-final-qa", qa_path)
    if qa.get("blockers") != 0:
        raise RuntimeError("Export blocked: deterministic final QA has blockers.")
    if Path(str(qa.get("segments") or "")).resolve() != segments_path.resolve():
        raise RuntimeError("Export blocked: final-QA validation points to different segments.")
    if Path(str(qa.get("aligned_segments") or "")).resolve() != aligned_segments:
        raise RuntimeError("Export blocked: final-QA validation points to different aligned segments.")
    if Path(str(qa.get("report") or "")).resolve() != report_path.resolve():
        raise RuntimeError("Export blocked: final-QA validation points to a different report.")
    if qa.get("segments_sha256") != sha256_file(segments_path):
        raise RuntimeError("Export blocked: final-QA validation is stale for segments.txt.")
    if qa.get("aligned_segments_sha256") != sha256_file(aligned_segments):
        raise RuntimeError("Export blocked: final-QA validation is stale for aligned_segments.json.")
    if qa.get("report_sha256") != sha256_file(report_path):
        raise RuntimeError("Export blocked: final-QA validation is stale for final_qa_report.md.")
    if Path(str(qa.get("subtitle_layout") or "")).resolve() != layout_path.resolve():
        raise RuntimeError("Export blocked: final-QA validation points to a different subtitle layout.")
    if qa.get("subtitle_layout_sha256") != sha256_file(layout_path):
        raise RuntimeError("Export blocked: final-QA validation is stale for subtitle_layout.json.")
    blockers_match = re.search(r"(?m)^- Blockers:\s*(\d+)\s*$", report_path.read_text(encoding="utf-8"))
    if not blockers_match or int(blockers_match.group(1)) != 0:
        raise RuntimeError("Export blocked: final_qa_report.md must record Blockers: 0.")

    qc_dir = work_dir / "global_review" / "final-qc"
    qc_path = qc_dir / "final-qc.validated.json"
    qc = read_json_object(qc_path, "final-QC validation")
    require_passed(qc, "final-qc", qc_path)
    if qc.get("segments_sha256") != sha256_file(segments_path):
        raise RuntimeError("Export blocked: final-QC validation is stale for segments.txt.")
    checks = qc.get("checks")
    if not isinstance(checks, dict) or checks.get("cross_segment_semantic_alignment") != "passed":
        raise RuntimeError("Export blocked: final QC lacks a passed cross-segment semantic-alignment check.")
    spot_checks = qc.get("spot_checks")
    if not isinstance(spot_checks, dict) or "opening_30" not in spot_checks:
        raise RuntimeError("Export blocked: final QC lacks the fixed spot checks.")
    manifest_path = qc_dir / "manifest.json"
    manifest = read_json_object(manifest_path, "final-QC manifest")
    if manifest.get("manifest_sha256") != manifest_digest(manifest):
        raise RuntimeError("Export blocked: final-QC manifest hash is invalid.")
    if qc.get("manifest_sha256") != manifest.get("manifest_sha256"):
        raise RuntimeError("Export blocked: final-QC validation does not match its manifest.")
    if qc.get("qa_report_sha256") != sha256_file(report_path):
        raise RuntimeError("Export blocked: final-QC validation is stale for final_qa_report.md.")
    global_context_path = work_dir / "global_review" / "semantic" / "global-context.json"
    if (
        not global_context_path.is_file()
        or qc.get("global_context_sha256") != sha256_file(global_context_path)
    ):
        raise RuntimeError("Export blocked: final-QC validation is stale for semantic global context.")

    status_path = work_dir / "workflow_status.json"
    if status_path.exists():
        status = read_json_object(status_path, "workflow status")
        steps = status.get("steps")
        if not isinstance(steps, dict):
            raise RuntimeError("Export blocked: workflow_status.json has no valid steps.")
        required_steps = set(REQUIRED_WORKFLOW_STEPS)
        if translation_provider == "agent":
            required_steps.add("ai_segments")
        missing = sorted(step for step in required_steps if step not in steps)
        if missing:
            raise RuntimeError("Export blocked: workflow status is missing gates: " + ", ".join(missing))
        incomplete = sorted(
            step
            for step in required_steps
            if not isinstance(steps[step], dict) or steps[step].get("status") != "done"
        )
        if incomplete:
            raise RuntimeError(
                "Export blocked: workflow gates are not done: " + ", ".join(incomplete)
            )

    return {
        "translation_provider": translation_provider,
        "segments_sha256": sha256_file(segments_path),
        "aligned_segments_sha256": sha256_file(aligned_segments),
        "qa_report_sha256": sha256_file(report_path),
        "subtitle_layout_sha256": sha256_file(layout_path),
        "final_qc_manifest_sha256": manifest["manifest_sha256"],
    }
