#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

from common import (
    prepare_env_file,
    read_json,
    url_significant_characters,
    write_json,
)
from media_probe import AUDIO_SUFFIXES, VIDEO_SUFFIXES, is_supported_audio, is_supported_video
from subtitle_layout import LAYOUT_NAMES, bind_subtitle_layout, resolve_layout


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GLOSSARY = PROJECT_ROOT / "references" / "trading_glossary.md"
DEFAULT_TERM_RULES = PROJECT_ROOT / "references" / "term_repair_rules.json"
NORMAL_WORD_LIMIT = 20_000
LONG_WORD_LIMIT = 50_000
GENERIC_VIDEO_TITLES = {
    "video",
    "originalvideo",
    "sourcevideo",
    "rawvideo",
    "原版视频",
    "原视频",
    "源视频",
}
GENERIC_PARENT_TITLES = GENERIC_VIDEO_TITLES | {
    "desktop",
    "downloads",
    "movies",
    "videos",
    "桌面",
    "下载",
    "影片",
    "视频",
}
PROTECTED_PROJECT_DIRECTORIES = GENERIC_PARENT_TITLES | {
    "home",
    "documents",
    "文档",
    "tmp",
    "temp",
    "temporaryitems",
    "var",
    "users",
    "usr",
    "applications",
    "library",
    "system",
    "volumes",
    "private",
    "opt",
    "etc",
    "bin",
    "sbin",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the fixed video subtitle workflow: local normalize and chunk -> synchronous Alibaba ASR -> selected translation provider -> subtitles."
    )
    parser.add_argument(
        "media",
        type=Path,
        help="Input local video file or a dedicated folder containing exactly one video. Direct audio input is not supported.",
    )
    parser.add_argument(
        "--inspect-inputs",
        action="store_true",
        help="Resolve the selected video and reusable project audio/subtitle locally, print JSON, and exit without external processing.",
    )
    parser.add_argument(
        "--language",
        default="en",
        help="Source language hint for ASR, for example en, fr, es, it. Translation target is always Chinese.",
    )
    parser.add_argument("--run-id", default=None, help="Run directory name. Defaults to a slug from the media name.")
    parser.add_argument(
        "--runs-dir",
        type=Path,
        default=None,
        help="Working directory. Defaults to .work/<run-id> inside the subtitle output directory.",
    )
    parser.add_argument("--outputs-dir", type=Path, default=None)
    parser.add_argument(
        "--overwrite-existing",
        action="store_true",
        help="Replace existing ASS/SRT outputs only. Existing video files or project directories are never overwritten.",
    )
    parser.add_argument(
        "--localized-title",
        default=None,
        help=(
            "Clean Chinese video title used as the visible ASS/SRT basename. "
            "Do not include a leading date, trailing platform ID, extension, or subtitle tag. "
            "Required when no usable Chinese title can be inferred from the media/project folder."
        ),
    )
    parser.add_argument(
        "--source-subtitle",
        type=Path,
        default=None,
        help="Optional original-language SRT/VTT used to correct ASR text while preserving the ASR word timestamps.",
    )
    parser.add_argument(
        "--keep-workflow-inputs",
        action="store_true",
        help="Keep downloader-created files under .work/input after successful export for debugging.",
    )
    parser.add_argument(
        "--subtitle-tag",
        dest="subtitle_tag",
        default=None,
        help="Suffix appended to the localized clean video title. Defaults to 中<source-language>双语字幕.",
    )
    parser.add_argument("--bilingual-tag", dest="subtitle_tag", help=argparse.SUPPRESS)
    parser.add_argument(
        "--domain-name",
        default="finance/trading training videos",
        help="Domain/style label injected into AI prompts.",
    )
    parser.add_argument(
        "--glossary",
        type=Path,
        default=DEFAULT_GLOSSARY,
        help="Domain glossary/style guide injected into AI prompts.",
    )
    parser.add_argument(
        "--term-rules",
        type=Path,
        default=DEFAULT_TERM_RULES,
        help="Automatic repair rules for SRC_DISPLAY and ZH.",
    )
    parser.add_argument(
        "--disable-domain-term-checks",
        action="store_true",
        help="Disable built-in finance/trading bad-term QA warnings for non-trading domains.",
    )
    parser.add_argument("--source-first", action="store_true", help="Put source line above Chinese line.")
    parser.add_argument(
        "--subtitle-layout",
        choices=LAYOUT_NAMES,
        default="auto",
        help="ASS/SRT layout profile. Auto detects displayed video geometry; override only when metadata is wrong.",
    )
    parser.add_argument("--orchestrator-model", default=None, help="Name of the AI model orchestrating this run, for the final chat summary.")
    parser.add_argument(
        "--translation-provider",
        choices=("qwen-mt-plus", "agent"),
        default="qwen-mt-plus",
        help="Initial translation path. Public default: qwen-mt-plus. Use agent for the current Codex/orchestrator model.",
    )
    parser.add_argument("--translation-model", default=None, help="Optional model label used only in the final run summary.")
    parser.add_argument(
        "--confirm-external-processing",
        action="store_true",
        help="Required acknowledgement before audio prepared from the selected video is uploaded to Model Studio temporary storage and sent to the Alibaba ASR service.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help=(
            "Explicit local .env path, forwarded to every credential-using step. Omit to "
            "discover one: $ALIYUN_ENV_FILE, then the nearest .env at or above the Skill "
            "directory and the current directory that declares a provider credential key."
        ),
    )
    parser.add_argument("--skip-env-check", action="store_true")
    return parser.parse_args()


def slugify(value: str) -> str:
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = value.strip("-")
    return value[:80] or "video"


def truncate_filename_bytes(value: str, max_bytes: int = 180) -> str:
    result: list[str] = []
    size = 0
    for character in value:
        character_size = len(character.encode("utf-8"))
        if size + character_size > max_bytes:
            break
        result.append(character)
        size += character_size
    return "".join(result).rstrip()


