"""
FFmpeg encoder – runs encoding as an async subprocess with progress tracking.
Supports two modes:
  - USE_VAPOURSYNTH=False  →  plain FFmpeg (original behaviour)
  - USE_VAPOURSYNTH=True   →  vspipe | x265 → FFmpeg mux pipeline
"""

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Callable, Optional

from config import MAX_UPLOAD_SIZE, PROGRESS_UPDATE_INTERVAL, TEMP_DIR
from utils.ffmpeg_args import build_ffmpeg_command, get_resolution_from_vf
from utils.helpers import format_duration, format_size

log = logging.getLogger(__name__)

# ── Toggle: set True to use the VapourSynth + x265 pipeline ──────────────────
# Requires: vspipe, x265, and VS plugins (bm3dcpu, neo_f3kdb, nnedi3, eedi3m, lsmas, descale)
USE_VAPOURSYNTH: bool = True

# VS worker threads (leave ~4 free for x265; total vCores = 24)
VS_THREADS: int = 20


async def get_duration(file_path: str) -> Optional[float]:
    """Use *ffprobe* to get the duration of *file_path* in seconds."""
    # Prefix with "file:" so ffprobe doesn't interpret brackets as patterns
    safe_path = f"file:{file_path}" if not file_path.startswith("file:") else file_path
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        safe_path,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        raw = stdout.decode().strip()
        if raw:
            return float(raw)
        log.warning("ffprobe returned empty output for %s", file_path)
        return None
    except Exception as exc:
        log.warning("ffprobe failed for %s: %s", file_path, exc)
        return None


class EncodeProgress:
    """Holds real-time encoding progress data."""

    __slots__ = (
        "percentage", "speed", "fps", "eta_seconds",
        "current_time", "total_duration", "started_at",
    )

    def __init__(self, total_duration: float):
        self.total_duration = total_duration
        self.percentage: float = 0.0
        self.speed: str = "0x"
        self.fps: float = 0.0
        self.eta_seconds: float = 0.0
        self.current_time: float = 0.0
        self.started_at: float = time.monotonic()


async def encode_video(
    input_file: str,
    output_file: str,
    settings: dict[str, str],
    progress_callback: Optional[Callable[[EncodeProgress], None]] = None,
) -> bool:
    """
    Encode *input_file* → *output_file*.

    When USE_VAPOURSYNTH is True, routes through the VapourSynth → x265 → FFmpeg
    mux pipeline for maximum quality.  Otherwise falls back to plain FFmpeg.

    Calls *progress_callback* periodically with an ``EncodeProgress`` object.
    Returns ``True`` on success, ``False`` on failure.
    """
    if USE_VAPOURSYNTH:
        return await _encode_vapoursynth(input_file, output_file, settings, progress_callback)
    return await _encode_ffmpeg(input_file, output_file, settings, progress_callback)


# ── VapourSynth pipeline ──────────────────────────────────────────────────────

async def _encode_vapoursynth(
    input_file: str,
    output_file: str,
    settings: dict[str, str],
    progress_callback: Optional[Callable[[EncodeProgress], None]] = None,
) -> bool:
    """Route through VapourSynth → x265 → FFmpeg mux pipeline."""
    from core.vs_filter import encode_with_vapoursynth, get_frame_count

    # Determine the resolution key from settings
    vf = settings.get("vf", "")
    res_label = get_resolution_from_vf(vf) if vf else "source"
    if res_label not in ("480p", "720p", "1080p", "source"):
        res_label = "1080p"

    # Get total frames for progress tracking
    total_frames = await get_frame_count(input_file)
    duration     = await get_duration(input_file) or 1.0

    progress     = EncodeProgress(duration)
    last_cb      = 0.0

    async def _vs_progress(pct: float, eta: float, frames: int):
        nonlocal last_cb
        progress.percentage = pct
        progress.eta_seconds = eta
        now = time.monotonic()
        if progress_callback and (now - last_cb >= PROGRESS_UPDATE_INTERVAL):
            last_cb = now
            try:
                await _maybe_await(progress_callback, progress)
            except Exception:
                pass

    success = await encode_with_vapoursynth(
        input_file=input_file,
        output_file=output_file,
        resolution=res_label,
        total_frames=total_frames,
        progress_callback=_vs_progress,
        vs_threads=VS_THREADS,
    )

    # Final callback
    if progress_callback:
        progress.percentage = 100.0
        progress.eta_seconds = 0.0
        try:
            await _maybe_await(progress_callback, progress)
        except Exception:
            pass

    return success


