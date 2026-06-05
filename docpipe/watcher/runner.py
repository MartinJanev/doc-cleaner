"""Watcher runner: startup reconciliation + live monitoring.

Owns the lifecycle of the daemon:
1. Reconcile on startup - scan the input dir and enqueue anything not already
   COMPLETED (crash recovery / idempotency).
2. Start the watchdog observer for live create/modify events.
3. Block until SIGINT/SIGTERM, then drain and shut down gracefully.
"""

from __future__ import annotations

import signal
import threading
from pathlib import Path

from watchdog.observers import Observer

from docpipe.core.logging import get_logger
from docpipe.pipeline.processor import DocumentProcessor
from docpipe.storage.file_repository import FileRepository
from docpipe.watcher.event_handler import DebouncedHandler
from docpipe.watcher.job_queue import JobQueue

logger = get_logger(__name__)


class WatcherRunner:
    """Coordinates the queue, observer, and graceful shutdown."""

    def __init__(
        self,
        input_dir: Path,
        processor: DocumentProcessor,
        repository: FileRepository,
        job_queue: JobQueue,
        debounce_seconds: float,
    ) -> None:
        self._input_dir = input_dir
        self._processor = processor
        self._repository = repository
        self._queue = job_queue
        self._debounce = debounce_seconds
        self._observer = Observer()
        self._stop_event = threading.Event()

    def reconcile(self) -> int:
        """Enqueue any pre-existing input files that still need processing."""
        count = 0
        for path in self._repository.iter_input_files():
            if self._queue.submit(path):
                count += 1
        logger.info("runner.reconcile", enqueued=count)
        return count

    def start(self) -> None:
        """Run the daemon until interrupted."""
        self._repository.ensure_directories()
        self._queue.start()
        self.reconcile()

        handler = DebouncedHandler(
            on_ready=self._queue.submit,
            is_supported=self._repository.is_supported,
            debounce_seconds=self._debounce,
        )
        self._observer.schedule(handler, str(self._input_dir), recursive=True)
        self._observer.start()
        logger.info("runner.watching", input_dir=str(self._input_dir))

        self._install_signal_handlers()
        try:
            self._stop_event.wait()
        finally:
            self._shutdown()

    def _install_signal_handlers(self) -> None:
        def _handle(signum: int, _frame: object) -> None:
            logger.info("runner.signal", signal=signum)
            self._stop_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _handle)
            except ValueError:
                # Not on the main thread (e.g. under test); skip.
                pass

    def _shutdown(self) -> None:
        logger.info("runner.shutdown.start")
        self._observer.stop()
        self._observer.join(timeout=5.0)
        self._queue.join()
        self._queue.shutdown()
        logger.info("runner.shutdown.done")
