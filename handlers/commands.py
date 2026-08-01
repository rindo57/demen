"""
Command handlers: /start, /help, /set, /settings
"""

import logging

from pyrogram import Client, filters, enums
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import ALLOWED_USERS, DEFAULT_ENCODE_SETTINGS, RESOLUTION_PRESETS
from utils.ffmpeg_args import (
    format_settings_display,
    merge_settings,
    parse_set_command,
)

log = logging.getLogger(__name__)

# ── Per-user settings store (in-memory) ───────────────────────
# Global default overrides (used when no per-resolution override exists)
user_settings: dict[int, dict[str, str]] = {}

# Per-resolution overrides: user_id → { resolution → settings_dict }
# e.g.  user_res_settings[12345]["720p"] = {"crf": "20", ...}
user_res_settings: dict[int, dict[str, dict[str, str]]] = {}

# Pending /set state: which resolution the user wants to configure next
# user_id → resolution label  (or "all" for global override)
pending_set_resolution: dict[int, str] = {}

# Human-readable labels for resolution buttons
RESOLUTION_LABELS: dict[str, str] = {
    "480p":   "📺 480p",
    "720p":   "🖥 720p",
    "1080p":  "🖥 1080p",
    "source": "🎯 Source (original res)",
    "all":    "🌐 All resolutions (global)",
}


def get_user_settings(user_id: int, resolution: str | None = None) -> dict[str, str]:
    """
    Return the effective encoding settings for *user_id*.

    If *resolution* is given, per-resolution overrides are merged on top
    of the user's global overrides (or defaults).
    """
    # Start with defaults
    if resolution and resolution in RESOLUTION_PRESETS:
        base = RESOLUTION_PRESETS[resolution].copy()
    else:
        base = DEFAULT_ENCODE_SETTINGS.copy()

    # Apply global user overrides
    global_overrides = user_settings.get(user_id, {})
    merged = {**base, **global_overrides}

    # Apply per-resolution overrides on top
    if resolution:
        res_overrides = user_res_settings.get(user_id, {}).get(resolution, {})
        merged.update(res_overrides)

    return merged


def _is_allowed(user_id: int) -> bool:
    """Check whether the user is in the allow-list (if one is configured)."""
    if not ALLOWED_USERS:
        return True
    return user_id in ALLOWED_USERS


def _build_set_resolution_keyboard() -> InlineKeyboardMarkup:
    """Keyboard to pick which resolution to configure with /set."""
    rows = []
    for res, label in RESOLUTION_LABELS.items():
        rows.append([
            InlineKeyboardButton(label, callback_data=f"setres:{res}")
        ])
    rows.append([
        InlineKeyboardButton("❌ Cancel", callback_data="setres:cancel")
    ])
    return InlineKeyboardMarkup(rows)


# ── /start ────────────────────────────────────────────────────

def register_start(app: Client):
    @app.on_message(filters.command("start") & filters.private)
    async def _start(client: Client, message: Message):
        if not _is_allowed(message.from_user.id):
            await message.reply("⛔ You are not authorised to use this bot.")
            return

        await message.reply(
            "<b>🎬 AniDL Video Compressor Bot</b>\n"
            "\n"
            "Send me a <b>magnet link</b>, a <b>.torrent file</b>, or a "
            "<b>direct torrent URL</b> and I will:\n"
            "\n"
            "1️⃣ Fetch the torrent metadata\n"
            "2️⃣ Let you pick which files to encode\n"
            "3️⃣ Compress videos with x265 10-Bit\n"
            "4️⃣ Upload the encoded files as documents\n"
            "\n"
            "<b>Commands:</b>\n"
            "/set — override encoding settings for a specific resolution\n"
            "/settings — show current settings\n"
            "/help — detailed usage guide\n",
        )


# ── /help ─────────────────────────────────────────────────────

def register_help(app: Client):
    @app.on_message(filters.command("help") & filters.private)
    async def _help(client: Client, message: Message):
        if not _is_allowed(message.from_user.id):
            return

        await message.reply(
            "<b>📖 Usage Guide</b>\n"
            "\n"
            "<b>1. Send a torrent</b>\n"
            "• Paste a <code>magnet:?xt=urn:btih:…</code> link\n"
            "• Upload a <code>.torrent</code> file\n"
            "• Send a direct torrent URL (e.g. nyaa.si download link)\n"
            "\n"
            "<b>2. Select files</b>\n"
            "Use the inline buttons to toggle files, then press <b>✅ Confirm</b>.\n"
            "\n"
            "<b>3. Choose resolutions</b>\n"
            "Pick one or more of: 480p, 720p, 1080p, or <b>Source</b> (original resolution).\n"
            "\n"
            "<b>4. Custom settings per-resolution</b>\n"
            "Use <code>/set</code> — the bot will first ask which resolution you want to configure.\n"
            "Then send your FFmpeg overrides, e.g.:\n"
            "<code>-crf 18 -preset slow</code>\n"
            "\n"
            "Supported flags: <code>-vf</code>, <code>-crf</code>, "
            "<code>-preset</code>, <code>-c:v</code>, <code>-c:a</code>, "
            "<code>-b:a</code>, <code>-ac</code>, <code>-r</code>, "
            "<code>-pix_fmt</code>, <code>-x265-params</code>, <code>-c:s</code>\n"
            "\n"
            "Use <code>/settings</code> to view your current configuration per resolution.",
        )


# ── /set  (Step 1 – pick resolution) ─────────────────────────

