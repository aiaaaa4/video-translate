#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from common import (
    ass_escape,
    ass_time,
    display_chinese_subtitle_text,
    read_json,
    split_chinese_line,
    split_source_line,
    srt_time,
)
from delivery_gate import assert_export_ready
from subtitle_layout import SubtitleProfile, load_bound_subtitle_layout
from video_to_subtitles import clean_video_title, contains_chinese, is_generic_video_title, validate_subtitle_tag


def ass_header(profile: SubtitleProfile) -> str:
    return f"""[Script Info]
ScriptType: v4.00+
WrapStyle: 0
ScaledBorderAndShadow: yes
PlayResX: {profile.play_res_x}
PlayResY: {profile.play_res_y}

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,{profile.zh_font_size},&H00FFFFFF,&H000000FF,&H00111111,&HAA000000,0,0,0,0,100,100,0,0,1,{profile.outline},{profile.shadow},{profile.alignment},{profile.margin_l},{profile.margin_r},{profile.margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export aligned subtitles.")
    parser.add_argument("aligned_segments", type=Path)
    parser.add_argument(
        "--work-dir",
        type=Path,
        required=True,
        help="Current run work directory containing all validated delivery-gate evidence.",
    )
    parser.add_argument("--out-dir", type=Path, default=Path("runs/subtitles"))
    parser.add_argument("--basename", default="subtitles")
    parser.add_argument("--source-first", action="store_true", help="Put source line above Chinese line.")
    return parser.parse_args()



def wrapped_lines(segment: dict, profile: SubtitleProfile) -> tuple[list[str], list[str]]:
    translation_lines = split_chinese_line(
        display_chinese_subtitle_text(segment["translation"]),
        profile.zh_wrap_width,
        profile.zh_wrap_adjustment,
        prefer_semantic_breaks=profile.name == "portrait",
    )
    source_lines = split_source_line(segment["source_display"], profile.source_wrap_width)
    return translation_lines, source_lines


def subtitle_lines(segment: dict, source_first: bool, profile: SubtitleProfile) -> list[str]:
    translation_lines, source_lines = wrapped_lines(segment, profile)
    return [*source_lines, *translation_lines] if source_first else [*translation_lines, *source_lines]


def assert_output_naming(work_dir: Path, basename: str) -> None:
    naming_path = work_dir / "output_naming.json"
    if not naming_path.is_file():
        raise RuntimeError(
            "Export blocked: missing output_naming.json. Resume through video_to_subtitles.py, "
            "or use finalize_run.py with --media and --localized-title for an existing run."
        )
    naming = read_json(naming_path)
    localized_title = str(naming.get("localized_title") or "")
    output_tag = validate_subtitle_tag(str(naming.get("output_tag") or ""))
    expected_basename = str(naming.get("output_base") or "")
    if (
        not localized_title
        or clean_video_title(localized_title) != localized_title
        or not contains_chinese(localized_title)
        or is_generic_video_title(localized_title)
    ):
        raise RuntimeError("Export blocked: output_naming.json does not contain a valid clean Chinese title.")
    if expected_basename != f"{localized_title}.{output_tag}":
        raise RuntimeError("Export blocked: output_naming.json contains an inconsistent output basename.")
    if basename != expected_basename:
        raise RuntimeError(
            f"Export blocked: --basename must match the run-bound output name `{expected_basename}`."
        )


def srt_text(segments: list[dict], source_first: bool, profile: SubtitleProfile) -> str:
    blocks = []
    for i, segment in enumerate(segments, start=1):
        lines = subtitle_lines(segment, source_first, profile)
        blocks.append(
            f"{i}\n{srt_time(segment['start'])} --> {srt_time(segment['end'])}\n" + "\n".join(lines) + "\n"
        )
    return "\n".join(blocks)


def srt_bytes(segments: list[dict], source_first: bool, profile: SubtitleProfile) -> bytes:
    """Encode standard multiline SubRip cues with CRLF line endings."""
    text = srt_text(segments, source_first, profile)
    return text.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8")


def ass_text(segments: list[dict], source_first: bool, profile: SubtitleProfile) -> str:
    events = []
    for segment in segments:
        translation_lines, source_lines = wrapped_lines(segment, profile)
        source = r"\N".join(ass_escape(line) for line in source_lines)
        translation = r"\N".join(ass_escape(line) for line in translation_lines)
        start = ass_time(segment["start"])
        end = ass_time(segment["end"])
        position = ""
        if profile.fixed_top_y is not None:
            position = rf"{{\an8\pos({profile.play_res_x // 2},{profile.fixed_top_y})}}"
        if source_first:
            text = position + rf"{{\fs{profile.source_font_size}}}" + source + rf"\N{{\fs{profile.zh_font_size}}}" + translation
        else:
            text = position + rf"{{\fs{profile.zh_font_size}}}" + translation + rf"\N{{\fs{profile.source_font_size}}}" + source
        events.append(
            "Dialogue: 0,"
            f"{start},{end},"
            f"Default,,0,0,0,,{text}"
        )
    return ass_header(profile) + "\n".join(events) + "\n"


def main() -> int:
    args = parse_args()
    assert_export_ready(args.work_dir, args.aligned_segments)
    assert_output_naming(args.work_dir, args.basename)
    profile = load_bound_subtitle_layout(args.work_dir)
    payload = read_json(args.aligned_segments)
    segments = payload["segments"] if isinstance(payload, dict) else payload

    args.out_dir.mkdir(parents=True, exist_ok=True)
    srt_path = args.out_dir / f"{args.basename}.srt"
    ass_path = args.out_dir / f"{args.basename}.ass"
    srt_path.write_bytes(srt_bytes(segments, args.source_first, profile))
    ass_path.write_text(ass_text(segments, args.source_first, profile), encoding="utf-8")
    print(f"Wrote {srt_path}")
    print(f"Wrote {ass_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as error:
        print(f"export-subtitles: {error}", file=sys.stderr)
        raise SystemExit(2)
