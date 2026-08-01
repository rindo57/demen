"""
FFmpeg argument parsing, merging, and command building.
"""

import re
import shlex
from typing import Optional

from config import DEFAULT_ENCODE_SETTINGS, FFMPEG_FLAG_MAP


# ── Parsing ───────────────────────────────────────────────────

def parse_set_command(args_string: str) -> dict[str, str]:
    """
    Parse FFmpeg arguments from a ``/set`` command.

    Example input:  ``-vf "scale=1920:1080:flags=spline" -crf 18``
    Returns:        ``{"vf": "scale=1920:1080:flags=spline", "crf": "18"}``
    """
    overrides: dict[str, str] = {}
    try:
        tokens = shlex.split(args_string)
    except ValueError:
        tokens = args_string.split()

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in FFMPEG_FLAG_MAP:
            key = FFMPEG_FLAG_MAP[tok]
            if i + 1 < len(tokens) and not tokens[i + 1].startswith("-"):
                overrides[key] = tokens[i + 1]
                i += 2
            else:
                i += 1
        else:
            i += 1
    return overrides


def merge_settings(
    base: dict[str, str],
    overrides: dict[str, str],
) -> dict[str, str]:
    """Return *base* with *overrides* applied on top."""
    merged = base.copy()
    merged.update(overrides)
    return merged


# ── Resolution / Codec Helpers ────────────────────────────────

def get_resolution_from_vf(vf: str) -> Optional[str]:
    """``'scale=-2:480:flags=spline'`` → ``'480p'``, empty vf → ``'Source'``"""
    if not vf:
        return "Source"
    m = re.search(r"scale=-2:(\d+):flags=spline", vf)
    if m:
        height = int(m.group(1))
        # Map height to resolution preset
        if height == 480:
            return "480p"
        elif height == 720:
            return "720p"
        elif height == 1080:
            return "1080p"
        else:
            # Fallback to generic height-based naming
            return f"{height}p"
    return None

def get_resolution_dimensions(vf: str) -> Optional[tuple[int, int]]:
    """Return ``(width, height)`` parsed from a ``-vf scale=…`` value."""
    m = re.search(r"scale=(\d+):(\d+)", vf)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


_AUDIO_CODEC_LABELS = {
    "libopus": "Opus", "aac": "AAC", "libmp3lame": "MP3",
    "libvorbis": "Vorbis", "flac": "FLAC", "copy": "Copy",
    "ac3": "AC3", "eac3": "EAC3",
}

_VIDEO_CODEC_LABELS = {
    "libx265": "x265", "libx264": "x264",
    "libsvtav1": "AV1", "libvpx-vp9": "VP9", "copy": "Copy",
}


def get_audio_codec_label(codec: str) -> str:
    return _AUDIO_CODEC_LABELS.get(codec, codec)


def get_video_codec_label(codec: str) -> str:
    return _VIDEO_CODEC_LABELS.get(codec, codec)


# ── Command Building ─────────────────────────────────────────

def build_ffmpeg_command(
    input_file: str,
    output_file: str,
    settings: dict[str, str],
) -> list[str]:
    """Build the full ``ffmpeg`` CLI invocation from *settings*."""
    from pathlib import Path as _Path

    # Use the output filename stem as the container title metadata
    container_title = _Path(output_file).stem

    res_label = get_resolution_from_vf(settings.get("vf", ""))
    v_label = get_video_codec_label(settings.get("video_codec", "libx265"))
    stream_title = (
        f"[AniDL] ~ {res_label} {v_label} 10Bit"
        if res_label and res_label != "Source"
        else f"[AniDL] ~ Source {v_label} 10Bit"
    )

    cmd = [
        "ffmpeg", "-y",
        "-i", input_file,
        "-metadata", f"title={container_title}",
        "-pix_fmt", settings.get("pix_fmt", "yuv420p10le"),
        # Only add -r flag if framerate is explicitly set in settings
        # Omit it to keep source framerate
    ]

    # Add framerate only if explicitly provided (not None/empty)
    if settings.get("framerate"):
        cmd.extend(["-r", settings.get("framerate")])

    # Add video filter if present
    if settings.get("vf"):
        cmd.extend(["-vf", settings.get("vf")])

    cmd.extend([
        "-preset", settings.get("preset", "medium"),
        "-c:v", settings.get("video_codec", "libx265"),
        "-crf", settings.get("crf", "23"),
    ])

    # x265-params only when using libx265
    if settings.get("video_codec", "libx265") == "libx265" and settings.get("x265_params"):
        cmd.extend(["-x265-params", settings.get("x265_params")])

    cmd.extend([
        "-metadata:s:v:0", f"title={stream_title}",
        "-metadata", "artist=AniDL",
        "-metadata", "album=AniDL Encodes",
        "-metadata", "comment=Visit our site AniDL.org for more encodes",
        "-metadata", "copyright=AniDL Encodes",
        "-metadata", "encoder=Diablo",
        "-metadata", "encoding_tool=AniDL",
        "-metadata", "encoded_by=Diablo",
        "-metadata", "source=AniDL",
        "-metadata", "content_origin=AniDL",
        "-map", "0:v",
        "-c:a", settings.get("audio_codec", "libopus"),
        "-b:a", settings.get("audio_bitrate", "96k"),
        "-ac", settings.get("audio_channels", "2"),
        "-map", "0:a",
        "-c:s", settings.get("sub_codec", "copy"),
        "-map", "0:s?",
        "-map", "0:t?",
        "-progress", "pipe:1",
        "-nostats",
        output_file,
    ])
    return cmd


# ── Display Formatting ────────────────────────────────────────

def format_settings_display(settings: dict[str, str]) -> str:
    """Return an HTML-formatted overview for ``/settings``."""
    vf = settings.get("vf", "")
    res = get_resolution_from_vf(vf) if vf else "Source (no rescaling)"
    vc = get_video_codec_label(settings.get("video_codec", ""))
    ac = get_audio_codec_label(settings.get("audio_codec", ""))

    # Show framerate as "Source" if not set, otherwise show the value
    framerate = settings.get("framerate")
    framerate_display = "Source (auto)" if not framerate else framerate

    return (
        f"<b>🎬 Video Codec:</b>  {vc}\n"
        f"<b>📐 Resolution:</b>   {res or 'Auto'}\n"
        f"<b>🎯 CRF:</b>          {settings.get('crf', 'N/A')}\n"
        f"<b>⚡ Preset:</b>       {settings.get('preset', 'N/A')}\n"
        f"<b>🖼 Pixel Format:</b> {settings.get('pix_fmt', 'N/A')}\n"
        f"<b>🎞 Framerate:</b>    {framerate_display}\n"
        f"<b>🔊 Audio Codec:</b>  {ac}\n"
        f"<b>🔉 Audio Bitrate:</b>{settings.get('audio_bitrate', 'N/A')}\n"
        f"<b>📢 Channels:</b>     {settings.get('audio_channels', 'N/A')}\n"
        f"<b>💬 Subtitles:</b>    {settings.get('sub_codec', 'N/A')}\n"
    )


def format_settings_short(settings: dict[str, str]) -> str:
    """One-line summary for captions (e.g. ``720p, CRF 23, Opus 96k``)."""
    res = get_resolution_from_vf(settings.get("vf", "")) or "Source"
    crf = settings.get("crf", "?")
    ac = get_audio_codec_label(settings.get("audio_codec", ""))
    ab = settings.get("audio_bitrate", "?")
    preset = settings.get("preset", "?")
    return f"{res}, CRF {crf}, {ac} {ab}, preset {preset}"
