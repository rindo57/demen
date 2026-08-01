"""
AniDL Video Compressor Bot – entry point.

Initialises Pyrogram, registers handlers, wires the queue worker, and runs.
"""

import asyncio
import logging
import os
import shutil
import sys
import time
from pathlib import Path

from pyrogram import Client

from config import (
    API_HASH, API_ID, BOT_TOKEN,
    BOT_UPLOAD_LIMIT, BUZZHEAVIER_API_KEY,
    DL_BASE_URL, TEMP_DIR, UPLOAD_TO_USER, USER_SESSION_STRING,
)
from core.encoder import (
    EncodeProgress,
    build_caption,
    check_output_size,
    encode_video,
)
from core.filename_builder import build_output_filename
from core.queue_manager import Job, JobStatus, queue_manager
from core.uploader import _NeedsBuzzHeavier, upload_to_buzzheavier, upload_to_channel_and_save
from handlers.callbacks import register_all_callbacks
from handlers.commands import register_all_commands
from handlers.torrent_input import register_all_torrent_handlers
from utils.episode_parser import extract_episode_number
from utils.helpers import compute_crc32, format_duration, format_size

# ── Logging ───────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bot")


# ── Batch tracker ────────────────────────────────────────────
batch_tracker: dict[tuple[int, str], dict] = {}


def init_batch_tracker(user_id: int, resolution: str, total: int, chat_id: int):
    """Initialise a batch tracker slot for (user, resolution)."""
    key = (user_id, resolution)
    batch_tracker[key] = {
        "total": total,
        "completed": [],
        "chat_id": chat_id,
    }


async def record_and_maybe_summarise(
    app_: "Client",
    user_id: int,
    resolution: str,
    episode: int | None,
    filename: str,
    dl_link: str,
):
    key = (user_id, resolution)
    tracker = batch_tracker.get(key)
    if tracker is None:
        return

    tracker["completed"].append({
        "episode": episode,
        "filename": filename,
        "dl_link": dl_link,
    })

    if len(tracker["completed"]) < tracker["total"]:
        return

    chat_id = tracker["chat_id"]
    entries = tracker["completed"]
    entries.sort(key=lambda e: (e["episode"] is None, e["episode"] or 0))

    lines: list[str] = []
    for entry in entries:
        if entry["episode"] is not None:
            lines.append(f"Episode {entry['episode']:02d}: {entry['dl_link']}")
        else:
            lines.append(f"{entry['filename']}: {entry['dl_link']}")

    summary = (
        f"<b>📦 {resolution} Encoding Complete!</b>\n\n"
        "<code>"
        + "\n".join(lines)
        + "</code>"
    )

    try:
        await app_.send_message(chat_id, summary)
    except Exception as exc:
        log.error("Failed to send batch summary: %s", exc)

    batch_tracker.pop(key, None)


# ── Startup Checks ───────────────────────────────────────────

def _check_ffmpeg() -> bool:
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            log.critical("%s not found in PATH – please install FFmpeg.", tool)
            return False
    return True


def _check_config() -> bool:
    if not API_ID or not API_HASH or not BOT_TOKEN:
        log.critical(
            "Missing credentials.  Set API_ID, API_HASH, and BOT_TOKEN "
            "in your .env file."
        )
        return False
    return True


# ── Premium user client (lazy start) ─────────────────────────
# Created at module level if USER_SESSION_STRING is set, but NOT started
# until the first large-file upload actually needs it.  This avoids all
# event-loop management complexity – user_client.start() is awaited from
# inside process_job which already runs inside app.run()'s event loop.

