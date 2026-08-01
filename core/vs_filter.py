"""
VapourSynth filter pipeline for anime encoding.

Generates a .vpy script for each input file then runs:
    vspipe --y4m script.vpy - | x265 [params] --output video.hevc

After encoding, FFmpeg muxes the HEVC stream with the original audio/subtitles.
"""

import asyncio
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

# ── x265 CLI params (best quality, tuned for anime, 24 vCores) ───────────────
_X265_BEST_PARAMS: dict[str, str] = {
    "preset":           "veryslow",
    "crf":              "16",
    "deblock":          "3,3",
    "no-sao":           "",            # flag, no value
    "subme":            "7",
    "me":               "3",           # STAR search
    "merange":          "57",
    "psy-rd":           "0.75",
    "psy-rdoq":         "0.30",
    "aq-mode":          "3",
    "aq-strength":      "0.70",
    "bframes":          "8",
    "b-adapt":          "2",
    "ref":              "6",
    "rc-lookahead":     "80",
    "lookahead-slices": "0",
    "limit-refs":       "0",
    "limit-modes":      "0",
    "rect":             "",            # flag
    "amp":              "",            # flag
    "pmode":            "",            # flag
    "pme":              "",            # flag
    "frame-threads":    "2",
    "pools":            "0-23",        # 24 vCores
    "no-info":          "",            # flag
    "output-depth":     "10",
}

# ── Per-resolution VapourSynth parameters ─────────────────────────────────────
_VS_PARAMS: dict[str, dict] = {
    "480p":   {"out_w": 854,  "out_h": 480,  "bm3d_sigma": [2, 0], "bm3d_radius": 0,
               "db_range": 12, "db_y": 40, "db_grain": 12},
    "720p":   {"out_w": 1280, "out_h": 720,  "bm3d_sigma": [3, 0], "bm3d_radius": 1,
               "db_range": 15, "db_y": 52, "db_grain": 16},
    "1080p":  {"out_w": 1920, "out_h": 1080, "bm3d_sigma": [4, 2], "bm3d_radius": 1,
               "db_range": 20, "db_y": 64, "db_grain": 24},
    "source": {"out_w": None, "out_h": None, "bm3d_sigma": [4, 2], "bm3d_radius": 1,
               "db_range": 20, "db_y": 64, "db_grain": 24},
}

# Native resolution map — update after running getnative on each source
# Format: "show_name_keyword": (native_w, native_h, "kernel")
# kernel: "bilinear" | "bicubic" | "lanczos"
NATIVE_RES_MAP: dict[str, tuple[int, int, str]] = {
    # Example entries — add your series here:
    # "kimetsu":   (1280, 720, "bicubic"),
    # "chainsaw":  (1280, 720, "bicubic"),
    # "frieren":   (1920, 1080, "bicubic"),   # some are natively 1080p
}

# Default native resolution when not in NATIVE_RES_MAP
# Set to None to skip descaling (safe default)
DEFAULT_NATIVE: Optional[tuple[int, int, str]] = (1280, 720, "bicubic")


# ── .vpy Script Generation ────────────────────────────────────────────────────

def _detect_native(input_path: str) -> tuple[Optional[int], Optional[int], str]:
    """Return (native_w, native_h, kernel) by matching filename against NATIVE_RES_MAP."""
    name_lower = Path(input_path).stem.lower()
    for keyword, params in NATIVE_RES_MAP.items():
        if keyword.lower() in name_lower:
            return params
    if DEFAULT_NATIVE:
        return DEFAULT_NATIVE
    return (None, None, "bicubic")


