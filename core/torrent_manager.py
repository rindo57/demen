"""
Torrent manager – handles magnet links and .torrent files via libtorrent.

All blocking libtorrent calls are wrapped with ``asyncio.to_thread`` so they
don't block the Pyrogram event-loop.
"""

import asyncio
import logging
import tempfile
import uuid
from pathlib import Path
from typing import Callable, Optional

import aiohttp
import libtorrent as lt

from config import DOWNLOAD_DIR, MAX_RETRIES, RETRY_DELAY

log = logging.getLogger(__name__)


class TorrentFile:
    """Represents a single file inside a torrent."""

    __slots__ = ("index", "path", "name", "size")

    def __init__(self, index: int, path: str, name: str, size: int):
        self.index = index
        self.path = path
        self.name = name
        self.size = size

    def __repr__(self) -> str:
        return f"<TorrentFile {self.index}: {self.name} ({self.size})>"


class TorrentSession:
    """
    Wraps a single libtorrent download.

    Lifecycle:
        1.  ``add_magnet()`` or ``add_torrent_file()``
        2.  ``wait_for_metadata()``   → populates ``self.files``
        3.  ``set_file_priorities()``  → user selects which files to grab
        4.  ``download()``            → blocks until complete
        5.  ``cleanup()``
    """

    def __init__(self):
        self.session_id: str = uuid.uuid4().hex[:8]
        self._ses: lt.session = lt.session()
        self._ses.listen_on(6881, 6891)

        # Performance settings
        settings = {
            "active_downloads": 3,
            "alert_mask": lt.alert.category_t.status_notification,
        }
        self._ses.apply_settings(settings)

        self._handle: Optional[lt.torrent_handle] = None
        self.files: list[TorrentFile] = []
        self.save_path: Path = DOWNLOAD_DIR / self.session_id

    # ── Adding Torrents ───────────────────────────────────────

    async def add_magnet(self, magnet_uri: str) -> None:
        """Add a magnet link to the session."""
        self.save_path.mkdir(parents=True, exist_ok=True)

        def _add():
            params = lt.parse_magnet_uri(magnet_uri)
            params.save_path = str(self.save_path)
            # Don't download anything yet — just metadata
            params.flags |= lt.torrent_flags.upload_mode
            self._handle = self._ses.add_torrent(params)
            # Disable upload mode so metadata can be fetched
            self._handle.unset_flags(lt.torrent_flags.upload_mode)
            # Set all file priorities to 0 initially (after metadata)

        await asyncio.to_thread(_add)

    async def add_torrent_file(self, torrent_path: str) -> None:
        """Add a ``.torrent`` file to the session."""
        self.save_path.mkdir(parents=True, exist_ok=True)

        def _add():
            info = lt.torrent_info(torrent_path)
            params = lt.add_torrent_params()
            params.ti = info
            params.save_path = str(self.save_path)
            self._handle = self._ses.add_torrent(params)

        await asyncio.to_thread(_add)

    async def add_torrent_from_url(self, url: str) -> str:
        """Download a ``.torrent`` from *url*, save to temp, return path."""
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                resp.raise_for_status()
                data = await resp.read()

        tmp = tempfile.NamedTemporaryFile(
            suffix=".torrent", delete=False, dir=str(self.save_path),
        )
        tmp.write(data)
        tmp.close()
        return tmp.name

    # ── Metadata ──────────────────────────────────────────────

    async def wait_for_metadata(self, timeout: int = 120) -> bool:
        """
        Block (async) until torrent metadata is available.
        Returns True on success, False on timeout.
        """

        def _wait() -> bool:
            import time

            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if self._handle.has_metadata():
                    return True
                time.sleep(0.5)
            return False

        ok = await asyncio.to_thread(_wait)
        if ok:
            self._populate_files()
            # Set all files to "don't download" until user selects
            self._set_all_priorities(0)
        return ok

    def _populate_files(self) -> None:
        """Read the file-list from torrent metadata."""
        ti = self._handle.torrent_file()
        fs = ti.files()
        self.files = [
            TorrentFile(
                index=i,
                path=fs.file_path(i),
                name=Path(fs.file_path(i)).name,
                size=fs.file_size(i),
            )
            for i in range(fs.num_files())
        ]

    # ── File Priorities ───────────────────────────────────────

    def _set_all_priorities(self, priority: int) -> None:
        for i in range(len(self.files)):
            self._handle.file_priority(i, priority)

    async def set_file_priorities(self, selected_indices: set[int]) -> None:
        """Set priority 4 for selected files, 0 for the rest."""

        def _set():
            for f in self.files:
                pri = 4 if f.index in selected_indices else 0
                self._handle.file_priority(f.index, pri)

        await asyncio.to_thread(_set)

    # ── Download ──────────────────────────────────────────────

    async def download(
        self,
        progress_callback: Optional[Callable[[float, float, int], None]] = None,
    ) -> None:
        """
        Download selected files.  Calls *progress_callback(progress_pct,
        download_rate_bytes, num_peers)* periodically.  Retries on failure.
        """
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                await self._download_loop(progress_callback)
                return
            except Exception as exc:
                log.warning(
                    "Download attempt %d/%d failed: %s",
                    attempt, MAX_RETRIES, exc,
                )
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(RETRY_DELAY)
                else:
                    raise

    async def _download_loop(
        self,
        cb: Optional[Callable] = None,
    ) -> None:
        import time

        self._handle.resume()

        def _loop():
            while True:
                s = self._handle.status()
                if s.is_seeding or s.state == lt.torrent_status.seeding:
                    break
                # Also break when 100 % and state is finished
                if s.progress >= 1.0:
                    break
                if cb:
                    cb(s.progress * 100, s.download_rate, s.num_peers)
                time.sleep(1)

        await asyncio.to_thread(_loop)

    # ── Helpers ───────────────────────────────────────────────

    def get_downloaded_file_paths(self, selected: set[int]) -> list[Path]:
        """Return absolute paths for the files that were downloaded."""
        paths: list[Path] = []
        for f in self.files:
            if f.index in selected:
                p = self.save_path / f.path
                if p.exists():
                    paths.append(p)
        return paths

    async def cleanup(self) -> None:
        """Remove the torrent from the session and delete on-disk data."""

        def _cleanup():
            if self._handle:
                self._ses.remove_torrent(self._handle, lt.options_t.delete_files)

        try:
            await asyncio.to_thread(_cleanup)
        except Exception as exc:
            log.warning("Cleanup error: %s", exc)