def clean_video_title(value: str) -> str:
    title = value.strip()
    for suffix in VIDEO_SUFFIXES:
        if title.lower().endswith(suffix):
            title = title[: -len(suffix)].rstrip()
            break
    title = re.sub(r"[\x00-\x1f\x7f]", " ", title)
    title = re.sub(r"[\\/]", " ", title)
    title = title.replace(":", "：")
    title = re.sub(r'[*?"<>|]', " ", title)
    title = re.sub(
        r"^\s*(?:\d{4}[-./_]\d{1,2}[-./_]\d{1,2}|\d{4}年\d{1,2}月\d{1,2}日|\d{8})"
        r"\s*(?:[-–—_]\s*)?",
        "",
        title,
    )
    title = re.sub(r"\s*[\[【]\s*[-_A-Za-z0-9]{6,}\s*[\]】]\s*$", "", title)
    title = re.sub(r"(?:[\s._\-–—]*(?:发布版|字幕版))+$", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s*[\[【]\s*[-_A-Za-z0-9]{6,}\s*[\]】]\s*$", "", title)
    title = re.sub(r"\s+", " ", title).strip(" ._-–—")
    title = re.sub(r"\s+([，。！？：；、）】])", r"\1", title)
    title = re.sub(r"([（【])\s+", r"\1", title)
    return truncate_filename_bytes(title)


def supported_videos(directory: Path) -> list[Path]:
    return sorted(
        (
            child.resolve()
            for child in directory.iterdir()
            if child.is_file() and not child.is_symlink() and is_supported_video(child)
        ),
        key=lambda path: path.name.casefold(),
    )


def resolve_media_input(value: Path) -> tuple[Path, Path, bool]:
    selected = value.expanduser().resolve()
    if not selected.exists():
        raise RuntimeError(f"Input media not found: {selected}")
    if selected.is_dir():
        candidates = supported_videos(selected)
        if not candidates:
            raise RuntimeError(f"No supported video file was found directly inside: {selected}")
        if len(candidates) > 1:
            listing = "\n".join(f"- {candidate}" for candidate in candidates)
            raise RuntimeError(
                "Multiple video files were found in the selected folder. "
                "Choose the intended video explicitly before translation:\n" + listing
            )
        return candidates[0], selected, True
    if not selected.is_file() or not is_supported_video(selected):
        raise RuntimeError(
            "video-translate accepts a local video file or a dedicated single-video folder; "
            f"direct audio input is not supported. Received: {selected}"
        )
    return selected, selected.parent, False


def normalized_title_key(value: str) -> str:
    return re.sub(r"[\s._\-–—]+", "", value).casefold()


def is_generic_video_title(value: str) -> bool:
    key = normalized_title_key(value)
    if key in GENERIC_VIDEO_TITLES:
        return True
    return any(
        key == f"{generic}{suffix}"
        for generic in GENERIC_VIDEO_TITLES
        for suffix in ("发布版", "字幕版", "final", "copy", "translated")
    )


def contains_chinese(value: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", value))


def source_title_candidate(media: Path) -> tuple[str, str]:
    media_title = clean_video_title(media.stem)
    parent_title = clean_video_title(media.parent.name)
    if media_title and contains_chinese(media_title) and not is_generic_video_title(media_title):
        return media_title, "media_filename"
    if (
        parent_title
        and contains_chinese(parent_title)
        and normalized_title_key(parent_title) not in GENERIC_PARENT_TITLES
        and not is_generic_video_title(parent_title)
    ):
        return parent_title, "project_directory"
    if media_title and not is_generic_video_title(media_title):
        return media_title, "media_filename"
    if parent_title and normalized_title_key(parent_title) not in GENERIC_PARENT_TITLES:
        return parent_title, "project_directory"
    raise RuntimeError(
        "No meaningful source title could be inferred from the selected video or its project directory. "
        "Provide --localized-title with a clean Chinese title."
    )


def resolve_localized_output_title(media: Path, explicit_title: str | None) -> tuple[str, str]:
    if explicit_title is not None:
        localized = clean_video_title(explicit_title)
        title_source = "explicit_localized_title"
    else:
        candidate, candidate_source = source_title_candidate(media)
        localized = candidate
        title_source = candidate_source
    if not localized or is_generic_video_title(localized):
        raise RuntimeError(
            "The output title cannot be empty or a generic label such as 原版视频. "
            "Provide --localized-title with the real clean Chinese title."
        )
    if not contains_chinese(localized):
        candidate = localized
        raise RuntimeError(
            "The source title still needs Chinese localization before processing: "
            f"`{candidate}`. Translate it using the video's domain glossary, then pass "
            "--localized-title with only the Chinese title; omit dates, platform IDs, extensions, "
            "and subtitle tags."
        )
    return localized, title_source


def bind_output_naming(
    work_dir: Path,
    media: Path,
    localized_title: str,
    title_source: str,
    output_tag: str,
    output_base: str,
) -> None:
    path = work_dir / "output_naming.json"
    expected = {
        "schema_version": 1,
        "media": str(media),
        "localized_title": localized_title,
        "title_source": title_source,
        "output_tag": output_tag,
        "output_base": output_base,
    }
    if path.exists():
        existing = read_json(path)
        if existing != expected:
            raise RuntimeError(
                "This run is already bound to different output naming. "
                "Resume with the same --localized-title and --subtitle-tag, or start a new --run-id."
            )
        return
    write_json(path, expected)


def language_slug(value: str) -> str:
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = value.strip("-")
    return value or "src"


def default_subtitle_tag(language: str) -> str:
    source_labels = {
        "en": "英",
        "eng": "英",
        "english": "英",
        "fr": "法",
        "fra": "法",
        "fre": "法",
        "french": "法",
        "es": "西",
        "spa": "西",
        "spanish": "西",
        "it": "意",
        "ita": "意",
        "italian": "意",
    }
    return f"中{source_labels.get(language.lower().strip(), language_slug(language))}双语字幕"


def validate_subtitle_tag(value: str) -> str:
    if not value or value != value.strip():
        raise RuntimeError("The subtitle tag must be a non-empty filename suffix without surrounding whitespace.")
    if re.search(r"[\x00-\x1f\x7f/\\:*?\"<>|]", value) or ".." in value:
        raise RuntimeError("The subtitle tag contains unsafe filename characters.")
    if len(value.encode("utf-8")) > 48:
        raise RuntimeError("The subtitle tag is too long; keep it within 48 UTF-8 bytes.")
    return value


def resolve_asr_media(media: Path) -> Path:
    """Reuse one same-basename project audio file; never guess among candidates."""
    if is_supported_audio(media):
        return media
    hidden_input = media.parent / ".work" / "input"
    groups = [
        [hidden_input / f"{media.stem}{suffix}" for suffix in AUDIO_SUFFIXES],
        [media.with_suffix(suffix) for suffix in AUDIO_SUFFIXES],
    ]
    for group in groups:
        candidates = [
            candidate
            for candidate in group
            if candidate.is_file() and candidate.stat().st_size > 0 and is_supported_audio(candidate)
        ]
        if len(candidates) > 1:
            listing = "\n".join(f"- {candidate}" for candidate in candidates)
            raise RuntimeError(
                "Multiple matching audio files were found for the selected video. "
                "Keep one unambiguous source before translation:\n" + listing
            )
        if candidates:
            return candidates[0]
    for root in (hidden_input, media.parent):
        if not root.is_dir():
            continue
        candidates = sorted(
            child
            for child in root.iterdir()
            if child.is_file() and child.stat().st_size > 0 and is_supported_audio(child)
        )
        if len(candidates) > 1:
            listing = "\n".join(f"- {candidate}" for candidate in candidates)
            raise RuntimeError(
                "Multiple project audio files were found and none is an unambiguous same-basename match. "
                "Keep one source or rename the intended audio to match the video:\n" + listing
            )
        if candidates:
            return candidates[0]
    return media


def inspect_media_input(
    value: Path,
    explicit_subtitle: Path | None = None,
    requested_layout: str = "auto",
) -> dict[str, object]:
    media, project_dir, input_was_directory = resolve_media_input(value)
    asr_media = resolve_asr_media(media)
    source_subtitle = resolve_source_subtitle(media, explicit_subtitle)
    profile, geometry = resolve_layout(media, requested_layout)
    name_warnings = []
    for label, candidate in (("video", media), ("audio", asr_media if asr_media != media else None)):
        if candidate is None:
            continue
        risky = url_significant_characters(candidate.name)
        if risky:
            name_warnings.append(
                f"{label} filename contains {', '.join(risky)}; transcription uploads under a neutral object "
                f"name, but rename it if another stage builds URLs or shell arguments from it."
            )

    result = {
        "video": str(media),
        "project_directory": str(project_dir),
        "input_was_directory": input_was_directory,
        "audio": str(asr_media) if asr_media != media else None,
        "audio_action": "reuse_project_audio" if asr_media != media else "extract_from_video",
        "source_subtitle": str(source_subtitle) if source_subtitle else None,
        "video_geometry": {
            "encoded_width": geometry.width,
            "encoded_height": geometry.height,
            "rotation": geometry.rotation,
            "display_width": geometry.display_width,
            "display_height": geometry.display_height,
        },
        "subtitle_layout_requested": requested_layout,
        "subtitle_layout_resolved": profile.name,
    }
    if name_warnings:
        result["name_warnings"] = name_warnings
    return result


def resolve_source_subtitle(media: Path, explicit: Path | None) -> Path | None:
    if explicit:
        resolved = explicit.expanduser().resolve()
        if not resolved.is_file():
            raise RuntimeError(f"Source subtitle reference was not found: {resolved}")
        if resolved.suffix.lower() not in {".srt", ".vtt"}:
            raise RuntimeError("Source subtitle reference must be SRT or VTT.")
        return resolved
    hidden_input = media.parent / ".work" / "input"
    search_roots = [hidden_input, media.parent]
    for root in search_roots:
        if not root.is_dir():
            continue
        preferred = [
            root / f"{media.stem}.原语言字幕.srt",
            root / f"{media.stem}.原语言字幕.vtt",
            root / f"{media.stem}.srt",
            root / f"{media.stem}.vtt",
        ]
        preferred_matches = [
            candidate.resolve()
            for candidate in preferred
            if candidate.is_file() and candidate.stat().st_size > 0
        ]
        if len(preferred_matches) > 1:
            listing = "\n".join(f"- {candidate}" for candidate in preferred_matches)
            raise RuntimeError(
                "Multiple preferred source subtitle references were found for the selected video. "
                "Choose one with --source-subtitle before translation:\n" + listing
            )
        if preferred_matches:
            return preferred_matches[0]
        candidates = sorted(
            path.resolve()
            for suffix in (".srt", ".vtt")
            for path in root.glob(f"{media.stem}*{suffix}")
            if path.is_file() and "双语字幕" not in path.name
        )
        if len(candidates) > 1:
            listing = "\n".join(f"- {candidate}" for candidate in candidates)
            raise RuntimeError(
                "Multiple source subtitle references were found for the selected video. "
                "Choose one with --source-subtitle before translation:\n" + listing
            )
        if candidates:
            return candidates[0]
        all_candidates = sorted(
            path.resolve()
            for path in root.iterdir()
            if path.is_file()
            and path.suffix.lower() in {".srt", ".vtt"}
            and "双语字幕" not in path.name
        )
        if len(all_candidates) > 1:
            listing = "\n".join(f"- {candidate}" for candidate in all_candidates)
            raise RuntimeError(
                "Multiple project subtitle files were found and none is an unambiguous same-basename match. "
                "Choose one with --source-subtitle:\n" + listing
            )
        if all_candidates:
            return all_candidates[0]
    return None


def cleanup_workflow_inputs(media: Path, paths: list[Path | None]) -> list[Path]:
    input_root = (media.parent / ".work" / "input").resolve()
    removed: list[Path] = []
    for path in {item.resolve() for item in paths if item and item.exists()}:
        if path == media or not path.is_relative_to(input_root):
            continue
        path.unlink()
        removed.append(path)
    if input_root.is_dir() and not any(input_root.iterdir()):
        input_root.rmdir()
    return sorted(removed)


def default_outputs_dir(media: Path | None = None) -> Path:
    if media is not None:
        return media.parent
    if PROJECT_ROOT.name == "work":
        return PROJECT_ROOT.parent / "outputs"
    if PROJECT_ROOT.parent.name == "work":
        return PROJECT_ROOT.parents[1] / "outputs"
    return PROJECT_ROOT / "outputs"


def relocated_path(path: Path, old_root: Path, new_root: Path) -> Path:
    absolute = path if path.is_absolute() else (Path.cwd() / path).absolute()
    if absolute == old_root:
        return new_root
    if absolute.is_relative_to(old_root):
        return new_root / absolute.relative_to(old_root)
    return absolute


def validate_delivery_plan(
    media: Path,
    project_dir: Path,
    localized_title: str,
    outputs_dir: Path,
    output_base: str,
    overwrite_existing: bool,
) -> None:
    videos = supported_videos(project_dir)
    if videos != [media.resolve()]:
        raise RuntimeError(
            "The containing folder is not a dedicated single-video project, so it cannot be safely renamed. "
            "Place the selected video and its matching audio/subtitle files in a dedicated folder first."
        )
    if project_dir == project_dir.parent or project_dir == Path.home().resolve():
        raise RuntimeError("Refusing to rename a filesystem root or the current user's home directory.")
    if project_dir == PROJECT_ROOT.resolve() or PROJECT_ROOT.resolve().is_relative_to(project_dir):
        raise RuntimeError("Refusing to rename the installed Skill or one of its parent directories.")
    if normalized_title_key(project_dir.name) in PROTECTED_PROJECT_DIRECTORIES:
        raise RuntimeError(
            f"Refusing to rename the shared `{project_dir.name}` directory. "
            "Place the video in a dedicated project folder first."
        )

    target_video = project_dir / f"{localized_title}{media.suffix}"
    target_project = project_dir.parent / localized_title
    if target_video != media and target_video.exists():
        raise RuntimeError(f"The localized video target already exists; no file was overwritten: {target_video}")
    if target_project != project_dir and target_project.exists():
        raise RuntimeError(f"The localized project-folder target already exists; no folder was overwritten: {target_project}")
    if not overwrite_existing:
        existing_outputs = [
            outputs_dir / f"{output_base}.{extension}"
            for extension in ("ass", "srt")
            if (outputs_dir / f"{output_base}.{extension}").exists()
        ]
        if existing_outputs:
            listing = "\n".join(f"- {path}" for path in existing_outputs)
            raise RuntimeError("Subtitle output already exists and overwrite was not confirmed:\n" + listing)


def finalize_visible_assets(
    media: Path,
    project_dir: Path,
    localized_title: str,
    outputs_dir: Path,
    output_base: str,
    run_dir: Path,
) -> dict[str, Path]:
    media = media.resolve()
    project_dir = project_dir.resolve()
    outputs_dir = outputs_dir.resolve()
    run_dir = run_dir.resolve()
    target_media = project_dir / f"{localized_title}{media.suffix}"
    target_project = project_dir.parent / localized_title
    renamed_media = False
    current_media = media
    if target_media != media:
        media.rename(target_media)
        current_media = target_media
        renamed_media = True
    try:
        if target_project != project_dir:
            project_dir.rename(target_project)
    except OSError:
        if renamed_media and current_media.exists() and not media.exists():
            current_media.rename(media)
        raise

    final_project = target_project if target_project != project_dir else project_dir
    final_media = relocated_path(current_media, project_dir, final_project)
    final_outputs = relocated_path(outputs_dir, project_dir, final_project)
    final_run = relocated_path(run_dir, project_dir, final_project)
    manifest = {
        "schema_version": 1,
        "localized_title": localized_title,
        "project_directory": str(final_project),
        "media": str(final_media),
        "ass": str(final_outputs / f"{output_base}.ass"),
        "srt": str(final_outputs / f"{output_base}.srt"),
    }
    write_json(final_run / "work" / "delivery_manifest.json", manifest)
    print(f"Localized project folder: {final_project}", flush=True)
    print(f"Localized source video: {final_media}", flush=True)
    return {
        "project_dir": final_project,
        "media": final_media,
        "outputs_dir": final_outputs,
        "run_dir": final_run,
        "work_dir": final_run / "work",
    }


def run_step(args: list[str], *, cwd: Path = PROJECT_ROOT) -> None:
    print("+ " + " ".join(args), flush=True)
    subprocess.run(args, cwd=cwd, check=True)


def run_step_status(args: list[str], *, cwd: Path = PROJECT_ROOT) -> int:
    print("+ " + " ".join(args), flush=True)
    return subprocess.run(args, cwd=cwd, check=False).returncode


def media_duration_seconds(media: Path) -> float | None:
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(media),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return float(proc.stdout.strip())
    except Exception:
        return None


def print_run_expectation(media: Path) -> None:
    duration = media_duration_seconds(media)
    if duration is None:
        print("Duration: unknown; long runs should still send user-facing heartbeat updates every 10 minutes.", flush=True)
        return
    minutes = duration / 60
    low = max(5.0, minutes * 0.25)
    high = max(low + 2.0, minutes * 0.75)
    print(f"Duration: {minutes:.1f} min", flush=True)
    print(
        f"Rough first-run estimate: {low:.0f}-{high:.0f} min before AI repair/QA; "
        "actual time depends on upload speed, Alibaba queueing, AI model speed, and whether screen context is enabled.",
        flush=True,
    )
    print("User-facing heartbeat policy: for runs expected over 10 minutes, report status every 10 minutes.", flush=True)


def model_name_from_env(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    return "current orchestrating AI (not recorded)"


def count_word_table(work_dir: Path) -> int | None:
    path = work_dir / "word_table.json"
    if not path.exists():
        return None
    try:
        payload = read_json(path)
    except Exception:
        return None
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict) and isinstance(payload.get("words"), list):
        return len(payload["words"])
    return None


def content_length_class(word_count: int | None, translation_provider: str = "qwen-mt-plus") -> tuple[str, str]:
    route = "qwen-mt-plus with context-bound cache" if translation_provider == "qwen-mt-plus" else "Agent-native section translation with validated receipts"
    if word_count is None:
        return "unknown", f"Word count unavailable; use {route} and rely on QA/export checks."
    if word_count <= NORMAL_WORD_LIMIT:
        return "normal", f"{word_count} words <= {NORMAL_WORD_LIMIT}; use {route}."
    if word_count <= LONG_WORD_LIMIT:
        return "long", f"{word_count} words is long content; use {route} and resume by section/cache."
    return "extra-long", f"{word_count} words > {LONG_WORD_LIMIT}; warn the user about time/quota and use {route} with resumable progress."


def print_segment_generation_policy(work_dir: Path, translation_provider: str) -> None:
    word_count = count_word_table(work_dir)
    length_class, detail = content_length_class(word_count, translation_provider)
    print(f"ASR word count: {word_count if word_count is not None else 'unknown'}", flush=True)
    print(f"Content length class: {length_class}. {detail}", flush=True)
    print(
        "Translation policy: public default uses Alibaba qwen-mt-plus; agent mode uses the current Codex/orchestrator "
        "after validated whole-source analysis. Both paths still require semantic review, deterministic QA, and final QC.",
        flush=True,
    )
    write_json(
        work_dir / "segment_routing_policy.json",
        {
            "word_count": word_count,
            "length_class": length_class,
            "normal_word_limit": NORMAL_WORD_LIMIT,
            "long_word_limit": LONG_WORD_LIMIT,
            "translation_provider": translation_provider,
            "segment_generation_path": "DashScope helper" if translation_provider == "qwen-mt-plus" else "Agent-native gate",
            "fixed_helper_model": "qwen-mt-plus" if translation_provider == "qwen-mt-plus" else "",
            "helper_fallback_model": "",
            "orchestrator_role": "translation plus review" if translation_provider == "agent" else "whole-source context, semantic review, QA, and export",
        },
    )


def bind_translation_provider(work_dir: Path, provider: str) -> None:
    config_path = work_dir / "workflow_config.json"
    if config_path.exists():
        config = read_json(config_path)
        previous = str(config.get("translation_provider") or "qwen-mt-plus")
        if previous != provider:
            raise RuntimeError(
                f"This run is already bound to translation_provider={previous}. "
                "Use the same --translation-provider when resuming, or start a new --run-id."
            )
        return
    meta_path = work_dir / "segment_generation_meta.json"
    if meta_path.exists():
        meta = read_json(meta_path)
        previous = str(meta.get("translation_provider") or "qwen-mt-plus")
        if previous != provider:
            raise RuntimeError(
                f"Existing translations in this run belong to translation_provider={previous}. "
                "Start a new --run-id before switching providers."
            )
    write_json(config_path, {"schema_version": 1, "translation_provider": provider})



def source_language_name(language: str) -> str:
    value = language.lower().strip()
    mapping = {
        "en": "English",
        "eng": "English",
        "english": "English",
        "fr": "French",
        "fra": "French",
        "fre": "French",
        "french": "French",
        "es": "Spanish",
        "spa": "Spanish",
        "spanish": "Spanish",
        "it": "Italian",
        "ita": "Italian",
        "italian": "Italian",
    }
    return mapping.get(value, language or "English")


def ensure_ai_segments(
    work_dir: Path,
    transcript_dir: Path,
    language: str,
    domain_name: str,
    source_subtitle: Path | None,
    translation_context: Path,
    env_file: Path | None = None,
) -> None:
    segments = work_dir / "segments.txt"
    if segments.exists():
        meta_path = work_dir / "segment_generation_meta.json"
        if not meta_path.exists():
            raise RuntimeError(
                "Existing segments have no generation metadata. Use a new --run-id so translations cannot mix."
            )
        meta = read_json(meta_path)
        if str(meta.get("translation_provider") or "qwen-mt-plus") != "qwen-mt-plus":
            raise RuntimeError("Existing segments belong to Agent-native translation. Use a new --run-id for qwen-mt-plus.")
        context_hash = hashlib.sha256(translation_context.read_bytes()).hexdigest()
        if meta.get("translation_context_sha256") != context_hash:
            raise RuntimeError(
                "Existing segments were generated with a different whole-source translation context. "
                "Use a new --run-id so cached translations cannot mix."
            )
        if source_subtitle:
            previous = str(meta.get("source_subtitle") or "")
            previous_hash = str(meta.get("source_subtitle_sha256") or "")
            current_hash = hashlib.sha256(source_subtitle.read_bytes()).hexdigest()
            if previous != str(source_subtitle) or previous_hash != current_hash:
                raise RuntimeError(
                    "Existing segments were generated with a different source subtitle reference. "
                    "Use a new --run-id so corrected text and cached translations cannot mix."
                )
        print(f"Using existing segments: {segments}", flush=True)
        return

    screen_context = work_dir / "screen_context.txt"
    cmd = [
        sys.executable,
        "scripts/generate_segments_with_dashscope.py",
        str(transcript_dir / "transcript_words.json"),
        "--out",
        str(segments),
        "--cache",
        str(work_dir / "dashscope_translation_cache.json"),
        "--model",
        "auto",
        "--source-language-name",
        source_language_name(language),
        "--domain-name",
        domain_name,
        "--translation-context",
        str(translation_context),
        "--concurrency",
        "1",
        "--max-retries",
        "8",
        "--qwen-mt-min-interval-seconds",
        "1.0",
        "--confirm-external-processing",
    ]
    if env_file is not None:
        cmd.extend(["--env", str(env_file)])
    if screen_context.exists() and screen_context.stat().st_size > 0:
        cmd.extend(["--screen-context", str(screen_context)])
    if source_subtitle:
        cmd.extend(["--source-subtitle", str(source_subtitle)])
    run_step(cmd)


def agent_translation_gate(work_dir: Path) -> bool:
    translation_dir = work_dir / "global_review" / "agent-translation"
    command = [
        sys.executable,
        "scripts/agent_translation.py",
        "prepare",
        "--source-manifest",
        str(work_dir / "global_review" / "source-analysis" / "manifest.json"),
        "--translation-context",
        str(work_dir / "global_review" / "source-analysis" / "translation-context.json"),
        "--out-dir",
        str(translation_dir),
    ]
    run_step(command)
    receipt = translation_dir / "agent-translation-receipt.json"
    if not receipt.exists():
        print("", flush=True)
        print("Agent-native initial translation is required before semantic review.", flush=True)
        print(f"- Follow the exact procedure in: {translation_dir / 'WORKFLOW.md'}", flush=True)
        print(f"- Read every translation section listed in: {translation_dir / 'manifest.json'}", flush=True)
        print(f"- Write translated sections under: {translation_dir / 'translated'}", flush=True)
        print(f"- Complete: {translation_dir / 'agent-translation-receipt.template.json'}", flush=True)
        print(f"- Save as: {receipt}", flush=True)
        print("- Rerun the same command with --translation-provider agent after all sections are translated.", flush=True)
        return False
    status = run_step_status(
        [
            sys.executable,
            "scripts/agent_translation.py",
            "validate",
            "--manifest",
            str(translation_dir / "manifest.json"),
            "--receipt",
            str(receipt),
            "--translated-dir",
            str(translation_dir / "translated"),
            "--out",
            str(work_dir / "segments.txt"),
            "--meta-out",
            str(work_dir / "segment_generation_meta.json"),
        ]
    )
    if status != 0:
        print("Agent-native translation validation failed; semantic review will not start.", flush=True)
        return False
    return True


def ensure_transcript(
    media: Path,
    workflow_video: Path,
    transcript_dir: Path,
    language: str,
    confirm_external_processing: bool,
    env_file: Path | None = None,
) -> None:
    transcript_path = transcript_dir / "transcript_words.json"
    if transcript_path.exists():
        transcript = read_json(transcript_path)
        existing_language = str(transcript.get("language") or "").strip()
        if existing_language and existing_language != language:
            raise RuntimeError(
                f"Existing transcript was created with language={existing_language}, "
                f"but this run requested language={language}. Use a new --run-id or remove the old run directory."
            )
        print(f"Using existing transcript: {transcript_path}", flush=True)
        return

    command = [
        sys.executable,
        "scripts/transcribe_api.py",
        str(media),
        "--provider",
        "aliyun-fun-asr",
        "--out-dir",
        str(transcript_dir),
        "--language",
        language,
        "--confirm-external-processing",
    ]
    if env_file is not None:
        command.extend(["--env-file", str(env_file)])
    if is_supported_audio(media):
        command.extend(["--workflow-video", str(workflow_video)])
    run_step(command)


def ensure_work_files(transcript_dir: Path, work_dir: Path) -> None:
    word_table = work_dir / "word_table.json"
    word_stream = work_dir / "word_stream.txt"
    asr_reference = work_dir / "asr_segments_reference.txt"
    if word_table.exists() and word_stream.exists() and asr_reference.exists():
        print(f"Using existing word stream: {word_stream}", flush=True)
        print(f"Using existing ASR reference: {asr_reference}", flush=True)
        return

    run_step(
        [
            sys.executable,
            "scripts/extract_word_stream.py",
            str(transcript_dir / "transcript_words.json"),
            "--out-dir",
            str(work_dir),
        ]
    )


def file_fingerprint(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    stat = path.stat()
    return {"path": str(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def ensure_prompt(work_dir: Path, domain_name: str, glossary: Path) -> None:
    prompt = work_dir / "prompt.txt"
    meta_path = work_dir / "prompt_meta.json"
    asr_reference = work_dir / "asr_segments_reference.txt"
    screen_context = work_dir / "screen_context.txt"
    desired_meta = {
        "domain_name": domain_name,
        "glossary": str(glossary),
        "asr_reference": str(asr_reference),
        "screen_context": file_fingerprint(screen_context),
    }
    if prompt.exists() and meta_path.exists():
        if read_json(meta_path) == desired_meta:
            print(f"Using existing prompt: {prompt}", flush=True)
            return
        print("Prompt settings changed; regenerating prompt.", flush=True)
    elif prompt.exists():
        print("Prompt metadata missing; regenerating prompt with current workflow rules.", flush=True)

    run_step(
        [
            sys.executable,
            "scripts/generate_prompt.py",
            str(work_dir / "word_stream.txt"),
            "--out",
            str(prompt),
            "--asr-reference",
            str(asr_reference),
            "--domain-name",
            domain_name,
            "--glossary",
            str(glossary),
            "--screen-context",
            str(screen_context),
        ]
    )
    write_json(meta_path, desired_meta)


def record_step_status(work_dir: Path, step: str, status: str, detail: str = "") -> None:
    status_path = work_dir / "workflow_status.json"
    payload = {"updated": dt.datetime.now().isoformat(timespec="seconds"), "steps": {}}
    if status_path.exists():
        try:
            existing = read_json(status_path)
            if isinstance(existing, dict):
                payload.update(existing)
                payload.setdefault("steps", {})
        except Exception:
            pass
    payload["updated"] = dt.datetime.now().isoformat(timespec="seconds")
    payload["steps"][step] = {"status": status, "detail": detail}
    write_json(status_path, payload)


def record_delivery_completion(work_dir: Path, media: Path, project_dir: Path) -> None:
    """Close the run only after every visible delivery artifact is finalized."""
    record_step_status(work_dir, "visible_asset_naming", "done", f"media={media}; project={project_dir}")
    record_step_status(work_dir, "start", "done", "all delivery gates passed and visible assets finalized")


def classify_failure(message: str) -> tuple[str, str, list[str]]:
    lower = message.lower()
    if (
        "localized-title" in lower
        or "output title" in lower
        or "output naming" in lower
        or "subtitle tag" in lower
    ):
        return (
            "VTZ-E006",
            "Output title localization failure",
            [
                "Derive the real video title from the selected filename or media project folder.",
                "Remove any leading date and trailing platform ID, translate the title to Chinese with the domain glossary, and pass only that title through --localized-title.",
            ],
        )
    if "temporary upload" in lower or "upload policy" in lower or "temporary storage" in lower:
        return (
            "VTZ-E003",
            "Model Studio temporary upload failure",
            [
                "Check DASHSCOPE_API_KEY, network access, and whether the temporary upload host is reachable.",
                "Rerun with the same --run-id so completed local stages can be reused.",
            ],
        )
    if "dashscope" in lower or "aliyun" in lower or "workspace" in lower:
        return (
            "VTZ-E004",
            "Alibaba ASR / DashScope failure",
            [
                "Check DASHSCOPE_API_KEY, ALIYUN_WORKSPACE_ID, ALIYUN_REGION, account balance, and Alibaba task status.",
                "If a cached Alibaba task failed, rerun with the same --run-id; the workflow will resubmit when the previous task is terminal failed.",
            ],
        )
    if "ffmpeg" in lower:
        return (
            "VTZ-E002",
            "ffmpeg environment failure",
            [
                "Install ffmpeg and ensure it is available on PATH.",
                "Run python scripts/check_env.py after installation.",
            ],
        )
    if "segments.txt" in lower or "src_raw" in lower:
        return (
            "VTZ-E005",
            "AI segment contract failure",
            [
                "The AI runner should repair segments.txt using prompt.txt, final_qa_prompt.txt, and final_qa_report.md.",
                "Escalate for user review only after two automatic repair attempts still fail.",
            ],
        )
    return (
        "VTZ-E000",
        "Unclassified workflow failure",
        [
            "Review workflow_status.json and the latest script output in the run directory.",
            "Run python scripts/check_env.py --json if environment or secret configuration is uncertain.",
        ],
    )


def print_failure_guidance(error: Exception) -> None:
    message = str(error)
    code, title, checks = classify_failure(message)
    print("\nWorkflow failed.", file=sys.stderr)
    print(f"- Code: {code}", file=sys.stderr)
    print(f"- Category: {title}", file=sys.stderr)
    print(f"- Error: {message}", file=sys.stderr)
    print("- Next step: use the checklist below, then rerun the same command; completed stages are reused when possible.", file=sys.stderr)
    for check in checks:
        print(f"- {check}", file=sys.stderr)



def read_optional_json(path: Path) -> object | None:
    if not path.exists():
        return None
    try:
        return read_json(path)
    except Exception:
        return None


def record_step_timing(work_dir: Path, step: str, elapsed: float, detail: str = "") -> None:
    path = work_dir / "step_timings.json"
    payload = read_optional_json(path)
    if not isinstance(payload, dict):
        payload = {"steps": {}}
    payload.setdefault("steps", {})
    payload["steps"][step] = {
        "seconds": round(elapsed, 3),
        "minutes": round(elapsed / 60, 3),
        "detail": detail,
        "updated": dt.datetime.now().isoformat(timespec="seconds"),
    }
    write_json(path, payload)


def qa_summary_from_report(report_path: Path) -> tuple[list[str], dict[str, str], Counter[str]]:
    qa_summary: list[str] = []
    summary_counts: dict[str, str] = {}
    warning_types: Counter[str] = Counter()
    if not report_path.exists():
        return qa_summary, summary_counts, warning_types

    lines = report_path.read_text(encoding="utf-8").splitlines()
    in_summary = False
    for line in lines:
        if line.strip() == "## Summary":
            in_summary = True
            continue
        if in_summary and line.startswith("## "):
            break
        if in_summary and line.strip().startswith("-"):
            item = line.strip()
            qa_summary.append(item)
            if ":" in item:
                key, value = item.lstrip("- ").split(":", 1)
                summary_counts[key.strip()] = value.strip()

    for line in lines:
        match = re.match(r"^### SEG \d+ \[warning\] ([a-z0-9_\-]+)", line.strip())
        if match:
            warning_types[match.group(1)] += 1
    return qa_summary, summary_counts, warning_types


def write_run_summary(
    work_dir: Path,
    run_dir: Path,
    media: Path,
    language: str,
    domain_name: str,
    outputs_dir: Path,
    output_base: str,
    elapsed: float,
    orchestrator_model: str = "current orchestrating AI (not recorded)",
    translation_model: str | None = None,
) -> None:
    aligned_path = work_dir / "aligned_segments.json"
    report_path = work_dir / "final_qa_report.md"
    transcript_path = run_dir / "transcript" / "transcript_words.json"
    timings_path = work_dir / "step_timings.json"
    helper_meta_path = work_dir / "segment_generation_meta.json"
    source_validation = read_optional_json(
        work_dir / "global_review" / "source-analysis" / "source-analysis.validated.json"
    )
    source_manifest = read_optional_json(
        work_dir / "global_review" / "source-analysis" / "manifest.json"
    )
    semantic_validation = read_optional_json(
        work_dir / "global_review" / "semantic" / "semantic-review.validated.json"
    )
    qc_validation = read_optional_json(work_dir / "global_review" / "final-qc" / "final-qc.validated.json")
    source_analysis_model = str(source_validation.get("model") or "unknown") if isinstance(source_validation, dict) else "unknown"
    semantic_review_model = str(semantic_validation.get("model") or "unknown") if isinstance(semantic_validation, dict) else "unknown"
    final_qc_model = str(qc_validation.get("model") or "unknown") if isinstance(qc_validation, dict) else "unknown"
    if orchestrator_model == "current orchestrating AI (not recorded)" and source_analysis_model != "unknown":
        orchestrator_model = source_analysis_model

    segment_count = "unknown"
    if aligned_path.exists():
        payload = read_optional_json(aligned_path)
        if isinstance(payload, dict):
            segment_count = str(len(payload.get("segments", [])))

    transcript = read_optional_json(transcript_path)
    if not isinstance(transcript, dict):
        transcript = {}
    asr_provider = str(transcript.get("provider") or "aliyun-fun-asr")
    asr_model = str(transcript.get("model") or "unknown")
    media_duration = transcript.get("duration")

    word_count = count_word_table(work_dir)
    workflow_config = read_optional_json(work_dir / "workflow_config.json")
    translation_provider = (
        str(workflow_config.get("translation_provider") or "qwen-mt-plus")
        if isinstance(workflow_config, dict)
        else "qwen-mt-plus"
    )
    length_class, length_detail = content_length_class(word_count, translation_provider)

    helper_meta = read_optional_json(helper_meta_path)
    if isinstance(helper_meta, dict) and str(helper_meta.get("translation_provider") or "qwen-mt-plus") == "qwen-mt-plus":
        translation_path = "helper_dashscope"
        effective_translation_model = str(helper_meta.get("model") or translation_model or "unknown")
        helper_detail = (
            f"concurrency={helper_meta.get('concurrency', 'unknown')}; "
            f"domains_applied={helper_meta.get('domain_prompt_applied', False)}; "
            f"terms={helper_meta.get('term_count', 0)}; tm={helper_meta.get('translation_memory_count', 0)}"
        )
        source_reference = str(helper_meta.get("source_subtitle") or "none")
        source_reference_status = str(helper_meta.get("source_reference_status") or "not_applied")
        reference_corrected_chunks = str(helper_meta.get("reference_corrected_chunks") or 0)
    elif isinstance(helper_meta, dict) and helper_meta.get("translation_provider") == "agent":
        translation_path = "agent_native"
        effective_translation_model = str(helper_meta.get("model") or translation_model or orchestrator_model)
        helper_detail = f"validated_segments={helper_meta.get('segments', 'unknown')}; receipt_bound=true"
        source_reference = "inherited from source-analysis"
        source_reference_status = (
            str(source_manifest.get("source_reference_status") or "not_applied")
            if isinstance(source_manifest, dict)
            else "unknown"
        )
        reference_corrected_chunks = (
            str(source_manifest.get("source_reference_corrected_segments") or 0)
            if isinstance(source_manifest, dict)
            else "unknown"
        )
    else:
        translation_path = "unknown_legacy"
        effective_translation_model = translation_model or orchestrator_model
        helper_detail = "generation metadata unavailable"
        source_reference = "none"
        source_reference_status = "unknown"
        reference_corrected_chunks = "0"

    timings = read_optional_json(timings_path)
    timing_lines: list[str] = []
    if isinstance(timings, dict) and isinstance(timings.get("steps"), dict):
        for step, item in timings["steps"].items():
            if isinstance(item, dict):
                detail = item.get("detail", "")
                suffix = f"; {detail}" if detail else ""
                timing_lines.append(f"- {step}: {float(item.get('seconds', 0.0)):.1f}s ({float(item.get('minutes', 0.0)):.1f} min){suffix}")
    if not timing_lines:
        timing_lines = ["- Step timings unavailable for this run invocation."]

    qa_summary, summary_counts, warning_types = qa_summary_from_report(report_path)
    warning_total = summary_counts.get("Warnings", "unknown")
    blocker_total = summary_counts.get("Blockers", "unknown")
    top_warning_lines = [f"- {name}: {count}" for name, count in warning_types.most_common(8)]
    if not top_warning_lines:
        top_warning_lines = ["- No QA warning categories detected."]

    duration_text = "unknown"
    if isinstance(media_duration, (int, float)):
        duration_text = f"{float(media_duration) / 60:.1f} min"

    lines = [
        "# Run Summary",
        "",
        f"- Generated: {dt.datetime.now().isoformat(timespec='seconds')}",
        f"- Media: {media}",
        f"- Media duration: {duration_text}",
        f"- Source language: {language}",
        "- Target language: Chinese",
        f"- Domain: {domain_name}",
        f"- Run dir: {run_dir}",
        f"- Segments: {segment_count}",
        f"- ASR word count: {word_count if word_count is not None else 'unknown'}",
        f"- Content length class: {length_class} ({length_detail})",
        f"- Total elapsed this invocation: {elapsed:.1f}s ({elapsed / 60:.1f} min)",
        "",
        "## Models",
        "",
        f"- ASR provider: {asr_provider}",
        f"- ASR model: {asr_model}",
        f"- Orchestrator model: {orchestrator_model}",
        f"- Whole-source analysis model: {source_analysis_model}",
        f"- Semantic translation review model: {semantic_review_model}",
        f"- Final whole-document QC model: {final_qc_model}",
        f"- Segment translation path: {translation_path}",
        f"- Segment translation model: {effective_translation_model}",
        f"- Helper detail: {helper_detail}",
        f"- Source subtitle reference: {source_reference}",
        f"- Source subtitle reference status: {source_reference_status}",
        f"- Reference-corrected chunks: {reference_corrected_chunks}",
        "",
        "## Step Timings",
        "",
    ]
    lines.extend(timing_lines)
    lines.extend(
        [
            "",
            "## Outputs",
            "",
            f"- ASS: {outputs_dir / (output_base + '.ass')}",
            f"- SRT: {outputs_dir / (output_base + '.srt')}",
            "",
            "## QA Summary",
            "",
        ]
    )
    lines.extend(qa_summary or ["- QA summary unavailable"])
    lines.extend(
        [
            "",
            "## Warning Breakdown",
            "",
            f"- Blockers: {blocker_total}",
            f"- Warnings: {warning_total}",
        ]
    )
    lines.extend(top_warning_lines)
    summary_text = "\n".join(lines).rstrip() + "\n"
    print("\n" + summary_text, flush=True)


def run_deterministic_qa(
    work_dir: Path,
    domain_name: str,
    glossary: Path,
    term_rules: Path,
    disable_domain_term_checks: bool,
) -> None:
    segments = work_dir / "segments.txt"
    aligned = work_dir / "aligned_segments.json"
    word_table = work_dir / "word_table.json"

    def align() -> None:
        run_step(
            [
                sys.executable,
                "scripts/align_segments.py",
                str(word_table),
                str(segments),
                "--out",
                str(aligned),
            ]
        )

    def final_qa() -> int:
        final_qa_cmd = [
            sys.executable,
            "scripts/final_qa.py",
            str(aligned),
            str(segments),
            "--domain-name",
            domain_name,
            "--glossary",
            str(glossary),
            "--qa-rules",
            str(term_rules),
            "--subtitle-layout-record",
            str(work_dir / "subtitle_layout.json"),
        ]
        if disable_domain_term_checks:
            final_qa_cmd.append("--disable-domain-term-checks")
        return run_step_status(final_qa_cmd)

    def auto_fix() -> bool:
        """Run the deterministic merger; returns True when segments.txt changed."""
        before = segments.read_text(encoding="utf-8")
        run_step([sys.executable, "scripts/auto_fix_segments.py", str(word_table), str(segments)])
        if segments.read_text(encoding="utf-8") == before:
            return False
        run_step([sys.executable, "scripts/validate_segments.py", str(word_table), str(segments)])
        align()
        return True

    run_step([sys.executable, "scripts/repair_segments_terms.py", str(segments), "--rules", str(term_rules)])
    run_step(
        [
            sys.executable,
            "scripts/validate_segments.py",
            str(word_table),
            str(segments),
            "--auto-repair",
        ]
    )
    align()

    # Deterministic auto-fix: merge mechanically-fixable cues (fillers, flash
    # subtitles, continuation fragments, hanging boundaries), then re-run QA.
    auto_fix()
    qa_status = final_qa()
    if qa_status != 0 and auto_fix():
        qa_status = final_qa()

    if qa_status != 0:
        print("", flush=True)
        print("Final QA found blockers that need AI review; subtitles were NOT exported.", flush=True)
        print(f"- Report: {work_dir / 'final_qa_report.md'}", flush=True)
        print(f"- AI repair prompt: {work_dir / 'final_qa_prompt.txt'}", flush=True)
        print(
            "AI runner: use final_qa_prompt.txt to revise the affected files under "
            "global_review/semantic/reviewed/, refresh their receipt hashes, then rerun this command.",
            flush=True,
        )
        print("Ask the user only if the same blocker remains after two AI repair attempts or the fix needs domain judgment.", flush=True)
        raise SystemExit(1)

def export_subtitle_files(
    work_dir: Path,
    subtitles_dir: Path,
    outputs_dir: Path,
    output_base: str,
    source_first: bool,
    overwrite_existing: bool = False,
) -> None:
    aligned = work_dir / "aligned_segments.json"
    export_cmd = [
        sys.executable,
        "scripts/export_subtitles.py",
        str(aligned),
        "--work-dir",
        str(work_dir),
        "--out-dir",
        str(subtitles_dir),
        "--basename",
        output_base,
    ]
    if source_first:
        export_cmd.append("--source-first")
    run_step(export_cmd)

    outputs_dir.mkdir(parents=True, exist_ok=True)
    for ext in ("srt", "ass"):
        source = subtitles_dir / f"{output_base}.{ext}"
        target = outputs_dir / f"{output_base}.{ext}"
        if target.exists() and not overwrite_existing:
            raise RuntimeError(f"Subtitle output already exists and overwrite was not confirmed: {target}")
        shutil.copyfile(source, target)
        print(f"Wrote output: {target}", flush=True)


def semantic_review_gate(work_dir: Path) -> bool:
    segments = work_dir / "segments.txt"
    initial_segments = work_dir / "segments.initial.txt"
    semantic_dir = work_dir / "global_review" / "semantic"
    if not initial_segments.exists():
        shutil.copyfile(segments, initial_segments)
    run_step(
        [
            sys.executable,
            "scripts/global_review.py",
            "prepare-semantic",
            "--segments",
            str(initial_segments),
            "--word-table",
            str(work_dir / "word_table.json"),
            "--source-context",
            str(work_dir / "global_review" / "source-analysis" / "translation-context.json"),
            "--out-dir",
            str(semantic_dir),
        ]
    )
    receipt = semantic_dir / "semantic-review-receipt.json"
    if not receipt.exists():
        print("", flush=True)
        print("Mandatory whole-document semantic review is required before deterministic QA.", flush=True)
        print(f"- Follow the procedure in: {semantic_dir / 'WORKFLOW.md'}", flush=True)
        print(f"- Read every section listed in: {semantic_dir / 'manifest.json'}", flush=True)
        print(f"- Build global context from: {semantic_dir / 'semantic-review-receipt.template.json'}", flush=True)
        print(f"- Write reviewed target sections under: {semantic_dir / 'reviewed'}", flush=True)
        print(f"- Save the completed receipt as: {receipt}", flush=True)
        print("- Rerun with the same --run-id after the orchestrator has reviewed every section.", flush=True)
        return False
    reviewed_segments = semantic_dir / "segments.global-reviewed.txt"
    status = run_step_status(
        [
            sys.executable,
            "scripts/global_review.py",
            "validate-semantic",
            "--manifest",
            str(semantic_dir / "manifest.json"),
            "--receipt",
            str(receipt),
            "--reviewed-dir",
            str(semantic_dir / "reviewed"),
            "--out",
            str(reviewed_segments),
        ]
    )
    if status != 0:
        print("Semantic review validation failed; no subtitles will be exported.", flush=True)
        return False
    shutil.copyfile(reviewed_segments, segments)
    return True


def source_analysis_gate(transcript_dir: Path, work_dir: Path, source_subtitle: Path | None, translation_provider: str = "qwen-mt-plus") -> bool:
    analysis_dir = work_dir / "global_review" / "source-analysis"
    translation_context = analysis_dir / "translation-context.json"
    existing_manifest = analysis_dir / "manifest.json"
    existing_receipt = analysis_dir / "source-analysis-receipt.json"
    if existing_manifest.is_file() and existing_receipt.is_file() and translation_context.is_file():
        try:
            manifest = read_json(existing_manifest)
            bound_reference = str(manifest.get("source_subtitle") or "")
            reference_matches = source_subtitle is None or (
                bound_reference == str(source_subtitle.resolve())
                and manifest.get("source_subtitle_sha256") == hashlib.sha256(source_subtitle.read_bytes()).hexdigest()
            )
            if reference_matches and run_step_status(
                [
                    sys.executable,
                    "scripts/source_analysis.py",
                    "validate",
                    "--manifest",
                    str(existing_manifest),
                    "--receipt",
                    str(existing_receipt),
                    "--out",
                    str(translation_context),
                ]
            ) == 0:
                return True
        except (OSError, ValueError, RuntimeError):
            pass
    command = [
        sys.executable,
        "scripts/source_analysis.py",
        "prepare",
        "--transcript",
        str(transcript_dir / "transcript_words.json"),
        "--out-dir",
        str(analysis_dir),
    ]
    if source_subtitle:
        command.extend(["--source-subtitle", str(source_subtitle)])
    screen_context = work_dir / "screen_context.txt"
    if screen_context.is_file() and screen_context.stat().st_size > 0:
        command.extend(["--screen-context", str(screen_context)])
    run_step(command)
    receipt = analysis_dir / "source-analysis-receipt.json"
    if not receipt.exists():
        print("", flush=True)
        print(f"Mandatory whole-source analysis is required before {translation_provider} translation.", flush=True)
        print(f"- Follow the exact procedure in: {analysis_dir / 'WORKFLOW.md'}", flush=True)
        print(f"- Read every source section listed in: {analysis_dir / 'manifest.json'}", flush=True)
        print(f"- Complete: {analysis_dir / 'source-analysis-receipt.template.json'}", flush=True)
        print(f"- Save as: {receipt}", flush=True)
        print("- Rerun the same command after the orchestrator has analyzed the complete source.", flush=True)
        return False
    status = run_step_status(
        [
            sys.executable,
            "scripts/source_analysis.py",
            "validate",
            "--manifest",
            str(analysis_dir / "manifest.json"),
            "--receipt",
            str(receipt),
            "--out",
            str(translation_context),
        ]
    )
    if status != 0:
        print("Whole-source analysis validation failed; translation will not start.", flush=True)
        return False
    return True


def final_qc_gate(work_dir: Path) -> bool:
    qc_dir = work_dir / "global_review" / "final-qc"
    global_context = work_dir / "global_review" / "semantic" / "global-context.json"
    run_step(
        [
            sys.executable,
            "scripts/global_review.py",
            "prepare-qc",
            "--segments",
            str(work_dir / "segments.txt"),
            "--qa-report",
            str(work_dir / "final_qa_report.md"),
            "--global-context",
            str(global_context),
            "--out-dir",
            str(qc_dir),
        ]
    )
    receipt = qc_dir / "final-qc-receipt.json"
    if not receipt.exists():
        print("", flush=True)
        print("Mandatory whole-document final consistency QC is required before export.", flush=True)
        print(f"- Follow the procedure in: {qc_dir / 'WORKFLOW.md'}", flush=True)
        print(f"- Read the global context: {global_context}", flush=True)
        print(f"- Read the deterministic QA report: {work_dir / 'final_qa_report.md'}", flush=True)
        print(f"- Read every section listed in: {qc_dir / 'manifest.json'}", flush=True)
        print(f"- Complete: {qc_dir / 'final-qc-receipt.template.json'}", flush=True)
        print(f"- Save the completed receipt as: {receipt}", flush=True)
        print("- If changes are needed, revise semantic review outputs and rerun from that gate.", flush=True)
        return False
    status = run_step_status(
        [
            sys.executable,
            "scripts/global_review.py",
            "validate-qc",
            "--manifest",
            str(qc_dir / "manifest.json"),
            "--receipt",
            str(receipt),
        ]
    )
    if status != 0:
        print("Final whole-document QC validation failed; no subtitles will be exported.", flush=True)
        return False
    return True



def main() -> int:
    args = parse_args()
    started_at = time.monotonic()
    # Resolve the credential file once and forward the resolved path to every
    # step that needs it. The steps run with this directory as cwd, so without
    # this they would look for `.env` here while the credentials may live in a
    # workspace root above it.
    env_search = prepare_env_file(args.env_file, start=Path(__file__).resolve().parent)
    env_file = env_search.path
    if env_search.explicit and env_file is None:
        return 2
    try:
        media, project_dir, _input_was_directory = resolve_media_input(args.media)
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 2
    if args.inspect_inputs:
        try:
            print(
                json.dumps(
                    inspect_media_input(args.media, args.source_subtitle, args.subtitle_layout),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        except RuntimeError as error:
            print(str(error), file=sys.stderr)
            return 2
        return 0
    if not args.confirm_external_processing:
        text_destination = (
            "Alibaba qwen-mt-plus" if args.translation_provider == "qwen-mt-plus" else "the current Agent model service"
        )
        print(
            "Refusing external processing without --confirm-external-processing. "
            f"This workflow uploads the selected audio to Model Studio temporary storage and sends it to the Alibaba ASR service, "
            f"and processes transcript text with {text_destination}.",
            file=sys.stderr,
        )
        return 2

    localized_title, title_source = resolve_localized_output_title(media, args.localized_title)
    run_id = args.run_id or slugify(media.stem)
    outputs_dir = args.outputs_dir or default_outputs_dir(media)
    if not outputs_dir.is_absolute():
        outputs_dir = project_dir / outputs_dir
    outputs_dir = outputs_dir.resolve()

    runs_dir = args.runs_dir or (outputs_dir / ".work")
    if not runs_dir.is_absolute():
        runs_dir = PROJECT_ROOT / runs_dir

    run_dir = runs_dir / run_id
    transcript_dir = run_dir / "transcript"
    work_dir = run_dir / "work"
    subtitles_dir = run_dir / "subtitles"
    work_dir.mkdir(parents=True, exist_ok=True)
    output_tag = validate_subtitle_tag(args.subtitle_tag or default_subtitle_tag(args.language))
    output_base = f"{localized_title}.{output_tag}"
    validate_delivery_plan(
        media,
        project_dir,
        localized_title,
        outputs_dir,
        output_base,
        args.overwrite_existing,
    )
    bind_output_naming(
        work_dir,
        media,
        localized_title,
        title_source,
        output_tag,
        output_base,
    )
    subtitle_profile = bind_subtitle_layout(work_dir, media, args.subtitle_layout)
    bind_translation_provider(work_dir, args.translation_provider)
    asr_media = resolve_asr_media(media)
    source_subtitle = resolve_source_subtitle(media, args.source_subtitle)
    record_step_status(
        work_dir,
        "start",
        "running",
        (
            f"media={media}; asr_media={asr_media}; source_subtitle={source_subtitle}; "
            f"language={args.language}; localized_title={localized_title}; output_base={output_base}; "
            f"subtitle_layout={subtitle_profile.name}"
        ),
    )

    print(f"Run: {run_id}", flush=True)
    print(f"Media: {media}", flush=True)
    print(f"Localized output title ({title_source}): {localized_title}", flush=True)
    print(f"Subtitle layout: {subtitle_profile.name} (requested: {args.subtitle_layout})", flush=True)
    if asr_media != media:
        print(f"Reusing downloader-provided audio for ASR: {asr_media}", flush=True)
    if source_subtitle:
        print(f"Using source subtitle as ASR correction reference: {source_subtitle}", flush=True)
    print(f"Run dir: {run_dir}", flush=True)
    print(f"Outputs: {outputs_dir}", flush=True)
    print_run_expectation(media)

    if not args.skip_env_check:
        record_step_status(work_dir, "environment", "running")
        step_started = time.monotonic()
        check_env_command = [sys.executable, "scripts/check_env.py"]
        if env_file is not None:
            check_env_command.extend(["--env-file", str(env_file)])
        run_step(check_env_command)
        record_step_timing(work_dir, "environment", time.monotonic() - step_started)
        record_step_status(work_dir, "environment", "done")

    record_step_status(work_dir, "transcription", "running")
    step_started = time.monotonic()
    ensure_transcript(
        asr_media, media, transcript_dir, args.language, args.confirm_external_processing, env_file
    )
    record_step_timing(work_dir, "transcription", time.monotonic() - step_started, f"ASR language={args.language}")
    record_step_status(work_dir, "transcription", "done")

    record_step_status(work_dir, "word_stream", "running")
    step_started = time.monotonic()
    ensure_work_files(transcript_dir, work_dir)
    record_step_timing(work_dir, "word_stream", time.monotonic() - step_started)
    record_step_status(work_dir, "word_stream", "done")
    print_segment_generation_policy(work_dir, args.translation_provider)

    record_step_status(work_dir, "prompt", "running")
    step_started = time.monotonic()
    ensure_prompt(work_dir, args.domain_name, args.glossary)
    record_step_timing(work_dir, "prompt", time.monotonic() - step_started)
    record_step_status(work_dir, "prompt", "done")

    record_step_status(work_dir, "source_analysis", "running", "mandatory whole-source analysis before translation")
    step_started = time.monotonic()
    if not source_analysis_gate(transcript_dir, work_dir, source_subtitle, args.translation_provider):
        record_step_timing(work_dir, "source_analysis", time.monotonic() - step_started, "waiting for orchestrator source analysis")
        record_step_status(work_dir, "source_analysis", "waiting", "complete all source-analysis sections and receipt")
        return 3
    record_step_timing(work_dir, "source_analysis", time.monotonic() - step_started, "validated whole-source translation context")
    record_step_status(work_dir, "source_analysis", "done")

    record_step_status(work_dir, "ai_segments", "running", f"translation_provider={args.translation_provider}")
    step_started = time.monotonic()
    if args.translation_provider == "qwen-mt-plus":
        ensure_ai_segments(
            work_dir,
            transcript_dir,
            args.language,
            args.domain_name,
            source_subtitle,
            work_dir / "global_review" / "source-analysis" / "translation-context.json",
            env_file,
        )
    elif not agent_translation_gate(work_dir):
        record_step_timing(work_dir, "ai_segments", time.monotonic() - step_started, "waiting for Agent-native translation")
        record_step_status(work_dir, "ai_segments", "waiting", "complete Agent translation sections and receipt")
        return 4
    record_step_timing(
        work_dir,
        "ai_segments",
        time.monotonic() - step_started,
        f"generated with {args.translation_provider}",
    )
    record_step_status(work_dir, "ai_segments", "done", "segments.txt ready")

    record_step_status(work_dir, "semantic_review", "running", "mandatory whole-document orchestrator review")
    step_started = time.monotonic()
    if not semantic_review_gate(work_dir):
        record_step_timing(work_dir, "semantic_review", time.monotonic() - step_started, "waiting for orchestrator review")
        record_step_status(work_dir, "semantic_review", "waiting", "complete all semantic review sections and receipt")
        return 5
    record_step_timing(work_dir, "semantic_review", time.monotonic() - step_started, "validated full-document semantic review")
    record_step_status(work_dir, "semantic_review", "done")

    record_step_status(work_dir, "deterministic_qa", "running")
    step_started = time.monotonic()
    run_deterministic_qa(
        work_dir,
        args.domain_name,
        args.glossary,
        args.term_rules,
        args.disable_domain_term_checks,
    )
    record_step_timing(work_dir, "deterministic_qa", time.monotonic() - step_started, "term repair, validation, alignment, auto-fix, QA")
    record_step_status(work_dir, "deterministic_qa", "done")

    record_step_status(work_dir, "global_qc", "running", "mandatory whole-document consistency QC")
    step_started = time.monotonic()
    if not final_qc_gate(work_dir):
        record_step_timing(work_dir, "global_qc", time.monotonic() - step_started, "waiting for orchestrator QC")
        record_step_status(work_dir, "global_qc", "waiting", "complete final QC receipt")
        return 6
    record_step_timing(work_dir, "global_qc", time.monotonic() - step_started, "validated full-document final QC")
    record_step_status(work_dir, "global_qc", "done")

    record_step_status(work_dir, "export", "running")
    step_started = time.monotonic()
    export_subtitle_files(
        work_dir,
        subtitles_dir,
        outputs_dir,
        output_base,
        args.source_first,
        args.overwrite_existing,
    )
    record_step_timing(work_dir, "export", time.monotonic() - step_started, "ASS/SRT export after both global gates")
    record_step_status(work_dir, "export", "done", f"outputs={outputs_dir}")
    if not args.keep_workflow_inputs:
        removed_inputs = cleanup_workflow_inputs(media, [asr_media, source_subtitle])
        if removed_inputs:
            print("Removed temporary combined-workflow inputs:", flush=True)
            for removed in removed_inputs:
                print(f"- {removed}", flush=True)
            record_step_status(work_dir, "input_cleanup", "done", f"removed={len(removed_inputs)}")
    finalized = finalize_visible_assets(
        media,
        project_dir,
        localized_title,
        outputs_dir,
        output_base,
        run_dir,
    )
    media = finalized["media"]
    outputs_dir = finalized["outputs_dir"]
    run_dir = finalized["run_dir"]
    work_dir = finalized["work_dir"]
    elapsed = time.monotonic() - started_at
    orchestrator_model = model_name_from_env(args.orchestrator_model)
    translation_model = args.translation_model or ("current Agent model" if args.translation_provider == "agent" else "qwen-mt-plus")
    write_run_summary(
        work_dir,
        run_dir,
        media,
        args.language,
        args.domain_name,
        outputs_dir,
        output_base,
        elapsed,
        orchestrator_model,
        translation_model,
    )
    record_delivery_completion(work_dir, media, finalized["project_dir"])
    print(f"Done in {elapsed:.1f}s ({elapsed / 60:.1f} min)", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print_failure_guidance(exc)
        raise SystemExit(1)
