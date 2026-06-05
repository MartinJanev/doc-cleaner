"""Debounced watchdog event handler.

Editors and copy operations emit bursts of created/modified events, and a file
may still be growing when the first event fires. This handler debounces per
path and waits for the file size to stop changing before enqueuing, so we never
hand a half-written file to Docling.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

from watchdog.events import (
    FileCreatedEvent,
    FileModifiedEvent,
    FileSystemEvent,
    FileSystemEventHandler,
)

from docpipe.core.logging import get_logger

logger = get_logger(__name__)


class DebouncedHandler(FileSystemEventHandler):
    """Schedules a stabilized enqueue after each create/modify event."""

    def __init__(
        self,
        on_ready: Callable[[Path], object],
        is_supported: Callable[[Path], bool],
        debounce_seconds: float,
    ) -> None:
        self._on_ready = on_ready
        self._is_supported = is_supported
        self._debounce = debounce_seconds
        self._timers: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def on_created(self, event: FileSystemEvent) -> None:
        if isinstance(event, FileCreatedEvent) and not event.is_directory:
            self._schedule(Path(str(event.src_path)))

    def on_modified(self, event: FileSystemEvent) -> None:
        if isinstance(event, FileModifiedEvent) and not event.is_directory:
            self._schedule(Path(str(event.src_path)))

    def _schedule(self, path: Path) -> None:
        if not self._is_supported(path):
            return
        key = str(path)
        with self._lock:
            existing = self._timers.pop(key, None)
            if existing is not None:
                existing.cancel()
            timer = threading.Timer(self._debounce, self._fire, args=(path,))
            timer.daemon = True
            self._timers[key] = timer
            timer.start()

    def _fire(self, path: Path) -> None:
        with self._lock:
            self._timers.pop(str(path), None)
        if not self._is_stable(path):
            logger.debug("watcher.unstable_retry", path=str(path))
            self._schedule(path)
            return
        logger.info("watcher.ready", path=str(path))
        self._on_ready(path)

    def _is_stable(self, path: Path) -> bool:
        """Return True if the file exists and its size held across a short wait."""
        try:
            first = path.stat().st_size
        except OSError:
            return False
        waiter = threading.Event()
        waiter.wait(min(self._debounce, 1.0))
        try:
            second = path.stat().st_size
        except OSError:
            return False
        return first == second
