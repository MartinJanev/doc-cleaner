"""Bounded worker pool consuming document-processing jobs.

Decouples event detection from processing. Producers (the watcher and the
startup reconciler) ``submit`` paths; a small pool of worker threads pulls them
off the queue and hands them to the ``DocumentProcessor``. An in-flight set
dedupes paths so rapid duplicate events do not enqueue the same file twice.
"""

from __future__ import annotations

import queue
import threading
from pathlib import Path
from typing import Callable

from docpipe.core.logging import get_logger

logger = get_logger(__name__)

_SENTINEL = None


class JobQueue:
    """Thread-pool job queue for document paths."""

    def __init__(
        self,
        handler: Callable[[Path], object],
        max_workers: int,
    ) -> None:
        self._handler = handler
        self._max_workers = max_workers
        self._queue: "queue.Queue[Path | None]" = queue.Queue()
        self._in_flight: set[str] = set()
        self._lock = threading.Lock()
        self._workers: list[threading.Thread] = []
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        for index in range(self._max_workers):
            worker = threading.Thread(
                target=self._run, name=f"docpipe-worker-{index}", daemon=True
            )
            worker.start()
            self._workers.append(worker)
        logger.info("jobqueue.started", workers=self._max_workers)

    def submit(self, path: Path) -> bool:
        """Enqueue a path unless an identical path is already in flight.

        Returns True if the job was accepted.
        """
        key = str(path)
        with self._lock:
            if key in self._in_flight:
                logger.debug("jobqueue.duplicate", path=key)
                return False
            self._in_flight.add(key)
        self._queue.put(path)
        logger.debug("jobqueue.submitted", path=key)
        return True

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is _SENTINEL:
                    return
                try:
                    self._handler(item)
                except Exception as exc:  # handler is expected to self-contain
                    logger.error("jobqueue.handler_error", path=str(item), error=repr(exc))
                finally:
                    with self._lock:
                        self._in_flight.discard(str(item))
            finally:
                self._queue.task_done()

    def join(self) -> None:
        """Block until all queued jobs have been processed."""
        self._queue.join()

    def shutdown(self) -> None:
        """Signal workers to stop and wait for them to finish."""
        for _ in self._workers:
            self._queue.put(_SENTINEL)
        for worker in self._workers:
            worker.join(timeout=5.0)
        logger.info("jobqueue.shutdown")
