#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from common import read_json, write_json
from media_probe import VideoGeometry, probe_video_geometry


LAYOUT_NAMES = ("auto", "landscape", "portrait", "square")


@dataclass(frozen=True)
class SubtitleProfile:
    name: str
    play_res_x: int
    play_res_y: int
    zh_font_size: int
    source_font_size: int
    outline: int
    shadow: int
    alignment: int
    margin_l: int
    margin_r: int
    margin_v: int
    zh_wrap_width: float
    zh_wrap_adjustment: float
    source_wrap_width: float | None
    max_zh_lines: int
    max_source_lines: int
    fixed_top_y: int | None = None


PROFILES = {
    "landscape": SubtitleProfile(
        name="landscape",
        play_res_x=1920,
        play_res_y=1080,
        zh_font_size=42,
        source_font_size=24,
        outline=2,
        shadow=1,
        alignment=2,
        margin_l=80,
        margin_r=80,
        margin_v=72,
        zh_wrap_width=36.0,
        zh_wrap_adjustment=3.0,
        source_wrap_width=None,
        max_zh_lines=3,
        max_source_lines=0,
    ),
    "portrait": SubtitleProfile(
        name="portrait",
        play_res_x=1080,
        play_res_y=1920,
        zh_font_size=64,
        source_font_size=34,
        outline=3,
        shadow=1,
        alignment=8,
        margin_l=80,
        margin_r=80,
        margin_v=1370,
        zh_wrap_width=17.0,
        zh_wrap_adjustment=0.5,
        source_wrap_width=27.0,
        max_zh_lines=3,
        max_source_lines=3,
        fixed_top_y=1370,
    ),
    "square": SubtitleProfile(
        name="square",
        play_res_x=1080,
        play_res_y=1080,
        zh_font_size=42,
        source_font_size=24,
        outline=2,
        shadow=1,
        alignment=2,
        margin_l=80,
        margin_r=80,
        margin_v=72,
        zh_wrap_width=24.0,
        zh_wrap_adjustment=2.0,
        source_wrap_width=38.0,
        max_zh_lines=3,
        max_source_lines=3,
    ),
}


def classify_layout(geometry: VideoGeometry) -> str:
    width = geometry.display_width
    height = geometry.display_height
    if min(width, height) <= 0:
        raise RuntimeError("Video dimensions must be positive.")
    ratio = width / height
    if 0.9 <= ratio <= 1.1:
        return "square"
    return "portrait" if height > width else "landscape"


def resolve_layout(media: Path, requested: str = "auto") -> tuple[SubtitleProfile, VideoGeometry]:
    if requested not in LAYOUT_NAMES:
        raise RuntimeError(f"Unsupported subtitle layout: {requested}")
    geometry = probe_video_geometry(media)
    resolved = classify_layout(geometry) if requested == "auto" else requested
    return PROFILES[resolved], geometry


def bind_subtitle_layout(work_dir: Path, media: Path, requested: str = "auto") -> SubtitleProfile:
    profile, geometry = resolve_layout(media, requested)
    record = {
        "schema_version": 1,
        "media": str(media.resolve()),
        "requested": requested,
        "resolved": profile.name,
        "source_geometry": asdict(geometry),
        "profile": asdict(profile),
    }
    path = work_dir / "subtitle_layout.json"
    if path.exists():
        existing = read_json(path)
        if existing != record:
            raise RuntimeError(
                "This run is already bound to a different subtitle layout or video geometry. "
                "Resume with the same --subtitle-layout, or start a new --run-id."
            )
    else:
        write_json(path, record)
    return profile


def rebind_finalized_subtitle_layout(work_dir: Path, media: Path) -> SubtitleProfile:
    """Rebind only a renamed delivered video whose geometry and profile are unchanged."""
    path = work_dir / "subtitle_layout.json"
    if not path.is_file():
        raise RuntimeError("Missing subtitle_layout.json; finalized media layout cannot be rebound.")
    record = read_json(path)
    requested = str(record.get("requested") or "")
    if requested not in LAYOUT_NAMES:
        raise RuntimeError("subtitle_layout.json has an invalid requested layout.")
    profile, geometry = resolve_layout(media, requested)
    expected = {
        "schema_version": 1,
        "media": str(media.resolve()),
        "requested": requested,
        "resolved": profile.name,
        "source_geometry": asdict(geometry),
        "profile": asdict(profile),
    }
    existing_without_media = {key: value for key, value in record.items() if key != "media"}
    expected_without_media = {key: value for key, value in expected.items() if key != "media"}
    if existing_without_media != expected_without_media:
        raise RuntimeError(
            "Finalized media geometry or subtitle profile changed; the immutable layout cannot be rebound."
        )
    write_json(path, expected)
    return profile


def load_bound_subtitle_layout(work_dir: Path) -> SubtitleProfile:
    path = work_dir / "subtitle_layout.json"
    if not path.is_file():
        raise RuntimeError("Missing subtitle_layout.json. Resume through the gated workflow to bind the video layout.")
    record = read_json(path)
    resolved = str(record.get("resolved") or "")
    profile = PROFILES.get(resolved)
    if not profile or record.get("profile") != asdict(profile):
        raise RuntimeError("subtitle_layout.json does not match a supported immutable layout profile.")
    return profile
