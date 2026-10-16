#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from pathlib import Path

from common import (
    FILLER_RAW_TOKENS,
    FILLER_ZH_TEXT,
    HANGING_RAW_END_WORDS,
    HANGING_RAW_START_WORDS,
    parse_segments,
    read_json,
    split_chinese_line,
    split_source_line,
    text_width,
    tokenize_raw,
    visible_len,
)
from subtitle_layout import load_bound_subtitle_layout


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GLOSSARY = PROJECT_ROOT / "references" / "trading_glossary.md"
DEFAULT_QA_RULES = PROJECT_ROOT / "references" / "term_repair_rules.json"

ALLOWED_ZH_LATIN_TOKENS = {
    "ai", "api", "asr", "ass", "srt", "ocr", "ui",
    "delta", "cvd", "fvg", "poc", "vsa", "choch",
    "rsi", "macd", "cci", "vwap", "atr", "ema", "sma",
    "long", "short", "call", "put", "bid", "ask",
    "nasdaq", "spx", "spy", "qqq", "aapl", "tsla", "nflx",
    "tradingview", "tradepro", "deep", "charts",
}


def suspicious_latin_tokens_in_zh(text: str) -> list[str]:
    tokens = re.findall(r"[A-Za-z][A-Za-z'’-]{1,}", text)
    suspicious: list[str] = []
    for token in tokens:
        normalized = token.strip("'’-_").lower()
        if not normalized or normalized in ALLOWED_ZH_LATIN_TOKENS:
            continue
        if token.isupper() and 2 <= len(token) <= 6:
            continue
        if re.fullmatch(r"[A-Z][a-z]{0,4}", token) and len(token) <= 5:
            continue
        suspicious.append(token)
    return suspicious


def load_qa_rules(path: Path | None) -> tuple[list[tuple[str, str]], list[tuple[re.Pattern, str]]]:
    """Load domain QA rules (bad Chinese terms, ASR split-display patterns) from the rules JSON.

    Both lists live in the same file as the automatic repair rules so a custom
    domain only has to provide one rules file. Missing keys mean no checks.
    """
    if not path or not path.exists():
        return [], []
    data = read_json(path)
    bad_terms = [(rule["term"], rule.get("note", "")) for rule in data.get("qa_bad_zh_terms", [])]
    split_patterns = [
        (re.compile(rule["pattern"], re.I), rule["preferred"])
        for rule in data.get("qa_split_display_patterns", [])
    ]
    return bad_terms, split_patterns


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate final QA report and AI review prompt.")
    parser.add_argument("aligned_segments", type=Path)
    parser.add_argument("segments_txt", type=Path)
    parser.add_argument("--out-report", type=Path, default=None)
    parser.add_argument("--out-prompt", type=Path, default=None)
    parser.add_argument("--min-duration", type=float, default=0.5)
    parser.add_argument("--max-duration", type=float, default=7.0)
    parser.add_argument("--max-source-words", type=int, default=24)
    parser.add_argument("--min-source-words", type=int, default=3)
    parser.add_argument("--max-zh-chars", type=int, default=108)
    parser.add_argument("--max-source-chars", type=int, default=0, help="Deprecated; source length is not a visual blocker.")
    parser.add_argument("--zh-chars-per-line", type=int, default=36)
    parser.add_argument("--source-chars-per-line", type=float, default=0, help="Estimated source-language wrap width.")
    parser.add_argument("--max-visual-lines", type=int, default=3, help="Maximum estimated Chinese subtitle lines.")
    parser.add_argument("--max-source-visual-lines", type=int, default=0, help="Maximum source-language lines; zero disables the check.")
    parser.add_argument(
        "--subtitle-layout-record",
        type=Path,
        default=None,
        help="Immutable subtitle_layout.json used to apply orientation-specific wrapping and QA.",
    )
    parser.add_argument("--max-zh-cps", type=float, default=13.0)
    parser.add_argument("--max-en-cps", type=float, default=30.0)
    parser.add_argument(
        "--domain-name",
        default="finance/trading training videos",
        help="Domain/style label used in the QA report and review prompt.",
    )
    parser.add_argument(
        "--glossary",
        type=Path,
        default=DEFAULT_GLOSSARY,
        help="Domain glossary/style guide injected into the review prompt.",
    )
    parser.add_argument(
        "--disable-domain-term-checks",
        action="store_true",
        help="Disable the built-in finance/trading bad-term warnings for non-trading domains.",
    )
    parser.add_argument(
        "--qa-rules",
        type=Path,
        default=DEFAULT_QA_RULES,
        help="Rules JSON providing qa_bad_zh_terms and qa_split_display_patterns.",
    )
    return parser.parse_args()