def build_vpy_script(
    input_path: str,
    resolution: str,
    vs_threads: int = 20,
) -> str:
    """
    Generate a VapourSynth .vpy script string for the given input and resolution.

    Args:
        input_path:  Absolute path to the source video file.
        resolution:  One of '480p', '720p', '1080p', 'source'.
        vs_threads:  Number of VS worker threads (leave ~4 for x265).
    """
    params   = _VS_PARAMS.get(resolution, _VS_PARAMS["1080p"])
    out_w    = params["out_w"]
    out_h    = params["out_h"]
    sigma    = params["bm3d_sigma"]
    radius   = params["bm3d_radius"]
    db_range = params["db_range"]
    db_y     = params["db_y"]
    db_grain = params["db_grain"]

    native_w, native_h, kernel = _detect_native(input_path)
    do_descale = native_w is not None and native_h is not None

    # Escape backslashes for Python string inside the generated script
    safe_path = input_path.replace("\\", "\\\\")

    # Build descale block
    if do_descale:
        if kernel == "bilinear":
            descale_call = (
                f"descaled = core.descale.Debilinear(src16, "
                f"width={native_w}, height={native_h})"
            )
        elif kernel == "lanczos":
            descale_call = (
                f"descaled = core.descale.Delanczos(src16, "
                f"width={native_w}, height={native_h}, taps=3)"
            )
        else:  # bicubic (most common for anime)
            descale_call = (
                f"descaled = core.descale.Debicubic(src16, "
                f"width={native_w}, height={native_h}, b=0, c=1)"
            )

        if out_w and out_h:
            rescale_block = f"""
# Rescale back to output resolution using nnedi3 (sharpest)
rescaled = core.nnedi3.nnedi3(denoised, field=1, dh=True, nsize=4, nns=4, qual=2)
rescaled = core.std.Transpose(rescaled)
rescaled = core.nnedi3.nnedi3(rescaled, field=1, dh=True, nsize=4, nns=4, qual=2)
rescaled = core.std.Transpose(rescaled)
# Correct nnedi3 half-pixel shift
clip = core.resize.Spline36(rescaled, width={out_w}, height={out_h},
                             src_left=-0.5, src_top=-0.5)
"""
        else:
            # Source resolution — rescale to native→original dimensions
            rescale_block = f"""
clip = core.resize.Spline36(denoised, width={native_w}, height={native_h})
"""
    else:
        descale_call = "descaled = src16  # No descale"
        if out_w and out_h:
            rescale_block = f"\nclip = core.resize.Spline36(denoised, width={out_w}, height={out_h})\n"
        else:
            rescale_block = "\nclip = denoised\n"

    script = f'''"""
Auto-generated VapourSynth filter script.
Source  : {safe_path}
Target  : {resolution}
"""
import vapoursynth as vs
core = vs.core

# ── Threading ────────────────────────────────────────────────────────────────
core.num_threads = {vs_threads}

# ── Source ───────────────────────────────────────────────────────────────────
src = core.ffms2.Source(r"{safe_path}")

# Convert to 16-bit for processing headroom
src16 = core.resize.Bicubic(src, format=vs.YUV420P16)

# ── Descale ──────────────────────────────────────────────────────────────────
{descale_call}

# ── Denoise ──────────────────────────────────────────────────────────────────
if hasattr(core, 'bm3dcpu'):
    ref      = core.bm3dcpu.BM3D(descaled, sigma={sigma}, radius={radius}, profile="np")
    denoised = core.bm3dcpu.BM3D(descaled, ref=ref, sigma={sigma}, radius={radius}, profile="np", final_=True)
elif hasattr(core, 'bm3d'):
    ref      = core.bm3d.Basic(descaled, sigma={sigma})
    denoised = core.bm3d.Final(descaled, ref=ref, sigma={sigma})
else:
    # High-quality built-in bilateral denoiser fallback
    denoised = core.std.Bilateral(descaled, sigmaS=3.0, sigmaR=0.02)
{rescale_block}
# ── Deband ───────────────────────────────────────────────────────────────────
if hasattr(core, 'neo_f3kdb'):
    debanded = core.neo_f3kdb.Deband(
        clip, range={db_range}, y={db_y}, cb=int({db_y}*0.75), cr=int({db_y}*0.75),
        grainy={db_grain}, grainc=int({db_grain}//2), sample_mode=2, blur_first=True
    )
elif hasattr(core, 'f3kdb'):
    debanded = core.f3kdb.Deband(
        clip, range={db_range}, y={db_y}, cb=int({db_y}*0.75), cr=int({db_y}*0.75),
        grainy={db_grain}, grainc=int({db_grain}//2), sample_mode=2
    )
else:
    # High quality built-in dither deband fallback
    debanded = core.resize.Bicubic(clip, dither_type="error_diffusion")


# ── Anti-aliasing ─────────────────────────────────────────────────────────────
# EEDI3 on both axes — fixes jagged diagonal edges
aa = core.eedi3m.EEDI3(debanded, field=1, alpha=0.25, beta=0.25, gamma=40,
                        nrad=2, mdis=20)
aa = core.std.Transpose(aa)
aa = core.eedi3m.EEDI3(aa, field=1, alpha=0.25, beta=0.25, gamma=40,
                        nrad=2, mdis=20)
aa = core.std.Transpose(aa)

# ── Output (10-bit for x265) ──────────────────────────────────────────────────
out = core.resize.Bicubic(aa, format=vs.YUV420P10)
out.set_output()
'''
    return script


