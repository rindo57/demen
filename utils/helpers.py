"""
Utility helpers: size formatting, duration formatting, disk checks, etc.
"""

import shutil
import zlib
from pathlib import Path

VIDEO_EXTENSIONS = {
    ".mkv", ".mp4", ".avi", ".mov", ".wmv", ".flv",
    ".webm", ".m4v", ".ts", ".mpg", ".mpeg", ".3gp",
}


def compute_crc32(filepath: str) -> str:
    """
    Compute the CRC32 checksum of a file.

    Returns an 8-character uppercase hex string, e.g. ``'1C30AA46'``.
    """
    crc = 0
    with open(filepath, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            crc = zlib.crc32(chunk, crc)
    return f"{crc & 0xFFFFFFFF:08X}"


def format_size(size_bytes: int) -> str:
    """Format bytes to human-readable size (e.g. '245.3 MB')."""
    if size_bytes <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    idx = 0
    size = float(size_bytes)
    while size >= 1024 and idx < len(units) - 1:
        size /= 1024
        idx += 1
    return f"{int(size)} B" if idx == 0 else f"{size:.1f} {units[idx]}"


def format_duration(seconds: float) -> str:
    """Format seconds to '4m 32s' style."""
    if seconds < 0:
        return "0s"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m {s}s"


def is_video_file(filename: str) -> bool:
    """Check if a filename has a video extension."""
    return Path(filename).suffix.lower() in VIDEO_EXTENSIONS


def get_disk_free_space(path: str = ".") -> int:
    """Get free disk space in bytes for the partition containing *path*."""
    _, _, free = shutil.disk_usage(path)
    return free


def check_disk_space(required_bytes: int, path: str = ".") -> bool:
    """Return True if there is enough free disk space."""
    return get_disk_free_space(path) >= required_bytes


def sanitize_filename(filename: str) -> str:
    """Remove characters that are invalid in Windows/Linux filenames."""
    for ch in '<>:"/\\|?*':
        filename = filename.replace(ch, "")
    return filename.strip()