def issue(segment: dict, severity: str, kind: str, message: str) -> dict:
    return {
        "severity": severity,
        "kind": kind,
        "index": segment.get("index"),
        "start": segment.get("start"),
        "end": segment.get("end"),
        "message": message,
        "zh": segment.get("translation", ""),
        "src_display": segment.get("source_display", ""),
        "src_raw": segment.get("source_raw", ""),
    }


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def collect_issues(
    payload: dict,
    min_duration: float,
    max_duration: float,
    max_source_words: int,
    min_source_words: int,
    max_zh_chars: int,
    max_source_chars: int,
    zh_chars_per_line: int,
    source_chars_per_line: float,
    max_visual_lines: int,
    max_source_visual_lines: int,
    max_zh_cps: float,
    max_en_cps: float,
    check_domain_terms: bool,
    bad_zh_terms: list[tuple[str, str]],
    split_display_patterns: list[tuple[re.Pattern, str]],
) -> list[dict]:
    segments = payload["segments"]
    failures = payload.get("failures", [])
    issues: list[dict] = []

    for failure in failures:
        issues.append(
            {
                "severity": "blocker",
                "kind": "match_failure",
                "index": failure.get("index"),
                "message": failure.get("reason", "Segment failed to match word table."),
                "src_raw": failure.get("source_raw", ""),
                "src_display": "",
                "zh": "",
            }
        )

    if segments:
        if int(segments[0].get("word_start_id", -1)) != 0:
            issues.append(
                {
                    "severity": "blocker",
                    "kind": "word_coverage_gap",
                    "index": segments[0].get("index"),
                    "start": segments[0].get("start"),
                    "end": segments[0].get("end"),
                    "message": "Word coverage does not start at the first ASR word. Check for missed opening words.",
                    "zh": segments[0].get("translation", ""),
                    "src_display": segments[0].get("source_display", ""),
                    "src_raw": segments[0].get("source_raw", ""),
                }
            )
        for previous, current in zip(segments, segments[1:]):
            expected_start = int(previous.get("word_end_id", -1)) + 1
            actual_start = int(current.get("word_start_id", -1))
            if actual_start != expected_start:
                issues.append(
                    {
                        "severity": "blocker",
                        "kind": "word_coverage_gap",
                        "index": current.get("index"),
                        "start": current.get("start"),
                        "end": current.get("end"),
                        "message": (
                            f"Word coverage is not continuous between SEG {int(previous.get('index')):04d} "
                            f"and SEG {int(current.get('index')):04d}. Check for a missed ASR fragment."
                        ),
                        "zh": current.get("translation", ""),
                        "src_display": current.get("source_display", ""),
                        "src_raw": current.get("source_raw", ""),
                    }
                )
        total_words = payload.get("total_words")
        if total_words is not None and int(segments[-1].get("word_end_id", -1)) != int(total_words) - 1:
            issues.append(
                {
                    "severity": "blocker",
                    "kind": "word_coverage_gap",
                    "index": segments[-1].get("index"),
                    "start": segments[-1].get("start"),
                    "end": segments[-1].get("end"),
                    "message": "Word coverage does not end at the final ASR word. Check for missed closing words.",
                    "zh": segments[-1].get("translation", ""),
                    "src_display": segments[-1].get("source_display", ""),
                    "src_raw": segments[-1].get("source_raw", ""),
                }
            )

    for i, segment in enumerate(segments):
        source_word_count = len(tokenize_raw(segment.get("source_raw", "")))
        if source_word_count > max_source_words:
            issues.append(
                issue(
                    segment,
                    "warning",
                    "long_source_segment",
                    f"SRC_RAW has {source_word_count} words; review only if the Chinese subtitle is too long.",
                )
            )
        elif source_word_count < min_source_words:
            issues.append(
                issue(
                    segment,
                    "info",
                    "micro_source_segment",
                    f"SRC_RAW has only {source_word_count} word(s); consider merging with a neighbor if it is just oral filler.",
                )
            )

        zh_text = segment.get("translation", "")
        zh_len = len(zh_text)
        wrapped_zh_lines = split_chinese_line(zh_text, zh_chars_per_line)
        zh_lines = len(wrapped_zh_lines)
        if zh_len > max_zh_chars or zh_lines > max_visual_lines:
            issues.append(
                issue(
                    segment,
                    "blocker",
                    "visual_overflow",
                    (
                        "Chinese subtitle is likely to occupy too much screen space after actual wrapping: "
                        f"ZH {zh_len} chars, {zh_lines} wrapped Chinese line(s). "
                        "Split only at a weak semantic boundary after re-understanding the whole segment."
                    ),
                )
            )
        if zh_lines > 1 and visible_len(wrapped_zh_lines[-1]) < 4:
            issues.append(
                issue(
                    segment,
                    "warning",
                    "short_final_zh_line",
                    "Final wrapped Chinese line is shorter than 4 visible characters; merge the tail into the previous line or rephrase.",
                )
            )

        if source_chars_per_line > 0 and max_source_visual_lines > 0:
            source_lines = split_source_line(segment.get("source_display", ""), source_chars_per_line)
            if len(source_lines) > max_source_visual_lines:
                issues.append(
                    issue(
                        segment,
                        "blocker",
                        "source_visual_overflow",
                        (
                            "Source subtitle exceeds the selected video layout after deterministic wrapping: "
                            f"{len(source_lines)} line(s), maximum {max_source_visual_lines}. "
                            "Split the SEG only at a weak semantic boundary."
                        ),
                    )
                )

        raw_tokens = tokenize_raw(segment.get("source_raw", ""))
        display_tokens = tokenize_raw(segment.get("source_display", ""))
        if raw_tokens and len(display_tokens) > len(raw_tokens) + max(8, math.ceil(len(raw_tokens) * 0.35)):
            issues.append(
                issue(
                    segment,
                    "blocker",
                    "display_scope_drift",
                    (
                        "SRC_DISPLAY appears to contain substantially more source-language content than SRC_RAW. "
                        "Do not borrow text or meaning from the next segment; split/merge SRC_RAW or shorten SRC_DISPLAY/ZH to this segment's word span."
                    ),
                )
            )
        if raw_tokens and raw_tokens[-1] in HANGING_RAW_END_WORDS and source_word_count >= 5:
            issues.append(
                issue(
                    segment,
                    "warning",
                    "hanging_source_boundary",
                    (
                        f"SRC_RAW ends with a connective/function word `{raw_tokens[-1]}`. "
                        "Review whether this cue should merge with the next segment or keep an intentionally unfinished Chinese/source line."
                    ),
                )
            )

        if raw_tokens and raw_tokens[0] in HANGING_RAW_START_WORDS and source_word_count <= 6:
            issues.append(
                issue(
                    segment,
                    "blocker",
                    "continuation_fragment",
                    (
                        f"SRC_RAW starts with a continuation word `{raw_tokens[0]}` and is very short. "
                        "It likely completes the previous cue; merge it unless it is truly independent."
                    ),
                )
            )

        duration = float(segment["end"]) - float(segment["start"])
        if (
            duration < 0.8
            and 1 <= len(raw_tokens) <= 2
            and all(token in FILLER_RAW_TOKENS for token in raw_tokens)
            and segment.get("translation", "").strip("。,.， ") in FILLER_ZH_TEXT
        ):
            issues.append(
                issue(
                    segment,
                    "blocker",
                    "isolated_filler_segment",
                    "Very short oral filler is isolated as its own subtitle. Merge it into a neighboring SRC_RAW span or absorb it silently in the neighboring translation.",
                )
            )
        elif duration < 0.8 and (zh_len <= 8 or source_word_count <= 4):
            issues.append(
                issue(
                    segment,
                    "blocker",
                    "flash_subtitle",
                    "Subtitle is under 0.8s and too weak/short to read comfortably. Merge it with a neighboring segment unless it carries essential independent meaning.",
                )
            )

        if source_word_count <= 3 and zh_len > 20:
            issues.append(
                issue(
                    segment,
                    "blocker",
                    "translation_scope_drift",
                    "SRC_RAW is very short but ZH is long. The Chinese likely borrowed meaning from adjacent segments; merge SRC_RAW or move the translation back to the correct segment.",
                )
            )

        zh_visible_len = visible_len(zh_text)
        if (duration >= 5.0 and source_word_count >= 12 and zh_visible_len <= 8) or (
            duration >= max_duration and source_word_count >= 30 and zh_visible_len <= 16
        ):
            issues.append(
                issue(
                    segment,
                    "blocker",
                    "undertranslated_scope_drift",
                    (
                        "SRC_RAW covers a substantial source span but ZH is extremely short. "
                        "This often means the Chinese line belongs to a neighboring segment or only translated a fragment. "
                        "Realign the ZH/SRC_DISPLAY to this SRC_RAW span, or merge/split adjacent segments at semantic boundaries."
                    ),
                )
            )

        zh_reading_pressure = (
            duration > 0 and len(zh_text) / duration >= max_zh_cps * 0.8
        ) or zh_lines >= 2
        if duration > 8.0 and source_word_count > max_source_words:
            issues.append(
                issue(
                    segment,
                    "blocker",
                    "dense_long_segment",
                    (
                        "Subtitle is too long for a light viewing rhythm: "
                        f"duration {duration:.1f}s, SRC_RAW {source_word_count} words, Chinese wrapped lines {zh_lines}. "
                        "Split at natural semantic boundaries unless the source is genuinely inseparable."
                    ),
                )
            )
        elif duration > max_duration and source_word_count > max_source_words:
            issues.append(
                issue(
                    segment,
                    "warning",
                    "long_duration",
                    f"Subtitle is longer than {max_duration:.1f}s and has {source_word_count} source words; consider splitting at a natural semantic boundary.",
                )
            )
        elif duration > max_duration and zh_reading_pressure:
            issues.append(
                issue(
                    segment,
                    "warning",
                    "long_duration",
                    (
                        f"Subtitle is longer than {max_duration:.1f}s with noticeable Chinese reading pressure "
                        f"({zh_lines} wrapped line(s)); consider splitting at a natural semantic boundary."
                    ),
                )
            )

        if duration <= 0:
            issues.append(issue(segment, "blocker", "bad_timing", "Subtitle end time must be after start time."))
        elif duration < min_duration:
            issues.append(issue(segment, "warning", "short_duration", f"Subtitle is shorter than {min_duration:.1f}s."))

        if i > 0 and float(segment["start"]) < float(segments[i - 1]["end"]) - 0.05:
            issues.append(issue(segment, "blocker", "overlap", "Subtitle overlaps with the previous segment."))

        zh_cps = len(segment["translation"]) / duration if duration > 0 else 0
        en_cps = len(segment["source_display"]) / duration if duration > 0 else 0
        if zh_cps > max_zh_cps:
            issues.append(issue(segment, "warning", "fast_zh_reading", f"Chinese reading speed is high: {zh_cps:.1f} chars/s."))
        if en_cps > max_en_cps:
            issues.append(issue(segment, "info", "fast_en_reading", f"English display speed is high: {en_cps:.1f} chars/s."))

        if check_domain_terms:
            for bad, note in bad_zh_terms:
                if bad in segment["translation"]:
                    issues.append(issue(segment, "warning", "term_quality", f"Potential bad Chinese term `{bad}`: {note}."))

        leftover_tokens = suspicious_latin_tokens_in_zh(segment.get("translation", ""))
        if leftover_tokens:
            issues.append(
                issue(
                    segment,
                    "warning",
                    "untranslated_source_token",
                    (
                        "Chinese subtitle contains source-language token(s) that may be untranslated or accidentally left in place: "
                        + ", ".join(sorted(set(leftover_tokens))[:6])
                        + ". Keep valid tickers/indicators/brand names, but translate ordinary words."
                    ),
                )
            )

        for pattern, preferred in split_display_patterns:
            if pattern.search(segment["source_display"]):
                issues.append(issue(segment, "warning", "asr_split_display", f"Display text may need split-word repair to `{preferred}`."))

    return issues


