"""
Per-user job queue – ensures one encode/upload runs at a time per user,
while supporting multiple users concurrently.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


class JobStatus(Enum):
    QUEUED = auto()
    DOWNLOADING = auto()
    ENCODING = auto()
    UPLOADING = auto()
    DONE = auto()
    ERROR = auto()


@dataclass
class Job:
    """A single file encode+upload job."""

    user_id: int
    chat_id: int
    file_index: int
    file_name: str
    file_path: Path
    settings: dict
    resolution: str = ""  # e.g. "480p", "720p", "1080p"
    status: JobStatus = JobStatus.QUEUED
    error: Optional[str] = None
    progress_message_id: Optional[int] = None
    # Download-link fields (populated after channel upload)
    dl_hash: Optional[str] = None
    dl_link: Optional[str] = None
    output_file_name: Optional[str] = None


class QueueManager:
    """
    Manages per-user asyncio Queues and worker tasks.

    Call ``enqueue()`` to add jobs.  A background worker is automatically
    spawned the first time a user submits work.
    """

    def __init__(self):
        self._queues: dict[int, asyncio.Queue[Job]] = {}
        self._workers: dict[int, asyncio.Task] = {}
        self._job_handler = None  # set by bot.py

    def set_handler(self, handler):
        """Register the coroutine that processes a single ``Job``."""
        self._job_handler = handler

    async def enqueue(self, job: Job) -> None:
        uid = job.user_id
        if uid not in self._queues:
            self._queues[uid] = asyncio.Queue()
        await self._queues[uid].put(job)

        # Spawn a worker if none is running for this user
        if uid not in self._workers or self._workers[uid].done():
            self._workers[uid] = asyncio.create_task(
                self._worker(uid), name=f"worker-{uid}"
            )

    async def _worker(self, user_id: int) -> None:
        q = self._queues[user_id]
        while not q.empty():
            job = await q.get()
            try:
                if self._job_handler:
                    await self._job_handler(job)
            except Exception as exc:
                log.exception("Job failed for user %d: %s", user_id, exc)
                job.status = JobStatus.ERROR
                job.error = str(exc)
            finally:
                q.task_done()

    def queue_size(self, user_id: int) -> int:
        if user_id in self._queues:
            return self._queues[user_id].qsize()
        return 0


# Singleton
queue_manager = QueueManager()
