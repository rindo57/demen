"""
MongoDB helpers for download-link storage.

Stores encoded-file metadata in the same schema used by the goat upload bot:

    { hash, file_size, filename, msg_id }

Connection details are pulled from environment variables so the bot can
share the same collection as the main DDL site.
"""

import logging
import random
from string import ascii_letters, digits

from pymongo import MongoClient

from config import MONGO_DL_URI, MONGO_DL_DB, MONGO_DL_COLLECTION

log = logging.getLogger(__name__)

# ── Lazy singleton connection ────────────────────────────────

_collection = None


def get_db_collection():
    """Return the PyMongo collection, creating the client on first call."""
    global _collection
    if _collection is not None:
        return _collection

    if not MONGO_DL_URI:
        log.warning("MONGO_DL_URI not set – download-link DB is disabled.")
        return None

    try:
        client = MongoClient(MONGO_DL_URI)
        db = client[MONGO_DL_DB]
        _collection = db[MONGO_DL_COLLECTION]
        log.info(
            "Connected to MongoDB: %s.%s", MONGO_DL_DB, MONGO_DL_COLLECTION
        )
        return _collection
    except Exception as exc:
        log.error("Failed to connect to MongoDB: %s", exc)
        return None


# ── Hash generation ──────────────────────────────────────────

def generate_dl_hash(length: int = 50) -> str:
    """Generate a random alphanumeric hash for the download link."""
    return "".join(random.choice(ascii_letters + digits) for _ in range(length))


# ── CRUD helpers ─────────────────────────────────────────────

def save_file_to_dl_db(
    filename: str,
    file_hash: str,
    msg_id: int,
    file_size: int | None = None,
) -> None:
    """
    Upsert a file record.  Matches the schema used by the goat bot:

        { hash, filename, msg_id, file_size }
    """
    col = get_db_collection()
    if col is None:
        return

    update_data: dict = {
        "filename": filename,
        "msg_id": msg_id,
    }
    if file_size is not None:
        update_data["file_size"] = file_size

    try:
        col.update_one(
            {"hash": file_hash},
            {
                "$set": update_data,
                "$unset": {"filenamex": "", "fid": "", "code": ""},
            },
            upsert=True,
        )
    except Exception as exc:
        log.error("save_file_to_dl_db failed: %s", exc)


def find_by_hash(file_hash: str):
    """Look up a file record by its download hash."""
    col = get_db_collection()
    if col is None:
        return None
    try:
        return col.find_one({"hash": file_hash})
    except Exception as exc:
        log.error("find_by_hash failed: %s", exc)
        return None