app = Client(
    "anidl_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)

if USER_SESSION_STRING:
    user_client = Client(
        "anidl_user",
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=USER_SESSION_STRING,
    )
    log.info("Premium user client configured (lazy start) – files > 2 GB will use it.")
else:
    user_client = None
    log.warning(
        "USER_SESSION_STRING not set. "
        "Files above 2 GB will fall back to BuzzHeavier."
    )

_user_client_started: bool = False


async def _get_user_client() -> Client | None:
    """
    Return the Premium user client, starting it on first call.
    Safe to call from any coroutine running inside app.run()'s event loop.
    """
    global _user_client_started
    if user_client is None:
        return None
    if not _user_client_started:
        log.info("Starting Premium user client…")
        await user_client.start()
        _user_client_started = True
        log.info("Premium user client started.")
    return user_client


# ── Job Processor (wired into queue_manager) ──────────────────

async def process_job(job: Job) -> None:
    """Encode a single file, upload, and send download link."""
    job.status = JobStatus.ENCODING
    chat_id = job.chat_id
    settings = job.settings
    input_path = str(job.file_path)
    original_name = job.file_name
    res_label = f"[{job.resolution}] " if job.resolution else ""

    output_name_no_crc = build_output_filename(original_name, settings)
    output_path = str(TEMP_DIR / output_name_no_crc)

    progress_msg = await app.send_message(
        chat_id,
        f"🔄 <b>Encoding {res_label}:</b> <code>{original_name}</code>\n"
        f"⏳ Starting…",
    )
    job.progress_message_id = progress_msg.id

    last_edit = [0.0]

    async def on_progress(p: EncodeProgress):
        now = time.monotonic()
        if now - last_edit[0] < 20:
            return
        last_edit[0] = now
        try:
            await progress_msg.edit_text(
                f"🔄 <b>Encoding {res_label}:</b> <code>{original_name}</code>\n"
                f"📊 Progress: {p.percentage:.1f}%\n"
                f"⚡ Speed: {p.speed}\n"
                f"🎞 FPS: {p.fps:.1f}\n"
                f"⏱ ETA: {format_duration(p.eta_seconds)}",
            )
        except Exception:
            pass

    encode_start = time.monotonic()
    success = await encode_video(input_path, output_path, settings, on_progress)

    if not success:
        job.status = JobStatus.ERROR
        job.error = "FFmpeg encoding failed"
        try:
            await progress_msg.edit_text(
                f"❌ <b>Encoding failed {res_label}:</b> <code>{original_name}</code>\n"
                f"Skipping this file.",
            )
        except Exception:
            pass
        _try_remove(output_path)
        return

    encode_elapsed = time.monotonic() - encode_start

    ok, output_size = check_output_size(output_path)
    if not ok:
        job.status = JobStatus.ERROR
        job.error = f"Output too large ({format_size(output_size)})"
        try:
            await progress_msg.edit_text(
                f"❌ <b>Output too large {res_label}:</b> {format_size(output_size)} "
                f"(limit: 4 GB)\n"
                f"File: <code>{original_name}</code>",
            )
        except Exception:
            pass
        _try_remove(output_path)
        return

    # ── Compute CRC32 & rebuild filename ─────────────────────
    crc = compute_crc32(output_path)
    output_name = build_output_filename(original_name, settings, crc32=crc)
    final_output_path = str(TEMP_DIR / output_name)

    if output_path != final_output_path:
        try:
            os.rename(output_path, final_output_path)
            output_path = final_output_path
        except OSError as exc:
            log.warning("Could not rename to CRC filename: %s", exc)
            output_name = output_name_no_crc

    # ── Decide upload destination & show status ───────────────
    needs_large_upload = output_size is not None and output_size > BOT_UPLOAD_LIMIT
    if needs_large_upload and user_client is not None:
        upload_note = " via Premium account"
    elif needs_large_upload:
        upload_note = " via BuzzHeavier" if BUZZHEAVIER_API_KEY else " via BuzzHeavier (anon)"
    else:
        upload_note = ""

    try:
        await progress_msg.edit_text(
            f"⬆️ <b>Uploading{upload_note} {res_label}:</b> <code>{output_name}</code>\n"
            f"📦 Size: {format_size(output_size)}",
        )
    except Exception:
        pass

    # ── Lazy-start user_client if needed, then upload ─────────
    active_user_client = await _get_user_client() if needs_large_upload else None

    dl_link: str
    try:
        _hash, _msg_id, dl_link = await upload_to_channel_and_save(
            bot_client=app,
            output_path=output_path,
            output_name=output_name,
            file_size=output_size,
            user_client=active_user_client,
        )
    except _NeedsBuzzHeavier:
        log.info(
            "Falling back to BuzzHeavier for %s (%.2f GB, no Premium session).",
            output_name,
            output_size / (1024 ** 3) if output_size else 0,
        )
        try:
            note = f"Encoded by AniDL | {output_name}"
            dl_link = await upload_to_buzzheavier(output_path, output_name, note=note)
        except Exception as exc:
            log.error("BuzzHeavier upload failed: %s", exc)
            job.status = JobStatus.ERROR
            job.error = f"BuzzHeavier upload failed: {exc}"
            try:
                await progress_msg.edit_text(
                    f"❌ <b>Upload failed {res_label}:</b> <code>{original_name}</code>\n"
                    f"Telegram: file too large (>2 GB, no Premium session)\n"
                    f"BuzzHeavier: {exc}",
                )
            except Exception:
                pass
            _try_remove(output_path)
            return
    except Exception as exc:
        log.error("Channel upload failed: %s", exc)
        job.status = JobStatus.ERROR
        job.error = f"Channel upload failed: {exc}"
        try:
            await progress_msg.edit_text(
                f"❌ <b>Channel upload failed {res_label}:</b> <code>{original_name}</code>\n"
                f"{exc}",
            )
        except Exception:
            pass
        _try_remove(output_path)
        return

    job.dl_link = dl_link
    job.output_file_name = output_name

    # ── Send download link to user ────────────────────────────
    ep_num = extract_episode_number(original_name)
    if ep_num is not None:
        link_text = f"Episode {ep_num:02d}: {dl_link}"
    else:
        link_text = f"{output_name}: {dl_link}"

    try:
        await app.send_message(chat_id, f"<code>{link_text}</code>")
    except Exception as exc:
        log.error("Failed to send download link to user: %s", exc)

    # ── Optionally send file to user ──────────────────────────
    if UPLOAD_TO_USER:
        job.status = JobStatus.UPLOADING
        try:
            await progress_msg.edit_text(
                f"⬆️ <b>Uploading to you {res_label}:</b> <code>{output_name}</code>\n"
                f"📦 Size: {format_size(output_size)}",
            )
        except Exception:
            pass

        caption = build_caption(original_name, settings, output_size, encode_elapsed)

        try:
            await app.send_document(
                chat_id=chat_id,
                document=output_path,
                file_name=output_name,
                caption=caption,
            )
            job.status = JobStatus.DONE
        except Exception as exc:
            log.error("Upload to user failed: %s", exc)
            job.status = JobStatus.ERROR
            job.error = str(exc)
            try:
                await progress_msg.edit_text(
                    f"❌ <b>Upload failed {res_label}:</b> <code>{original_name}</code>\n"
                    f"{exc}",
                )
            except Exception:
                pass
            _try_remove(output_path)
            await record_and_maybe_summarise(
                app, job.user_id, job.resolution, ep_num, output_name, dl_link
            )
            return
    else:
        job.status = JobStatus.DONE

    _try_remove(output_path)

    try:
        await progress_msg.edit_text(
            f"✅ <b>Done {res_label}:</b> <code>{output_name}</code>\n"
            f"🔗 <code>{dl_link}</code>",
        )
    except Exception:
        pass

    await record_and_maybe_summarise(
        app, job.user_id, job.resolution, ep_num, output_name, dl_link
    )


def _try_remove(path: str):
    """Silently remove a file."""
    try:
        os.remove(path)
    except OSError:
        pass


# ── Main ──────────────────────────────────────────────────────

def main():
    if not _check_config():
        sys.exit(1)
    if not _check_ffmpeg():
        sys.exit(1)

    # Register handlers
    register_all_commands(app)
    register_all_torrent_handlers(app)
    register_all_callbacks(app)

    # Wire the queue worker
    queue_manager.set_handler(process_job)

    log.info("Bot starting…")
    app.run()   # ← identical to original; no event-loop tricks


if __name__ == "__main__":
    main()
