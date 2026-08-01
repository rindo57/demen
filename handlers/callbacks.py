"""
Inline keyboard callback handlers for file selection, resolution selection,
pagination, and confirmation.
"""

import asyncio
import logging
import os
import shutil
import time
from pathlib import Path

from pyrogram import Client, filters
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from config import ALLOWED_USERS, FILES_PER_PAGE, RESOLUTION_PRESETS, TEMP_DIR
from core.encoder import EncodeProgress, build_caption, check_output_size, encode_video
from core.filename_builder import build_output_filename
from core.queue_manager import Job, JobStatus, queue_manager
from core.torrent_manager import TorrentSession
from handlers.commands import get_user_settings
from utils.helpers import format_size, is_video_file

log = logging.getLogger(__name__)

# ── State stores ──────────────────────────────────────────────
torrent_sessions: dict[str, TorrentSession] = {}           # session_id → TorrentSession
user_selections: dict[tuple[int, str], set[int]] = {}      # (user_id, session_id) → selected file indices
user_resolution_selections: dict[tuple[int, str], set[str]] = {}  # (user_id, session_id) → selected resolutions

# Sorted resolution order for encoding
RESOLUTION_ORDER = ["480p", "720p", "1080p", "source"]


# ── File Selection Keyboard ──────────────────────────────────

def build_file_selection_keyboard(
    ts: TorrentSession,
    selected: set[int],
    page: int = 0,
) -> InlineKeyboardMarkup:
    """Build an inline keyboard with file toggles + control buttons."""
    files = ts.files
    total_pages = max(1, (len(files) + FILES_PER_PAGE - 1) // FILES_PER_PAGE)
    page = min(page, total_pages - 1)
    start = page * FILES_PER_PAGE
    end = min(start + FILES_PER_PAGE, len(files))

    rows: list[list[InlineKeyboardButton]] = []

    for f in files[start:end]:
        check = "✅" if f.index in selected else "⬜"
        label = f"{check} {f.name} ({format_size(f.size)})"
        # Truncate long labels to fit Telegram's limits
        if len(label) > 60:
            label = label[:57] + "…"
        rows.append([
            InlineKeyboardButton(
                label,
                callback_data=f"tf:{ts.session_id}:{f.index}",
            )
        ])

    # Navigation row
    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(
            "⬅️ Prev", callback_data=f"pg:{ts.session_id}:{page - 1}"
        ))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton(
            "Next ➡️", callback_data=f"pg:{ts.session_id}:{page + 1}"
        ))
    if nav_row:
        rows.append(nav_row)

    # Control row
    rows.append([
        InlineKeyboardButton("☑️ Select All", callback_data=f"sa:{ts.session_id}"),
        InlineKeyboardButton("🔲 Deselect All", callback_data=f"da:{ts.session_id}"),
    ])
    rows.append([
        InlineKeyboardButton("✅ Confirm", callback_data=f"cf:{ts.session_id}"),
    ])

    return InlineKeyboardMarkup(rows)


# ── Resolution Selection Keyboard ────────────────────────────

def build_resolution_keyboard(
    session_id: str,
    selected_resolutions: set[str],
) -> InlineKeyboardMarkup:
    """Build an inline keyboard for resolution selection (480p / 720p / 1080p / source)."""
    rows: list[list[InlineKeyboardButton]] = []

    labels = {
        "480p": "480p",
        "720p": "720p",
        "1080p": "1080p",
        "source": "🎯 Source (original res)",
    }

    for res in RESOLUTION_ORDER:
        check = "✅" if res in selected_resolutions else "⬜"
        rows.append([
            InlineKeyboardButton(
                f"{check} {labels.get(res, res)}",
                callback_data=f"rs:{session_id}:{res}",
            )
        ])

    # Control row
    rows.append([
        InlineKeyboardButton("☑️ Select All", callback_data=f"ra:{session_id}"),
        InlineKeyboardButton("🔲 Deselect All", callback_data=f"rd:{session_id}"),
    ])
    rows.append([
        InlineKeyboardButton("✅ Start Encoding", callback_data=f"rc:{session_id}"),
    ])

    return InlineKeyboardMarkup(rows)