# ── Plain FFmpeg pipeline (original) ─────────────────────────────────────────

async def _encode_ffmpeg(
    input_file: str,
    output_file: str,
    settings: dict[str, str],
    progress_callback: Optional[Callable[[EncodeProgress], None]] = None,
) -> bool:
    """Original FFmpeg-only encode path."""
    duration = await get_duration(input_file)
    if duration is None or duration <= 0:
        duration = 1.0

    cmd = build_ffmpeg_command(input_file, output_file, settings)
    log.info("FFmpeg command: %s", " ".join(cmd))

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    progress = EncodeProgress(duration)
    last_callback = 0.0

    try:
        async for raw_line in proc.stdout:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue

            # Parse -progress key=value lines
            if "=" in line:
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()

                if key == "out_time_us":
                    try:
                        us = int(value)
                        progress.current_time = us / 1_000_000
                        progress.percentage = min(
                            (progress.current_time / duration) * 100, 99.9
                        )
                        elapsed = time.monotonic() - progress.started_at
                        if progress.percentage > 0:
                            total_est = elapsed / (progress.percentage / 100)
                            progress.eta_seconds = max(total_est - elapsed, 0)
                    except (ValueError, ZeroDivisionError):
                        pass

                elif key == "fps":
                    try:
                        progress.fps = float(value)
                    except ValueError:
                        pass

                elif key == "speed":
                    progress.speed = value

                elif key == "progress" and value == "end":
                    progress.percentage = 100.0
                    progress.eta_seconds = 0.0

            # Throttled callback
            now = time.monotonic()
            if progress_callback and (now - last_callback >= PROGRESS_UPDATE_INTERVAL):
                last_callback = now
                try:
                    await _maybe_await(progress_callback, progress)
                except Exception:
                    pass

        await proc.wait()

    except Exception as exc:
        log.error("Encoding error: %s", exc)
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return False

    if proc.returncode != 0:
        stderr_tail = ""
        try:
            stderr_data = await proc.stderr.read()
            stderr_tail = stderr_data.decode(errors="replace")[-500:]
        except Exception:
            pass
        log.error("FFmpeg exited with code %d: %s", proc.returncode, stderr_tail)
        return False

    # Final callback
    if progress_callback:
        progress.percentage = 100.0
        progress.eta_seconds = 0.0
        try:
            await _maybe_await(progress_callback, progress)
        except Exception:
            pass

    return True


async def _maybe_await(func, *args):
    """Call *func* – if it returns a coroutine, await it."""
    result = func(*args)
    if asyncio.iscoroutine(result):
        await result


def check_output_size(output_path: str) -> tuple[bool, int]:
    """
    Check whether the encoded file exceeds the Telegram upload limit.
    Returns ``(is_ok, size_bytes)``.
    """
    size = os.path.getsize(output_path)
    return size <= MAX_UPLOAD_SIZE, size


def build_caption(
    original_name: str,
    settings: dict[str, str],
    output_size: int,
    elapsed_seconds: float,
) -> str:
    """Build the HTML caption for the uploaded document."""
    from utils.ffmpeg_args import (
        format_settings_short,
        get_resolution_from_vf,
    )

    res = get_resolution_from_vf(settings.get("vf", "")) or "?"
    dims = ""
    try:
        import re
        m = re.search(r"scale=(\d+):(\d+)", settings.get("vf", ""))
        if m:
            dims = f"{m.group(1)}x{m.group(2)}"
    except Exception:
        pass

    return (
        "<b>✅ Encoded Successfully!</b>\n"
        "\n"
        f"<b>📁 Original:</b> {original_name}\n"
        f"<b>🔧 Settings:</b> {format_settings_short(settings)}\n"
        f"<b>🎬 Resolution:</b> {dims or res}\n"
        f"<b>📦 Size:</b> {format_size(output_size)}\n"
        f"<b>⏱️ Time taken:</b> {format_duration(elapsed_seconds)}\n"
        "\n"
        "<i>Download the file from the document below.</i>"
    )
