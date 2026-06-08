"""Thread-safe JSON state ledger.

The ledger is the source of truth for idempotency and crash recovery. It is
keyed by document content hash so that re-saving an identical file is a no-op,
while editing a file (new hash) re-triggers processing.

Writes are atomic (temp file + ``os.replace``) and guarded by a re-entrant lock
so multiple worker threads can update state concurrently without corrupting the
file.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from docpipe.core.exceptions import StorageError
from docpipe.models.documents import DocumentRecord, ProcessingState


class JsonStateStore:
    """Persistent, concurrency-safe ledger of document processing state."""

    def __init__(self, state_file: Path) -> None:
        self._path = state_file
        self._lock = threading.RLock()
        self._records: dict[str, DocumentRecord] = {}
        self._load()

    # --- Persistence -------------------------------------------------------
    def _load(self) -> None:
        with self._lock:
            if not self._path.exists():
                self._records = {}
                return
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                raise StorageError(f"Cannot read state file {self._path}: {exc}") from exc
            self._records = {
                key: DocumentRecord.from_dict(value) for key, value in raw.items()
            }

    def _flush(self) -> None:
        """Atomically persist the in-memory ledger to disk."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {key: rec.to_dict() for key, rec in self._records.items()}
        try:
            fd, tmp_name = tempfile.mkstemp(
                dir=str(self._path.parent), prefix=".state-", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, indent=2, sort_keys=True)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_name, self._path)
            finally:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)
        except OSError as exc:
            raise StorageError(f"Cannot write state file {self._path}: {exc}") from exc

    # --- Queries -----------------------------------------------------------
    def get(self, file_hash: str) -> Optional[DocumentRecord]:
        with self._lock:
            return self._records.get(file_hash)

    def is_completed(self, file_hash: str) -> bool:
        with self._lock:
            record = self._records.get(file_hash)
            return record is not None and record.state is ProcessingState.COMPLETED

    def records(self) -> dict[str, DocumentRecord]:
        """Return a snapshot copy of all ledger records, keyed by content hash."""
        with self._lock:
            return dict(self._records)

    def should_process(self, file_hash: str, max_attempts: int) -> bool:
        """Return True if a document needs (re)processing.

        A document is processed when it is unknown, not yet COMPLETED, or FAILED
        but still within the retry budget.
        """
        with self._lock:
            record = self._records.get(file_hash)
            if record is None:
                return True
            if record.state is ProcessingState.COMPLETED:
                return False
            if record.state is ProcessingState.FAILED:
                return record.attempts < max_attempts
            return True

    # --- Mutations ---------------------------------------------------------
    def upsert(self, record: DocumentRecord) -> DocumentRecord:
        with self._lock:
            record.updated_at = _utc_now()
            self._records[record.file_hash] = record
            self._flush()
            return record

    def start(self, file_hash: str, source_path: str) -> DocumentRecord:
        """Create or reset a record to PENDING, incrementing the attempt count."""
        with self._lock:
            record = self._records.get(file_hash)
            if record is None:
                record = DocumentRecord(file_hash=file_hash, source_path=source_path)
            record.source_path = source_path
            record.state = ProcessingState.PENDING
            record.attempts += 1
            record.error = None
            record.updated_at = _utc_now()
            self._records[file_hash] = record
            self._flush()
            return record

    def mark(
        self,
        file_hash: str,
        state: ProcessingState,
        error: Optional[str] = None,
    ) -> DocumentRecord:
        with self._lock:
            record = self._records.get(file_hash)
            if record is None:
                raise StorageError(f"No ledger record for hash {file_hash}")
            record.state = state
            record.error = error
            record.updated_at = _utc_now()
            self._flush()
            return record

    def delete(self, file_hash: str) -> bool:
        """Remove a record from the ledger. Returns True if one was removed.

        Clearing an entry makes ``should_process`` return True again, which is
        how the web layer forces a retry or full reprocess of a document.
        """
        with self._lock:
            if file_hash not in self._records:
                return False
            del self._records[file_hash]
            self._flush()
            return True


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