def _get_page_from_message(callback_data_prefix: str, ts: TorrentSession, current_data: str) -> int:
    """Try to recover which page the user is on from the callback data."""
    return 0


# ── Callback Registration ────────────────────────────────────

def register_all_callbacks(app: Client):

    # ── File Selection Callbacks ─────────────────────────────

    @app.on_callback_query(filters.regex(r"^tf:"))
    async def _toggle_file(client: Client, cq: CallbackQuery):
        """Toggle a single file's selection."""
        parts = cq.data.split(":")
        if len(parts) != 3:
            await cq.answer("❌ Invalid data")
            return

        _, session_id, file_idx_str = parts
        file_idx = int(file_idx_str)
        uid = cq.from_user.id
        key = (uid, session_id)

        if session_id not in torrent_sessions:
            await cq.answer("⚠️ Session expired")
            return

        ts = torrent_sessions[session_id]
        selected = user_selections.get(key, set())

        if file_idx in selected:
            selected.discard(file_idx)
        else:
            selected.add(file_idx)
        user_selections[key] = selected

        # Figure out current page from the file index
        page = file_idx // FILES_PER_PAGE
        keyboard = build_file_selection_keyboard(ts, selected, page)

        try:
            await cq.edit_message_reply_markup(reply_markup=keyboard)
        except Exception:
            pass
        await cq.answer()

    @app.on_callback_query(filters.regex(r"^sa:"))
    async def _select_all(client: Client, cq: CallbackQuery):
        _, session_id = cq.data.split(":", 1)
        uid = cq.from_user.id
        key = (uid, session_id)

        if session_id not in torrent_sessions:
            await cq.answer("⚠️ Session expired")
            return

        ts = torrent_sessions[session_id]
        selected = {f.index for f in ts.files}
        user_selections[key] = selected

        keyboard = build_file_selection_keyboard(ts, selected, 0)
        try:
            await cq.edit_message_reply_markup(reply_markup=keyboard)
        except Exception:
            pass
        await cq.answer("All files selected")

    @app.on_callback_query(filters.regex(r"^da:"))
    async def _deselect_all(client: Client, cq: CallbackQuery):
        _, session_id = cq.data.split(":", 1)
        uid = cq.from_user.id
        key = (uid, session_id)

        if session_id not in torrent_sessions:
            await cq.answer("⚠️ Session expired")
            return

        ts = torrent_sessions[session_id]
        user_selections[key] = set()

        keyboard = build_file_selection_keyboard(ts, set(), 0)
        try:
            await cq.edit_message_reply_markup(reply_markup=keyboard)
        except Exception:
            pass
        await cq.answer("All files deselected")

    @app.on_callback_query(filters.regex(r"^pg:"))
    async def _paginate(client: Client, cq: CallbackQuery):
        parts = cq.data.split(":")
        if len(parts) != 3:
            await cq.answer()
            return

        _, session_id, page_str = parts
        page = int(page_str)
        uid = cq.from_user.id
        key = (uid, session_id)

        if session_id not in torrent_sessions:
            await cq.answer("⚠️ Session expired")
            return

        ts = torrent_sessions[session_id]
        selected = user_selections.get(key, set())
        keyboard = build_file_selection_keyboard(ts, selected, page)

        try:
            await cq.edit_message_reply_markup(reply_markup=keyboard)
        except Exception:
            pass
        await cq.answer()

    # ── File Confirm → Show Resolution Picker ────────────────

    @app.on_callback_query(filters.regex(r"^cf:"))
    async def _confirm_files(client: Client, cq: CallbackQuery):
        """User confirmed file selection – show resolution selection keyboard."""
        _, session_id = cq.data.split(":", 1)
        uid = cq.from_user.id
        key = (uid, session_id)

        if session_id not in torrent_sessions:
            await cq.answer("⚠️ Session expired")
            return

        ts = torrent_sessions[session_id]
        selected = user_selections.get(key, set())

        if not selected:
            await cq.answer("⚠️ No files selected!", show_alert=True)
            return

        # Check for video files
        video_indices = {
            idx for idx in selected if is_video_file(ts.files[idx].name)
        }
        if not video_indices:
            await cq.answer("⚠️ No video files in your selection!", show_alert=True)
            return

        await cq.answer()

        # Remove the file selection keyboard
        try:
            await cq.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

        # Pre-select 720p as default
        user_resolution_selections[key] = {"720p"}

        # Show resolution selection keyboard
        res_keyboard = build_resolution_keyboard(session_id, {"720p"})
        total = len(video_indices)
        await cq.message.reply(
            f"📐 <b>{total} video file(s) selected.</b>\n"
            f"Choose encoding resolution(s):\n\n"
            f"<i>Files will be encoded in order: 480p → 720p → 1080p → Source</i>\n"
            f"<i>📌 'Source' keeps the original resolution without downscaling.</i>",
            reply_markup=res_keyboard,
        )

    # ── Resolution Selection Callbacks ───────────────────────

    @app.on_callback_query(filters.regex(r"^rs:"))
    async def _toggle_resolution(client: Client, cq: CallbackQuery):
        """Toggle a single resolution."""
        parts = cq.data.split(":")
        if len(parts) != 3:
            await cq.answer("❌ Invalid data")
            return

        _, session_id, resolution = parts
        uid = cq.from_user.id
        key = (uid, session_id)

        if session_id not in torrent_sessions:
            await cq.answer("⚠️ Session expired")
            return

        selected_res = user_resolution_selections.get(key, set())

        if resolution in selected_res:
            selected_res.discard(resolution)
        else:
            selected_res.add(resolution)
        user_resolution_selections[key] = selected_res

        keyboard = build_resolution_keyboard(session_id, selected_res)
        try:
            await cq.edit_message_reply_markup(reply_markup=keyboard)
        except Exception:
            pass
        await cq.answer()

    @app.on_callback_query(filters.regex(r"^ra:"))
    async def _select_all_resolutions(client: Client, cq: CallbackQuery):
        """Select all resolutions."""
        _, session_id = cq.data.split(":", 1)
        uid = cq.from_user.id
        key = (uid, session_id)

        if session_id not in torrent_sessions:
            await cq.answer("⚠️ Session expired")
            return

        selected_res = set(RESOLUTION_ORDER)
        user_resolution_selections[key] = selected_res

        keyboard = build_resolution_keyboard(session_id, selected_res)
        try:
            await cq.edit_message_reply_markup(reply_markup=keyboard)
        except Exception:
            pass
        await cq.answer("All resolutions selected")

    @app.on_callback_query(filters.regex(r"^rd:"))
    async def _deselect_all_resolutions(client: Client, cq: CallbackQuery):
        """Deselect all resolutions."""
        _, session_id = cq.data.split(":", 1)
        uid = cq.from_user.id
        key = (uid, session_id)

        if session_id not in torrent_sessions:
            await cq.answer("⚠️ Session expired")
            return

        user_resolution_selections[key] = set()

        keyboard = build_resolution_keyboard(session_id, set())
        try:
            await cq.edit_message_reply_markup(reply_markup=keyboard)
        except Exception:
            pass
        await cq.answer("All resolutions deselected")

    # ── Resolution Confirm → Start Download & Encode ─────────

    @app.on_callback_query(filters.regex(r"^rc:"))
    async def _confirm_resolutions(client: Client, cq: CallbackQuery):
        """User confirmed resolution selection – start download & encode pipeline."""
        _, session_id = cq.data.split(":", 1)
        uid = cq.from_user.id
        chat_id = cq.message.chat.id
        key = (uid, session_id)

        if session_id not in torrent_sessions:
            await cq.answer("⚠️ Session expired")
            return

        ts = torrent_sessions[session_id]
        selected_files = user_selections.get(key, set())
        selected_resolutions = user_resolution_selections.get(key, set())

        if not selected_resolutions:
            await cq.answer("⚠️ No resolutions selected!", show_alert=True)
            return

        await cq.answer("Starting…")

        # Remove the resolution keyboard
        try:
            await cq.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

        # Sort resolutions in encoding order: 480p → 720p → 1080p → source
        sorted_resolutions = [r for r in RESOLUTION_ORDER if r in selected_resolutions]

        # Count selected videos
        video_indices = {
            idx for idx in selected_files if is_video_file(ts.files[idx].name)
        }
        if not video_indices:
            await cq.message.reply("⚠️ No video files in your selection.")
            return

        total_files = len(video_indices)
        res_list = ", ".join(sorted_resolutions)
        total_jobs = total_files * len(sorted_resolutions)
        await cq.message.reply(
            f"⬇️ <b>Downloading {total_files} file(s)…</b>\n"
            f"📐 Resolutions: <b>{res_list}</b>\n"
            f"📝 Total encode jobs: <b>{total_jobs}</b>",
        )

        # Set priorities and download
        await ts.set_file_priorities(selected_files)

        # Create a progress message for download
        dl_msg = await cq.message.reply("⬇️ Download: 0%")
        last_dl_update = [0.0]
        # Capture the running loop *before* entering the worker thread
        loop = asyncio.get_running_loop()

        def dl_progress(pct, rate, peers):
            nonlocal last_dl_update
            now = time.monotonic()
            # Update every 15 seconds to prevent flood wait
            if now - last_dl_update[0] >= 15:
                last_dl_update[0] = now
                asyncio.run_coroutine_threadsafe(
                    _safe_edit(
                        dl_msg,
                        f"⬇️ Download: {pct:.1f}% | "
                        f"Speed: {format_size(int(rate))}/s | "
                        f"Peers: {peers}",
                    ),
                    loop,
                )

        try:
            await ts.download(progress_callback=dl_progress)
        except Exception as exc:
            await cq.message.reply(f"❌ Download failed: {exc}")
            await ts.cleanup()
            torrent_sessions.pop(session_id, None)
            user_selections.pop(key, None)
            user_resolution_selections.pop(key, None)
            return

        await _safe_edit(dl_msg, "✅ Download complete!")

        # Get downloaded paths
        downloaded = ts.get_downloaded_file_paths(selected_files)
        video_paths = [p for p in downloaded if is_video_file(p.name)]

        # Enqueue encoding jobs: all files per resolution, in resolution order
        # 480p (all files) → 720p (all files) → 1080p (all files) → source (all files)
        for res in sorted_resolutions:
            # Pull per-resolution settings (includes user overrides)
            res_settings = get_user_settings(uid, res)
            for i, vp in enumerate(video_paths):
                job = Job(
                    user_id=uid,
                    chat_id=chat_id,
                    file_index=i,
                    file_name=vp.name,
                    file_path=vp,
                    settings=res_settings,
                    resolution=res,
                )
                await queue_manager.enqueue(job)

        remaining = queue_manager.queue_size(uid)
        if remaining > 0:
            await cq.message.reply(
                f"📝 <b>{total_jobs} encode job(s) queued.</b>\n"
                f"📐 Resolution order: {res_list}",
            )

        # Initialise batch tracker for each resolution so the bot can
        # send a consolidated summary once all files for a resolution
        # are done.
        from bot import init_batch_tracker
        for res in sorted_resolutions:
            init_batch_tracker(uid, res, total_files, chat_id)

        # Cleanup torrent session state (files are on disk)
        torrent_sessions.pop(session_id, None)
        user_selections.pop(key, None)
        user_resolution_selections.pop(key, None)


async def _safe_edit(msg, text: str):
    """Edit message text, swallowing any Telegram errors."""
    try:
        await msg.edit_text(text)
    except Exception:
        pass