# ── x265 CLI builder ──────────────────────────────────────────────────────────

def build_x265_command(output_hevc: str, x265_params: Optional[dict] = None) -> list[str]:
    """Build x265 CLI argument list from params dict."""
    params = dict(_X265_BEST_PARAMS)
    if x265_params:
        params.update(x265_params)

    cmd = ["x265", "--y4m"]
    for flag, value in params.items():
        if value == "":
            # Boolean flag (no value)
            cmd.append(f"--{flag}")
        else:
            cmd.extend([f"--{flag}", str(value)])

    cmd.extend(["--output", output_hevc, "-"])
    return cmd


# ── Progress Parsing ──────────────────────────────────────────────────────────

def _parse_x265_progress(line: str) -> Optional[float]:
    """
    x265 writes lines like:
        encoded 1234 frames: 3.45 fps, 1234.56 kb/s, ...
    We count frames to estimate progress.
    Returns frame count or None.
    """
    import re
    m = re.search(r"encoded\s+(\d+)\s+frames", line)
    if m:
        return int(m.group(1))
    return None


# ── Main Encode Entry Point ───────────────────────────────────────────────────

async def encode_with_vapoursynth(
    input_file: str,
    output_file: str,
    resolution: str,
    total_frames: Optional[int] = None,
    progress_callback: Optional[Callable] = None,
    x265_overrides: Optional[dict] = None,
    vs_threads: int = 20,
) -> bool:
    """
    Full VapourSynth → x265 → FFmpeg mux pipeline.

    Steps:
      1. Write .vpy script to a temp directory.
      2. Run: vspipe --y4m script.vpy - | x265 ... --output tmp.hevc
      3. Mux tmp.hevc + original audio/subs into output_file (MKV) via FFmpeg.

    Returns True on success, False on failure.
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="anidl_vs_"))
    vpy_path  = tmp_dir / "filter.vpy"
    hevc_path = tmp_dir / "video.hevc"

    try:
        # ── Step 1: Write VPY script ─────────────────────────────────────────
        script = build_vpy_script(input_file, resolution, vs_threads=vs_threads)
        vpy_path.write_text(script, encoding="utf-8")
        log.info("[VS] Script written to %s", vpy_path)

        # ── Step 2: vspipe | x265 ────────────────────────────────────────────
        vspipe_cmd = ["vspipe", "--y4m", str(vpy_path), "-"]
        x265_cmd   = build_x265_command(str(hevc_path), x265_overrides)

        log.info("[VS] vspipe: %s", " ".join(vspipe_cmd))
        log.info("[VS] x265:   %s", " ".join(x265_cmd))

        vspipe_proc = await asyncio.create_subprocess_exec(
            *vspipe_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        x265_proc = await asyncio.create_subprocess_exec(
            *x265_cmd,
            stdin=vspipe_proc.stdout,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,  # x265 progress goes to stderr; merge
        )

        # Close vspipe stdout in parent so x265 gets EOF when vspipe exits
        if vspipe_proc.stdout:
            vspipe_proc.stdout._transport.close()  # type: ignore[attr-defined]

        started_at = time.monotonic()
        frames_done = 0

        async for raw in x265_proc.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue

            # Parse progress from x265 output
            f = _parse_x265_progress(line)
            if f is not None:
                frames_done = f
                if progress_callback and total_frames and total_frames > 0:
                    pct = min((frames_done / total_frames) * 100, 99.9)
                    elapsed = time.monotonic() - started_at
                    eta = 0.0
                    if pct > 0:
                        eta = max((elapsed / (pct / 100)) - elapsed, 0)
                    try:
                        await _maybe_await(progress_callback, pct, eta, frames_done)
                    except Exception:
                        pass

            log.debug("[x265] %s", line)

        await x265_proc.wait()
        await vspipe_proc.wait()

        if vspipe_proc.returncode not in (0, None):
            log.error("[VS] vspipe exited with code %d", vspipe_proc.returncode)
            return False
        if x265_proc.returncode not in (0, None):
            log.error("[VS] x265 exited with code %d", x265_proc.returncode)
            return False

        if not hevc_path.exists() or hevc_path.stat().st_size == 0:
            log.error("[VS] HEVC output missing or empty")
            return False

        # ── Step 3: Mux HEVC + original audio/subs ───────────────────────────
        mux_ok = await _mux_streams(str(hevc_path), input_file, output_file)
        return mux_ok

    except Exception as exc:
        log.error("[VS] Pipeline error: %s", exc, exc_info=True)
        return False

    finally:
        # Clean up temp dir
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass


async def _mux_streams(hevc_path: str, source_path: str, output_path: str) -> bool:
    """
    Mux encoded HEVC video with audio and subtitles from the original source.

    FFmpeg command:
        ffmpeg -i video.hevc -i source.mkv
               -map 0:v -map 1:a -map 1:s?
               -c:v copy -c:a libopus -b:a 192k -ac 2 -c:s copy
               output.mkv
    """
    cmd = [
        "ffmpeg", "-y",
        "-i", hevc_path,
        "-i", source_path,
        "-map", "0:v",
        "-map", "1:a",
        "-map", "1:s?",
        "-map", "1:t?",
        "-c:v", "copy",
        "-c:a", "libopus",
        "-b:a", "192k",
        "-ac", "2",
        "-c:s", "copy",
        # Metadata
        "-metadata", f"title={Path(output_path).stem}",
        "-metadata", "artist=AniDL",
        "-metadata", "album=AniDL Encodes",
        "-metadata", "comment=Visit our site AniDL.org for more encodes",
        "-metadata", "copyright=AniDL Encodes",
        "-metadata", "encoder=Diablo",
        "-metadata", "encoding_tool=AniDL",
        "-metadata", "encoded_by=Diablo",
        "-progress", "pipe:1",
        "-nostats",
        output_path,
    ]
    log.info("[VS] Mux command: %s", " ".join(cmd))

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        log.error("[VS] FFmpeg mux failed (code %d): %s",
                  proc.returncode, stderr.decode(errors="replace")[-500:])
        return False

    return True


# ── Helper ────────────────────────────────────────────────────────────────────

async def _maybe_await(func, *args):
    result = func(*args)
    if asyncio.iscoroutine(result):
        await result


# ── Frame count via ffprobe ───────────────────────────────────────────────────

async def get_frame_count(file_path: str) -> Optional[int]:
    """Return total frame count of a video using ffprobe."""
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-count_packets",
        "-show_entries", "stream=nb_read_packets",
        "-of", "default=noprint_wrappers=1:nokey=1",
        f"file:{file_path}",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        raw = stdout.decode().strip()
        if raw.isdigit():
            return int(raw)
    except Exception as exc:
        log.warning("[VS] ffprobe frame count failed: %s", exc)
    return None