def format_time(value: float | None) -> str:
    if value is None:
        return "--"
    return f"{value:.2f}s"


def report_text(payload: dict, issues: list[dict], domain_name: str) -> str:
    segments = payload["segments"]
    counts: dict[str, int] = {}
    for item in issues:
        counts[item["severity"]] = counts.get(item["severity"], 0) + 1

    lines = [
        "# Final QA Report",
        "",
        "## Summary",
        "",
        f"- Segments: {len(segments)}",
        f"- Match failures: {len(payload.get('failures', []))}",
        f"- Blockers: {counts.get('blocker', 0)}",
        f"- Warnings: {counts.get('warning', 0)}",
        f"- Info: {counts.get('info', 0)}",
        "",
        "## Principles",
        "",
        "- `SRC_RAW` is the timestamp matching contract. Do not rewrite it unless fixing a match failure against the original word stream.",
        "- `SRC_DISPLAY` should be readable source-language text and must repair obvious ASR split words, proper names, platform names, and domain terms.",
        f"- `ZH` should be concise, natural Chinese for the target viewers of this domain: {domain_name}.",
        "- Word coverage gaps, timing issues, match failures, overlap, Chinese visual overflow, display/translation scope drift, undertranslated scope drift, flash subtitles, continuation fragments, and dense long subtitles (over 8s plus over 24 source-language words; Chinese overflow is handled by the 3-line visual guardrail) must be fixed before final delivery.",
        "- Long source-language lines are review hints only. Do not split a cue just because the source-language line is long.",
        "- Split only when the Chinese subtitle exceeds the visual guardrail; re-understand the full segment and cut at weak semantic boundaries.",
        "- Micro source segments should be merged when they are only oral filler; isolated m/um/uh-style fillers and sub-0.8s weak subtitles must not export as standalone subtitles.",
        "",
        "## Issues",
        "",
    ]

    if not issues:
        lines.append("No automatic QA issues found.")
        lines.append("")
        return "\n".join(lines)

    for item in issues:
        lines.extend(
            [
                f"### SEG {int(item['index']):04d} [{item['severity']}] {item['kind']}",
                "",
                f"- Time: {format_time(item.get('start'))} -> {format_time(item.get('end'))}",
                f"- Message: {item['message']}",
                f"- ZH: {item.get('zh', '')}",
                f"- SRC_DISPLAY: {item.get('src_display', '')}",
                f"- SRC_RAW: {item.get('src_raw', '')}",
                "",
            ]
        )
    return "\n".join(lines)


