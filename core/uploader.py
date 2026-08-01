"""
Channel uploader – uploads encoded files to the configured Telegram channel,
generates download link hashes, and persists metadata in MongoDB.

Upload routing:
  ┌─────────────────────────────────────────────────────────────────┐
  │  file ≤ 2 GB                 → bot client  → Telegram channel  │
  │  file > 2 GB + user session  → user client → Telegram channel  │
  │  file > 2 GB, no session     → BuzzHeavier (HTTP PUT)          │
  └─────────────────────────────────────────────────────────────────┘
"""

import asyncio
import base64
import logging
import os
from pathlib import Path
from typing import Optional

import aiohttp
import aiofiles
from pyrogram import Client
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import (
    BUZZHEAVIER_API_KEY,
    BOT_UPLOAD_LIMIT,
    DL_BASE_URL,
    UPLOAD_CHANNEL_ID,
)
from db import generate_dl_hash, save_file_to_dl_db

log = logging.getLogger(__name__)

_BUZZHEAVIER_UPLOAD_BASE = "https://w.buzzheavier.com"
_BUZZHEAVIER_DL_BASE = "https://buzzheavier.com"


# ── BuzzHeavier upload ────────────────────────────────────────

async def upload_to_buzzheavier(
    file_path: str,
    file_name: str,
    note: str = "",
) -> str:
    """
    Upload *file_path* to BuzzHeavier via an async streaming PUT request.

    Returns the public download URL (``https://buzzheavier.com/{file_id}``).
    Raises ``RuntimeError`` on failure.
    """
    # Build URL  –  PUT https://w.buzzheavier.com/{filename}
    safe_name = Path(file_name).name[:500]  # API limit: 500 chars
    url = f"{_BUZZHEAVIER_UPLOAD_BASE}/{safe_name}"

    params: dict[str, str] = {}
    if note:
        params["note"] = base64.b64encode(note.encode()).decode()

    headers: dict[str, str] = {"Content-Type": "application/octet-stream"}
    if BUZZHEAVIER_API_KEY:
        headers["Authorization"] = f"Bearer {BUZZHEAVIER_API_KEY}"

    file_size = os.path.getsize(file_path)
    headers["Content-Length"] = str(file_size)

    log.info(
        "Uploading %.2f GB to BuzzHeavier: %s",
        file_size / (1024 ** 3),
        safe_name,
    )

    # Stream the file so we don't load it all into RAM
    async with aiofiles.open(file_path, "rb") as fh:

        async def _file_sender():
            chunk_size = 8 * 1024 * 1024  # 8 MB chunks
            while True:
                chunk = await fh.read(chunk_size)
                if not chunk:
                    break
                yield chunk

        connector = aiohttp.TCPConnector(limit=1)
        timeout = aiohttp.ClientTimeout(
            total=None,        # no overall timeout (large files take time)
            connect=30,
            sock_read=120,
        )
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            async with session.put(
                url,
                data=_file_sender(),
                headers=headers,
                params=params,
            ) as resp:
                body = await resp.json(content_type=None)
                if resp.status not in (200, 201):
                    raise RuntimeError(
                        f"BuzzHeavier returned HTTP {resp.status}: {body}"
                    )

    # Response contains the file id
    # Typical response: {"id": "abc123", "name": "...", ...}
    file_id = body.get("data", {}).get("id") or body.get("id")
    if not file_id:
        raise RuntimeError(f"BuzzHeavier response missing file id: {body}")

    dl_url = f"{_BUZZHEAVIER_DL_BASE}/{file_id}"
    log.info("BuzzHeavier upload complete: %s → %s", safe_name, dl_url)
    return dl_url


# ── Telegram channel upload ───────────────────────────────────

async def upload_to_channel_and_save(
    bot_client: Client,
    output_path: str,
    output_name: str,
    file_size: int | None = None,
    user_client: Client | None = None,
) -> tuple[str, int, str]:
    """
    Upload an encoded file to the Telegram channel and save its metadata.

    Returns ``(file_hash, channel_message_id, dl_url)`` where *dl_url* is
    the AniDL download link built from *file_hash*.

    Routing:
    - file ≤ 2 GB → bot client
    - file > 2 GB + user_client → Premium user client
    - file > 2 GB, no user_client → raises ``NeedsBuzzHeavier`` (caller handles)
    """
    file_hash = generate_dl_hash()
    dl_url = f"{DL_BASE_URL}{file_hash}"

    dl_markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton(text="🔗 Download Link", url=dl_url)]]
    )

    if file_size is None:
        try:
            file_size = os.path.getsize(output_path)
        except OSError:
            file_size = None

    needs_premium = file_size is not None and file_size > BOT_UPLOAD_LIMIT

    if needs_premium and user_client is None:
        # No premium session at all – caller will try BuzzHeavier
        raise _NeedsBuzzHeavier()

    if needs_premium:
        log.info(
            "File %s is %.2f GB – attempting Premium user client upload.",
            output_name,
            file_size / (1024 ** 3),
        )
        try:
            ch_msg = await user_client.send_document(
                chat_id=UPLOAD_CHANNEL_ID,
                document=output_path,
                file_name=output_name,
                caption=f"`{output_name}: {dl_url}`",
                reply_markup=dl_markup,
            )
        except Exception as exc:
            # Pyrofork can fail for large files via user sessions (media-DC auth
            # not fully established, NoneType write buffer, etc.).
            # Fall through to BuzzHeavier rather than surfacing a cryptic error.
            log.warning(
                "Premium Telegram upload failed (%s) – falling back to BuzzHeavier.",
                exc,
            )
            raise _NeedsBuzzHeavier()
    else:
        ch_msg = await bot_client.send_document(
            chat_id=UPLOAD_CHANNEL_ID,
            document=output_path,
            file_name=output_name,
            caption=f"`{output_name}: {dl_url}`",
            reply_markup=dl_markup,
        )

    msg_id = int(ch_msg.id)

    save_file_to_dl_db(
        filename=output_name,
        file_hash=file_hash,
        msg_id=msg_id,
        file_size=file_size,
    )

    log.info(
        "Uploaded to Telegram channel: %s → hash=%s msg_id=%d",
        output_name, file_hash, msg_id,
    )
    return file_hash, msg_id, dl_url


class _NeedsBuzzHeavier(Exception):
    """Internal signal: file is too large for bot and no Premium session exists."""
