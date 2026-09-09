"""Persistent image-description cache with asynchronous access helpers."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from astrbot.api import logger

DEBUG_MODE = False


class ImageDescriptionCache:
    """Store image descriptions in memory and persist them as JSONL.

    The synchronous methods remain available for command handlers and backwards
    compatibility. Message-processing code should use the asynchronous methods,
    which move file work to a worker thread. A per-URL single-flight operation
    prevents concurrent messages from requesting the same image description more
    than once.
    """

    _MIN_ENTRIES = 10
    _MAX_ENTRIES = 10_000
    _RETENTION_RATIO = 0.8

    def __init__(
        self,
        data_dir: str,
        max_entries: int = 500,
        enabled: bool = False,
    ) -> None:
        """Initialize an image-description cache.

        Args:
            data_dir: Directory used for plugin data.
            max_entries: Maximum number of persisted JSONL entries.
            enabled: Whether the cache should be active.
        """
        try:
            configured_max_entries = int(max_entries)
        except (TypeError, ValueError):
            configured_max_entries = 500

        self._enabled = bool(enabled)
        self._max_entries = max(
            self._MIN_ENTRIES,
            min(configured_max_entries, self._MAX_ENTRIES),
        )
        self._cache_dir = Path(data_dir) / "image_cache"
        self._cache_file = self._cache_dir / "descriptions.jsonl"
        self._entry_count = 0
        self._cache_index: dict[str, str] = {}
        self._file_lock = threading.RLock()
        self._initialized = False
        self._inflight: dict[str, asyncio.Future[str | None]] = {}
        self._inflight_lock: asyncio.Lock | None = None
        self._inflight_loop: asyncio.AbstractEventLoop | None = None

        if self._enabled:
            self._init_storage()

    def _init_storage(self) -> None:
        """Create storage and load the in-memory lookup index."""
        try:
            with self._file_lock:
                self._cache_dir.mkdir(parents=True, exist_ok=True)
                self._entry_count = self._load_index()
                if self._entry_count > self._max_entries:
                    self._cleanup_oldest()
                self._initialized = True
            logger.debug(
                "[ImageCache] initialized: entries=%s max=%s file=%s",
                self._entry_count,
                self._max_entries,
                self._cache_file,
            )
        except OSError as error:
            logger.error(f"[ImageCache] initialization failed: {error}")
            self._initialized = False

    def _load_index(self) -> int:
        """Load the JSONL file into the in-memory URL index.

        Returns:
            Number of non-empty physical entries in the cache file.
        """
        self._cache_index.clear()
        if not self._cache_file.exists():
            return 0

        entry_count = 0
        try:
            with open(self._cache_file, encoding="utf-8-sig") as file:
                for line in file:
                    if not line.strip():
                        continue
                    entry_count += 1
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(entry, dict):
                        continue
                    url = entry.get("u")
                    description = entry.get("d")
                    if isinstance(url, str) and url and isinstance(description, str):
                        if description:
                            self._cache_index[url] = description
        except OSError as error:
            logger.warning(f"[ImageCache] failed to load index: {error}")
            return 0
        return entry_count

    @property
    def enabled(self) -> bool:
        """Return whether the cache is ready for use."""
        return self._enabled and self._initialized

    @property
    def entry_count(self) -> int:
        """Return the number of persisted cache entries."""
        with self._file_lock:
            return self._entry_count

    def lookup(self, url: str) -> str | None:
        """Look up an image description without scanning the cache file.

        Args:
            url: Image URL or local path.

        Returns:
            Cached description, or None when no entry exists.
        """
        if not self.enabled or not url:
            return None
        with self._file_lock:
            description = self._cache_index.get(url)
        if description and DEBUG_MODE:
            logger.debug(f"[ImageCache] hit: {url[:80]}...")
        return description

    async def lookup_async(self, url: str) -> str | None:
        """Look up a description without blocking the event loop."""
        return await asyncio.to_thread(self.lookup, url)

    def save(self, url: str, description: str) -> None:
        """Persist a description if the URL is not already cached.

        Args:
            url: Image URL or local path.
            description: Generated image description.
        """
        if not self.enabled or not url or not description:
            return

        with self._file_lock:
            if url in self._cache_index:
                return
            try:
                self._cache_dir.mkdir(parents=True, exist_ok=True)
                entry = {"u": url, "d": description, "t": int(time.time())}
                with open(self._cache_file, "a", encoding="utf-8") as file:
                    file.write(json.dumps(entry, ensure_ascii=False) + "\n")
                self._cache_index[url] = description
                self._entry_count += 1
                if self._entry_count > self._max_entries:
                    self._cleanup_oldest()
            except OSError as error:
                logger.warning(f"[ImageCache] save failed: {error}")

    async def save_async(self, url: str, description: str) -> None:
        """Persist a description without blocking the event loop."""
        await asyncio.to_thread(self.save, url, description)

    async def get_or_create(
        self,
        url: str,
        factory: Callable[[], Awaitable[str | None]],
    ) -> str | None:
        """Return a cached value or create one with per-URL single-flight.

        Args:
            url: Image URL or local path.
            factory: Async callback that generates a description when needed.

        Returns:
            Cached or newly generated description.

        Raises:
            Exception: Propagates an exception raised by the owner callback.
        """
        if not self.enabled or not url:
            return await factory()

        cached = await self.lookup_async(url)
        if cached:
            return cached

        lock = self._get_inflight_lock()
        owner = False
        async with lock:
            cached = await self.lookup_async(url)
            if cached:
                return cached
            future = self._inflight.get(url)
            if future is None:
                future = asyncio.get_running_loop().create_future()
                self._inflight[url] = future
                owner = True

        if not owner:
            return await asyncio.shield(future)

        try:
            description = await factory()
            if description:
                await self.save_async(url, description)
                description = description.strip() or None
            if not future.done():
                future.set_result(description)
            return description
        except asyncio.CancelledError:
            if not future.done():
                future.set_result(None)
            raise
        except Exception:
            if not future.done():
                future.set_result(None)
            raise
        finally:
            async with lock:
                if self._inflight.get(url) is future:
                    self._inflight.pop(url, None)

    def _get_inflight_lock(self) -> asyncio.Lock:
        """Return the single-flight lock for the current event loop."""
        loop = asyncio.get_running_loop()
        if self._inflight_lock is None or self._inflight_loop is not loop:
            if self._inflight_loop is not None and self._inflight_loop is not loop:
                self._inflight.clear()
            self._inflight_lock = asyncio.Lock()
            self._inflight_loop = loop
        return self._inflight_lock

    def _cleanup_oldest(self) -> None:
        """Keep the newest 80 percent of entries using atomic replacement."""
        with self._file_lock:
            if not self._cache_file.exists() or self._entry_count <= self._max_entries:
                return

            keep_count = max(1, int(self._max_entries * self._RETENTION_RATIO))
            skip_count = self._entry_count - keep_count
            temporary_path: str | None = None
            descriptor: int | None = None
            try:
                descriptor, temporary_path = tempfile.mkstemp(
                    dir=self._cache_dir,
                    prefix=f".{self._cache_file.name}.",
                    suffix=".tmp",
                )
                skipped = 0
                with open(self._cache_file, encoding="utf-8-sig") as source:
                    with os.fdopen(descriptor, "w", encoding="utf-8") as target:
                        descriptor = None
                        for line in source:
                            if not line.strip():
                                continue
                            if skipped < skip_count:
                                skipped += 1
                                continue
                            target.write(line)
                        target.flush()
                        os.fsync(target.fileno())
                os.replace(temporary_path, self._cache_file)
                temporary_path = None
                self._entry_count = self._load_index()
            except OSError as error:
                logger.error(f"[ImageCache] cleanup failed: {error}")
            finally:
                if descriptor is not None:
                    os.close(descriptor)
                if temporary_path is not None:
                    try:
                        os.unlink(temporary_path)
                    except OSError:
                        pass

    def clear(self) -> bool:
        """Remove current and legacy cache files."""
        with self._file_lock:
            try:
                if self._cache_file.exists():
                    self._cache_file.unlink()
                legacy_path = self._cache_dir.parent / "image_description_cache.json"
                if legacy_path.exists():
                    legacy_path.unlink()
                self._cache_index.clear()
                self._entry_count = 0
                return True
            except OSError as error:
                logger.error(f"[ImageCache] clear failed: {error}")
                return False

    async def clear_async(self) -> bool:
        """Clear cache files without blocking the event loop."""
        return await asyncio.to_thread(self.clear)

    def get_stats(self) -> dict[str, object]:
        """Return cache status for the plugin control page."""
        file_size = 0
        try:
            file_size = self._cache_file.stat().st_size
        except OSError:
            pass
        return {
            "enabled": self._enabled,
            "initialized": self._initialized,
            "entry_count": self.entry_count,
            "max_entries": self._max_entries,
            "file_size_bytes": file_size,
            "file_path": str(self._cache_file),
        }