def glossary_text(path: Path | None) -> str:
    if not path:
        return "未提供固定术语表。请按上下文使用中文观众最自然、最熟悉的表达。"
    if not path.exists():
        return f"术语表文件未找到：{path}。请按上下文使用中文观众最自然、最熟悉的表达。"
    return path.read_text(encoding="utf-8").strip()


def prompt_text(segments_txt: str, report: str, domain_name: str, glossary: str) -> str:
    return f"""你是视频字幕的最终质检编辑，负责把中文字幕修到准确、自然、符合对应圈层常用说法。

当前领域：
{domain_name}

领域术语和风格参考：
{glossary}

任务：
1. 阅读 Final QA Report 和完整 segments.txt。
2. 修复报告指出的问题。
3. 再主动检查一遍：词流是否完整覆盖；逐条比较 `ZH_i` 与本段及前后至少 2 段源文，确认它最匹配自己的 SEG，且没有连续同方向错位；检查 SRC_DISPLAY/ZH 是否只表达当前 SRC_RAW 覆盖的内容、是否存在孤立语气词字幕、时间轴观看体验、字幕是否占屏过多、ASR 拆词、专名、领域术语、中文自然度、是否漏掉关键逻辑。
4. 只在必要时调整 `SRC_DISPLAY` 和 `ZH`。
5. `SRC_RAW` 是时间轴匹配契约，默认不得改。只有当报告指出匹配失败、词流覆盖缺口、字幕过长、字幕过短、视觉溢出、重叠或明显影响观看体验时，才可以拆分/合并分段；拆分/合并后的 `SRC_RAW` 仍必须逐词照抄原始词流，不能加词、漏词、改词。
6. 中文要像当前领域里中文观众熟悉的视频字幕，不要教材腔，不要逐词硬翻。
7. 如果拆分或合并分段，必须重新连续编号 `[SEG 0001]`、`[SEG 0002]`。
8. 必须输出完整修订后的 segments.txt，保持 `[SEG 0001]` 格式，不要解释。

核心原则：
- SRC_RAW 保真用于匹配。
- SRC_DISPLAY 修正用于阅读，但不能借用相邻 SEG 的内容。
- ZH 面向当前领域的中文观众，准确、自然、短句优先。
- 任何修改后的结果都必须能重新通过 validate_segments.py 和 align_segments.py。

Final QA Report:
{report}

完整 segments.txt:
{segments_txt}
"""


