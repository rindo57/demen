"""
Torrent input handlers – detect magnet links, .torrent files, and torrent URLs.
"""

import logging
import re

from pyrogram import Client, enums, filters
from pyrogram.types import Message

from config import ALLOWED_USERS
from core.torrent_manager import TorrentSession
from handlers.callbacks import (
    build_file_selection_keyboard,
    torrent_sessions,
    user_selections,
)
from utils.helpers import format_size, is_video_file

log = logging.getLogger(__name__)

MAGNET_REGEX = re.compile(r"magnet:\?xt=urn:btih:[a-zA-Z0-9]+", re.IGNORECASE)
TORRENT_URL_REGEX = re.compile(r"https?://\S+\.torrent", re.IGNORECASE)


def _is_allowed(user_id: int) -> bool:
    if not ALLOWED_USERS:
        return True
    return user_id in ALLOWED_USERS


async def _handle_torrent_session(
    client: Client,
    message: Message,
    ts: TorrentSession,
) -> None:
    """Common flow after a TorrentSession is created – fetch metadata & show files."""
    uid = message.from_user.id
    status_msg = await message.reply("⏳ Fetching torrent metadata…")

    ok = await ts.wait_for_metadata(timeout=120)
    if not ok:
        await status_msg.edit_text("❌ Timed out waiting for torrent metadata.")
        await ts.cleanup()
        return

    if not ts.files:
        await status_msg.edit_text("❌ No files found in this torrent.")
        await ts.cleanup()
        return

    # Store session and pre-select all video files
    torrent_sessions[ts.session_id] = ts
    selected = set()
    for f in ts.files:
        if is_video_file(f.name):
            selected.add(f.index)
    user_selections[(uid, ts.session_id)] = selected

    # Build file list text
    total_files = len(ts.files)
    video_count = sum(1 for f in ts.files if is_video_file(f.name))
    text = (
        f"📦 <b>Found {total_files} file(s) in torrent</b> "
        f"({video_count} video).\n"
        f"Select files to encode:\n"
    )

    keyboard = build_file_selection_keyboard(ts, selected, page=0)
    await status_msg.edit_text(text, reply_markup=keyboard, parse_mode=enums.ParseMode.HTML)


# ── Magnet Link Handler ──────────────────────────────────────

def register_magnet_handler(app: Client):
    @app.on_message(filters.private & filters.text & ~filters.command(["start", "help", "set", "settings", "cancel"]))
    async def _on_text(client: Client, message: Message):
        if not _is_allowed(message.from_user.id):
            return

        uid = message.from_user.id
        text = message.text.strip()

        # ── FFmpeg flags for pending /set resolution ──────────
        # Import here to avoid circular imports at module load time
        from handlers.commands import (
            RESOLUTION_LABELS,
            _apply_overrides,
            pending_set_resolution,
        )
        from utils.ffmpeg_args import parse_set_command

        resolution = pending_set_resolution.get(uid)
        if resolution and not resolution.startswith("__args__:"):
            # User is in the middle of a /set flow – consume this message as FFmpeg flags
            if not text.startswith("-"):
                await message.reply(
                    "⚠️ Expected FFmpeg flags (starting with <code>-</code>), e.g. "
                    "<code>-crf 18 -preset slow</code>.\n\n"
                    "Send /cancel to abort the current /set.",
                    parse_mode="html",
                )
                return

            overrides = parse_set_command(text)
            if not overrides:
                await message.reply(
                    "⚠️ Could not parse any valid FFmpeg flags from your input.\n"
                    "Use /help to see supported flags.",
                    parse_mode="html",
                )
                return

            pending_set_resolution.pop(uid, None)
            await _apply_overrides(uid, resolution, overrides)

            applied = ", ".join(f"<code>{k}={v}</code>" for k, v in overrides.items())
            res_label = RESOLUTION_LABELS.get(resolution, resolution)
            await message.reply(
                f"✅ <b>Settings updated for {res_label}!</b>\n\n"
                f"Applied: {applied}\n\n"
                "Use /settings to see full configuration.",
                parse_mode="html",
            )
            return
        # ─────────────────────────────────────────────────────

        # Check for magnet link
        magnet_match = MAGNET_REGEX.search(text)
        if magnet_match:
            magnet_uri = magnet_match.group(0)
            ts = TorrentSession()
            try:
                await ts.add_magnet(magnet_uri)
            except Exception as exc:
                await message.reply(f"❌ Invalid magnet link: {exc}")
                return
            await _handle_torrent_session(client, message, ts)
            return

        # Check for torrent URL
        url_match = TORRENT_URL_REGEX.search(text)
        if url_match:
            url = url_match.group(0)
            ts = TorrentSession()
            try:
                torrent_path = await ts.add_torrent_from_url(url)
                await ts.add_torrent_file(torrent_path)
            except Exception as exc:
                await message.reply(f"❌ Failed to download torrent file: {exc}")
                return
            await _handle_torrent_session(client, message, ts)
            return


# ── .torrent File Handler ─────────────────────────────────────

def register_torrent_file_handler(app: Client):
    @app.on_message(filters.private & filters.document)
    async def _on_document(client: Client, message: Message):
        if not _is_allowed(message.from_user.id):
            return

        doc = message.document
        if not doc.file_name or not doc.file_name.lower().endswith(".torrent"):
            return

        status_msg = await message.reply("⬇️ Downloading torrent file…")
        path = await message.download()

        ts = TorrentSession()
        try:
            await ts.add_torrent_file(path)
        except Exception as exc:
            await status_msg.edit_text(f"❌ Invalid torrent file: {exc}")
            return

        await _handle_torrent_session(client, message, ts)


# ── Registration ──────────────────────────────────────────────

def register_all_torrent_handlers(app: Client):
    register_magnet_handler(app)
    register_torrent_file_handler(app)