def register_set(app: Client):
    @app.on_message(filters.command("set") & filters.private)
    async def _set(client: Client, message: Message):
        uid = message.from_user.id
        if not _is_allowed(uid):
            return

        # If extra args are passed along with /set, store them for after resolution selection
        # e.g. /set -crf 18  → still ask resolution first
        raw = message.text.split(maxsplit=1)
        if len(raw) >= 2 and raw[1].strip():
            # Cache the raw FFmpeg args so we can apply after resolution is selected
            pending_set_resolution[uid] = f"__args__:{raw[1].strip()}"

        await message.reply(
            "⚙️ <b>Which resolution do you want to change settings for?</b>\n\n"
            "<i>Select a resolution below, then send your FFmpeg flags "
            "(e.g. <code>-crf 18 -preset slow</code>).</i>\n\n"
            "Choose <b>All resolutions</b> to apply a global override to all presets.",
            # parse_mode="html",
            reply_markup=_build_set_resolution_keyboard(),
        )

    # ── Step 2 – resolution selected ────────────────────────────
    @app.on_callback_query(filters.regex(r"^setres:"))
    async def _setres_callback(client: Client, cq: CallbackQuery):
        uid = cq.from_user.id
        if not _is_allowed(uid):
            await cq.answer("⛔ Not authorised", show_alert=True)
            return

        _, resolution = cq.data.split(":", 1)

        if resolution == "cancel":
            pending_set_resolution.pop(uid, None)
            try:
                await cq.edit_message_text("❌ /set cancelled.")
            except Exception:
                pass
            await cq.answer()
            return

        # Check if args were already passed with /set command
        pending = pending_set_resolution.get(uid, "")
        if pending.startswith("__args__:"):
            ffmpeg_args = pending[len("__args__:"):]
            pending_set_resolution.pop(uid, None)

            overrides = parse_set_command(ffmpeg_args)
            if not overrides:
                try:
                    await cq.edit_message_text(
                        "⚠️ Could not parse any valid FFmpeg flags.\n"
                        "Use /help to see supported flags."
                    )
                except Exception:
                    pass
                await cq.answer()
                return

            await _apply_overrides(uid, resolution, overrides)

            applied = ", ".join(f"<code>{k}={v}</code>" for k, v in overrides.items())
            res_label = RESOLUTION_LABELS.get(resolution, resolution)
            try:
                await cq.edit_message_text(
                    f"✅ <b>Settings updated for {res_label}!</b>\n\n"
                    f"Applied: {applied}\n\n"
                    "Use /settings to see full configuration.",
                    # parse_mode="html",
                )
            except Exception:
                pass
            await cq.answer("Settings saved!")
            return

        # No args yet – store chosen resolution and ask for FFmpeg flags
        pending_set_resolution[uid] = resolution
        res_label = RESOLUTION_LABELS.get(resolution, resolution)
        try:
            await cq.edit_message_text(
                f"✏️ <b>Configuring settings for: {res_label}</b>\n\n"
                "Now send your FFmpeg flags as a plain message, e.g.:\n"
                "<code>-crf 18 -preset slow</code>\n\n"
                "<i>Send /cancel to abort.</i>",
                # parse_mode="html",
            )
        except Exception:
            pass
        await cq.answer()

    # ── /cancel ───────────────────────────────────────────────
    @app.on_message(filters.command("cancel") & filters.private)
    async def _cancel(client: Client, message: Message):
        uid = message.from_user.id
        if pending_set_resolution.pop(uid, None):
            await message.reply("❌ /set cancelled.")
        else:
            await message.reply("Nothing to cancel.")



async def _apply_overrides(uid: int, resolution: str, overrides: dict[str, str]):
    """Apply *overrides* to the correct settings store for *uid* and *resolution*."""
    if resolution == "all":
        # Global override – applies to all resolutions
        base = user_settings.get(uid, DEFAULT_ENCODE_SETTINGS.copy())
        user_settings[uid] = merge_settings(base, overrides)
    else:
        # Per-resolution override
        if uid not in user_res_settings:
            user_res_settings[uid] = {}
        base = user_res_settings[uid].get(
            resolution, RESOLUTION_PRESETS.get(resolution, DEFAULT_ENCODE_SETTINGS).copy()
        )
        user_res_settings[uid][resolution] = merge_settings(base, overrides)


# ── /settings ─────────────────────────────────────────────────

def register_settings(app: Client):
    @app.on_message(filters.command("settings") & filters.private)
    async def _settings(client: Client, message: Message):
        uid = message.from_user.id
        if not _is_allowed(uid):
            return

        lines: list[str] = ["<b>⚙️ Encoding Settings Overview</b>\n"]

        has_custom = uid in user_settings or uid in user_res_settings

        for res in ("480p", "720p", "1080p", "source"):
            s = get_user_settings(uid, res)
            res_label = RESOLUTION_LABELS.get(res, res)
            has_res_override = uid in user_res_settings and res in user_res_settings[uid]
            marker = " ✏️" if has_res_override else ""
            lines.append(f"<b>── {res_label}{marker} ──</b>")
            lines.append(format_settings_display(s))

        if has_custom:
            lines.append("\n<i>ℹ️ You have custom overrides active. Use /set to change per-resolution settings.</i>")
        else:
            lines.append("\n<i>ℹ️ Using default settings for all resolutions.</i>")

        # Telegram has a 4096-char message limit; send as two parts if needed
        full = "\n".join(lines)
        if len(full) > 4000:
            half = len(lines) // 2
            await message.reply("\n".join(lines[:half]))
            await message.reply("\n".join(lines[half:]))
        else:
            await message.reply(full)


# ── Registration shortcut ─────────────────────────────────────

def register_all_commands(app: Client):
    register_start(app)
    register_help(app)
    register_set(app)
    register_settings(app)