def main() -> int:
    args = parse_args()
    payload = read_json(args.aligned_segments)
    segments_text = args.segments_txt.read_text(encoding="utf-8")
    parse_segments(segments_text)

    bad_zh_terms, split_display_patterns = load_qa_rules(args.qa_rules)
    layout_record = args.subtitle_layout_record.resolve() if args.subtitle_layout_record else None
    if layout_record:
        if layout_record.name != "subtitle_layout.json":
            raise RuntimeError("--subtitle-layout-record must point to subtitle_layout.json.")
        profile = load_bound_subtitle_layout(layout_record.parent)
        args.zh_chars_per_line = profile.zh_wrap_width
        args.source_chars_per_line = profile.source_wrap_width or 0
        args.max_visual_lines = profile.max_zh_lines
        args.max_source_visual_lines = profile.max_source_lines

    issues = collect_issues(
        payload,
        args.min_duration,
        args.max_duration,
        args.max_source_words,
        args.min_source_words,
        args.max_zh_chars,
        args.max_source_chars,
        args.zh_chars_per_line,
        args.source_chars_per_line,
        args.max_visual_lines,
        args.max_source_visual_lines,
        args.max_zh_cps,
        args.max_en_cps,
        not args.disable_domain_term_checks,
        bad_zh_terms,
        split_display_patterns,
    )
    report = report_text(payload, issues, args.domain_name)
    prompt = prompt_text(segments_text, report, args.domain_name, glossary_text(args.glossary))

    report_path = args.out_report or args.aligned_segments.parent / "final_qa_report.md"
    prompt_path = args.out_prompt or args.aligned_segments.parent / "final_qa_prompt.txt"
    report_path.write_text(report, encoding="utf-8")
    prompt_path.write_text(prompt, encoding="utf-8")

    blockers = sum(1 for item in issues if item["severity"] == "blocker")
    warnings = sum(1 for item in issues if item["severity"] == "warning")
    info = sum(1 for item in issues if item["severity"] == "info")
    evidence = {
        "schema_version": 1,
        "stage": "deterministic-final-qa",
        "status": "passed" if blockers == 0 else "changes_required",
        "aligned_segments": str(args.aligned_segments.resolve()),
        "aligned_segments_sha256": sha256_file(args.aligned_segments.resolve()),
        "segments": str(args.segments_txt.resolve()),
        "segments_sha256": sha256_file(args.segments_txt.resolve()),
        "report": str(report_path.resolve()),
        "report_sha256": sha256_file(report_path.resolve()),
        "blockers": blockers,
        "warnings": warnings,
        "info": info,
        "subtitle_layout": str(layout_record) if layout_record else None,
        "subtitle_layout_sha256": sha256_file(layout_record) if layout_record else None,
    }
    evidence_path = report_path.resolve().parent / "final_qa.validated.json"
    evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {report_path}")
    print(f"Wrote {prompt_path}")
    print(f"Wrote {evidence_path}")
    print(f"Final QA issues: blockers={blockers}, warnings={warnings}, total={len(issues)}")
    return 1 if blockers else 0


if __name__ == "__main__":
    raise SystemExit(main())
