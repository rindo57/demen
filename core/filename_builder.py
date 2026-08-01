"""
Filename builder – construct the AniDL-branded output filename.

Uses **anitopy** (best for anime) with **guessit** as fallback.
"""

import logging
import re
from pathlib import Path
from typing import Optional

from utils.ffmpeg_args import get_audio_codec_label, get_resolution_from_vf

log = logging.getLogger(__name__)

# Try importing anitopy (preferred) and guessit (fallback)
try:
    import anitopy
    HAS_ANITOPY = True
except ImportError:
    HAS_ANITOPY = False
    log.warning("anitopy not installed – falling back to guessit only")

try:
    from guessit import guessit
    HAS_GUESSIT = True
except ImportError:
    HAS_GUESSIT = False
    log.warning("guessit not installed – filename parsing may be limited")


def _clean_source(source: Optional[str]) -> Optional[str]:
    """
    Normalize source tags:  ``'BDrip'`` → ``'BD'``,
    ``'Blu-ray Disc'`` → ``'BD'``, etc.
    """
    if not source:
        return None
    s = re.sub(r"(?i)[-_]?rip$", "", source).strip()
    s = re.sub(r"(?i)^blu[-\s]?ray(\s*disc)?$", "BD", s)
    return s or None


def _parse_with_anitopy(filename: str) -> dict:
    """Parse *filename* with anitopy and normalise keys."""
    if not HAS_ANITOPY:
        return {}
    try:
        data = anitopy.parse(filename)
    except Exception:
        return {}

    result: dict = {}
    if "anime_title" in data:
        result["title"] = data["anime_title"]
    if "anime_season" in data:
        result["season"] = int(data["anime_season"])
    if "episode_number" in data:
        ep = data["episode_number"]
        result["episode"] = int(ep) if ep.isdigit() else ep
    if "source" in data:
        result["source"] = data["source"]
    if "video_resolution" in data:
        result["screen_size"] = data["video_resolution"]
    if "video_term" in data:
        result["video_codec"] = data["video_term"]
    if "audio_term" in data:
        result["audio_codec"] = data["audio_term"]
    if "release_group" in data:
        result["release_group"] = data["release_group"]
    if "file_checksum" in data:
        result["crc32"] = data["file_checksum"]
    return result


def _parse_with_guessit(filename: str) -> dict:
    """Parse *filename* with guessit."""
    if not HAS_GUESSIT:
        return {}
    try:
        data = guessit(filename)
    except Exception:
        return {}

    result: dict = {}
    for key in ("title", "season", "episode", "source",
                "screen_size", "video_codec", "audio_codec",
                "release_group"):
        if key in data:
            result[key] = data[key]
    return result


def parse_filename(filename: str) -> dict:
    """
    Best-effort metadata extraction.
    Tries anitopy first, fills gaps with guessit.
    """
    result = _parse_with_anitopy(filename)
    fallback = _parse_with_guessit(filename)
    for k, v in fallback.items():
        if k not in result:
            result[k] = v
    return result


def build_output_filename(
    original_filename: str,
    settings: dict[str, str],
    crc32: Optional[str] = None,
) -> str:
    """
    Construct the encoded output filename in the AniDL format:

        ``[AniDL] {title} - S{ss}E{ee} [{source} {res} x265 10Bit][{audio}][{group}][{CRC32}].mkv``

    Missing fields are omitted gracefully.
    Pass *crc32* (e.g. ``"1C30AA46"``) to append a checksum bracket.
    """
    meta = parse_filename(Path(original_filename).stem)

    title = meta.get("title", Path(original_filename).stem)
    season = meta.get("season")
    episode = meta.get("episode")
    source = _clean_source(meta.get("source"))
    release_group = meta.get("release_group")

    # ── Clean up dot-separated titles ─────────────────────────
    # Filenames like "I.Was.Reincarnated.as.the.7th.Prince.S02E01.1080p..."
    # produce a title full of dots.  Convert to spaces.
    if "." in title and " " not in title:
        title = title.replace(".", " ").strip()

    # Strip a trailing season/episode pattern that leaked into the title
    # e.g. "I Was Reincarnated as the 7th Prince S02E01" → drop "S02E01"
    title = re.sub(
        r"\s*-?\s*S\d{1,2}E\d{1,4}\.?\s*$", "", title, flags=re.IGNORECASE
    ).strip()

    # Encoded resolution from settings
    enc_res = get_resolution_from_vf(settings.get("vf", ""))

    # Encoded audio codec from settings
    enc_audio = get_audio_codec_label(settings.get("audio_codec", "libopus"))

    # Encoded video codec label
    vc = settings.get("video_codec", "libx265")
    vc_label = "x265" if vc == "libx265" else "x264" if vc == "libx264" else vc

    # ── Build the name ────────────────────────────────────────
    parts = [f"[AniDL] {title}"]

    # Season + Episode
    if season is not None and episode is not None:
        s = int(season) if isinstance(season, (int, float)) else season
        e = int(episode) if isinstance(episode, (int, float)) else episode
        if isinstance(s, int) and isinstance(e, int):
            parts.append(f"S{s:02d}E{e:02d}")
        else:
            parts.append(f"S{s}E{e}")
    elif episode is not None:
        e = int(episode) if str(episode).isdigit() else episode
        if isinstance(e, int):
            parts.append(f"E{e:02d}")
        else:
            parts.append(f"E{e}")

    name = " - ".join(parts)

    # Quality bracket: [BD 720p x265 10Bit]
    quality_parts: list[str] = []
    if source:
        quality_parts.append(source)
    if enc_res:
        quality_parts.append(enc_res)
    quality_parts.append(f"{vc_label} 10Bit")
    name += f" [{' '.join(quality_parts)}]"

    # Audio bracket: [Opus]
    if enc_audio:
        name += f"[{enc_audio}]"

    # Release group bracket: [EXP]
    if release_group:
        name += f"[{release_group}]"

    # CRC32 bracket: [1C30AA46]
    if crc32:
        name += f"[{crc32}]"

    name += ".mkv"

    # Sanitize for filesystem
    for ch in '<>:"/\\|?*':
        name = name.replace(ch, "")

    return name
