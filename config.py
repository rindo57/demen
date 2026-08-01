"""
Configuration for the Telegram Video Compressor Bot.
Loads environment variables and defines default encoding settings.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ── Telegram API ──────────────────────────────────────────────
API_ID = int(os.getenv("API_ID", "10247139"))  # Replace with your API ID
API_HASH = os.getenv("API_HASH", "96b46175824223a33737657ab943fd6a")  # Replace with your API Hash
BOT_TOKEN = os.getenv("BOT_TOKEN", "8911032602:AAE7kkM5rlVPzJqZZs7KxEPnwiYUlpB_rx4")  # Replace with your Bot Token
# 8953086807:AAGXLffXX_GTBCCLnimNfcQpTlaSNfKRNyA
# 8618736064:AAHWok7FYaerlqbNcYN-4POD34iwKCrSkS4 bot 1
# ── BuzzHeavier Fallback (for >2 GB files without Premium session) ──
# Used automatically when USER_SESSION_STRING is empty and the file exceeds 2 GB.
# Get your account ID from https://buzzheavier.com/api/account after signing in.
# Leave empty to upload anonymously (file still works, but not tied to your account).
BUZZHEAVIER_API_KEY = os.getenv("BUZZHEAVIER_API_KEY", "")

# ── Access Control ────────────────────────────────────────────
_allowed = os.getenv("ALLOWED_USERS", "1498366357")
ALLOWED_USERS: list[int] = [
    int(x.strip()) for x in _allowed.split(",") if x.strip()
]

# ── Directories ───────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", str(BASE_DIR / "downloads")))
TEMP_DIR = Path(os.getenv("TEMP_DIR", str(BASE_DIR / "temp")))

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
TEMP_DIR.mkdir(parents=True, exist_ok=True)

# ── Channel Upload ────────────────────────────────────────────
UPLOAD_CHANNEL_ID = int(os.getenv("UPLOAD_CHANNEL_ID", "-1001431492334"))

# ── MongoDB (download-link storage) ──────────────────────────
MONGO_DL_URI = os.getenv(
    "MONGO_DL_URI",
    "mongodb+srv://blackhole:f6kVDHrOwPfWfMzs@cluster0.jlfrpbo.mongodb.net/?appName=Cluster0",
)
MONGO_DL_DB = os.getenv("MONGO_DL_DB", "void")
MONGO_DL_COLLECTION = os.getenv("MONGO_DL_COLLECTION", "files")

# ── Download Link ─────────────────────────────────────────────
DL_BASE_URL = os.getenv("DL_BASE_URL", "https://anidl.ddlserverv1.me.in/shark/")

# ── Upload to User Toggle ────────────────────────────────────
# When True the encoded file is also sent as a document to the user chat.
# When False only the download link is sent.
# ── Premium User Session (for uploads > 2 GB) ────────────────
# Generate with: python generate_session.py
# Then paste the result into your .env file as USER_SESSION_STRING=...
USER_SESSION_STRING = os.getenv("USER_SESSION_STRING", "BQCcW-MAg4nrNeEAwIUj3PMrSaoVtTLCtlU-SJ2SqyKpP_sj2Gk_hexc-Kjk_wGE9EVQX6T_UFgYo8OTMyBPOI3tX-HuhdF1NjnLf94-zuQYWUnoeLISFRXVvrsBJ3K_Ew-sjApMIjYtX2Zj8OwNXXBh4EDoWGxdGtghKxYTmEG16woRdep2jsbSmmcUjGqbwdwj1J1t7rm52JxrsFHsma82__knPVPQW45tgHTyO9ohyGx4QORiyFXPD7r7cD-6JqcSuwg9TWouoYxM6kzW_ictPqQOyfRYaNcnEzAie-E_9_dNfRP3gl63SkpWsFukBNR6P1ZD5dcxvynjlN9ANRzyqcUjAwAAAAFDAN9YAA")  # Leave empty to disable large-file uploads

UPLOAD_TO_USER = os.getenv("UPLOAD_TO_USER", "true").strip().lower() in (
    "1", "true", "yes",
)

# ── Limits & Constants ────────────────────────────────────────
# ── Limits & Constants ────────────────────────────────────────

BOT_UPLOAD_LIMIT = 2 * 1024 * 1024 * 1024  # 2 GB – bot token hard cap; above this needs user session

MAX_UPLOAD_SIZE = 4 * 1024 * 1024 * 1024  # 4 GB (Telegram Premium limit)
MAX_RETRIES = 3
RETRY_DELAY = 5            # seconds
PROGRESS_UPDATE_INTERVAL = 5  # seconds (avoid Telegram rate limits)
FILES_PER_PAGE = 8          # inline keyboard pagination

# ── Default Encoding Settings ─────────────────────────────────
DEFAULT_ENCODE_SETTINGS: dict[str, str] = {
    "pix_fmt":       "yuv420p10le",
    # "framerate" removed - will use source framerate
    "vf":            "scale=1280:720:flags=spline",
    "preset":        "medium",
    "video_codec":   "libx265",
    "crf":           "21",
    "x265_params":   (
        "aq-mode=3:deblock=-1,-1:limit-sao=0:bframes=6:frame-threads=5:psy-rd=1.5:psy-rdoq=1.2"
    ),
    "audio_codec":   "libopus",
    "audio_bitrate": "96k",
    "audio_channels": "2",
    "sub_codec":     "copy",
}

# ── Per-Resolution Encoding Presets ───────────────────────────
# NOTE: These settings are used ONLY when USE_VAPOURSYNTH=False in encoder.py.
# When VapourSynth is enabled, x265 params are defined in core/vs_filter.py.
_COMMON_X265_PARAMS = (
    # Deblocking — strong for anime (removes block artifacts in flat areas)
    "deblock=3,3:"
    # Disable SAO — causes ringing/halos on anime line art
    "sao=0:"
    # Motion estimation — best quality
    "subme=7:"
    "me=3:"              # STAR search
    "merange=57:"
    # Psychovisual — tuned LOW for clean anime (high values amplify grain)
    "psy-rd=0.75:"
    "psy-rdoq=0.30:"
    # Adaptive quantisation
    "aq-mode=3:"
    "aq-strength=0.70:"
    # Disable strong-intra-smoothing (causes blur on fine anime lines)
    "no-strong-intra-smoothing=1:"
    # Structure / reference frames
    "bframes=8:"
    "b-adapt=2:"
    "ref=6:"
    # Lookahead — maximum quality decisions
    "rc-lookahead=80:"
    "lookahead-slices=0:"
    # Partition modes — try everything
    "rect=1:amp=1:"
    # Threading — 24 vCores EPYC
    "frame-threads=2:"
    "pmode=1:pme=1:"
    "numa-pools=24:"
    "no-info=1"
)

RESOLUTION_PRESETS: dict[str, dict[str, str]] = {
    "480p": {
        "pix_fmt":        "yuv420p10le",
        "vf":             "scale=-2:480:flags=spline",
        "preset":         "slow",
        "video_codec":    "libx265",
        "crf":            "20",
        "x265_params":    _COMMON_X265_PARAMS,
        "audio_codec":    "libopus",
        "audio_bitrate":  "96k",
        "audio_channels": "2",
        "sub_codec":      "copy",
    },
    "720p": {
        "pix_fmt":        "yuv420p10le",
        "vf":             "scale=-2:720:flags=spline",
        "preset":         "slow",
        "video_codec":    "libx265",
        "crf":            "18",
        "x265_params":    _COMMON_X265_PARAMS,
        "audio_codec":    "libopus",
        "audio_bitrate":  "96k",
        "audio_channels": "2",
        "sub_codec":      "copy",
    },
    "1080p": {
        "pix_fmt":        "yuv420p10le",
        "vf":             "scale=-2:1080:flags=spline",
        "preset":         "slow",
        "video_codec":    "libx265",
        "crf":            "16",
        "x265_params":    _COMMON_X265_PARAMS,
        "audio_codec":    "libopus",
        "audio_bitrate":  "192k",
        "audio_channels": "2",
        "sub_codec":      "copy",
    },
    "source": {
        "pix_fmt":        "yuv420p10le",
        # No "vf" key → no rescaling, keeps original resolution
        "preset":         "veryslow",
        "video_codec":    "libx265",
        "crf":            "16",
        "x265_params":    _COMMON_X265_PARAMS,
        "audio_codec":    "libopus",
        "audio_bitrate":  "192k",
        "audio_channels": "2",
        "sub_codec":      "copy",
    },
}

# ── FFmpeg Flag → Setting Key Mapping ─────────────────────────
FFMPEG_FLAG_MAP: dict[str, str] = {
    "-pix_fmt":      "pix_fmt",
    "-r":            "framerate",  # Keep this mapping for custom overrides
    "-vf":           "vf",
    "-preset":       "preset",
    "-c:v":          "video_codec",
    "-crf":          "crf",
    "-x265-params":  "x265_params",
    "-c:a":          "audio_codec",
    "-b:a":          "audio_bitrate",
    "-ac":           "audio_channels",
    "-c:s":          "sub_codec",
}
