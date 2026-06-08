"""Business logic for the web interface.

``DocumentService`` is a thin, testable layer over the same collaborators the
watcher uses: the shared ``FileRepository``, ``JsonStateStore``, and
``JobQueue``. Because the web server runs in the same process as the watcher,
it reads live state straight from the store and enqueues work directly onto the
shared queue - no file re-reads or cross-process coordination.
"""

from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path
from typing import Any, BinaryIO, Optional

from docpipe.core.config import Settings
from docpipe.core.exceptions import StorageError, UploadTooLargeError
from docpipe.core.logging import get_logger
from docpipe.models.documents import DocumentRecord
from docpipe.storage.file_repository import FileRepository
from docpipe.storage.state_store import JsonStateStore

logger = get_logger(__name__)

QUEUED = "QUEUED"  # synthetic state for input files not yet in the ledger
_UPLOAD_CHUNK = 1 << 20  # 1 MiB


class DocumentService:
    """Upload, status, browsing, and management for the document pipeline."""

    def __init__(
        self,
        settings: Settings,
        repository: FileRepository,
        state_store: JsonStateStore,
        submit_job: "Optional[Any]" = None,
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._state = state_store
        # Callable[[Path], bool]; the shared JobQueue.submit. Optional so tests
        # can run without a live queue (uploads still land for the watcher).
        self._submit_job = submit_job
        self._hash_cache: dict[str, tuple[int, int, str]] = {}
        self._cache_lock = threading.Lock()

    # --- Listing -----------------------------------------------------------
    def list_documents(self) -> list[dict[str, Any]]:
        """Return every known document, newest activity first.

        Merges live ledger records with input files not yet picked up (shown as
        ``QUEUED``) and flags whether output artifacts exist on disk.
        """
        records = self._state.records()
        seen_hashes: set[str] = set()
        documents: list[dict[str, Any]] = []

        for file_hash, record in records.items():
            seen_hashes.add(file_hash)
            documents.append(self._record_to_dict(file_hash, record))

        for path in self._repository.iter_input_files():
            try:
                file_hash = self._cached_hash(path)
            except StorageError:
                continue
            if file_hash in seen_hashes:
                continue
            seen_hashes.add(file_hash)
            documents.append(self._queued_to_dict(file_hash, path))

        documents.sort(key=lambda doc: doc.get("updated_at") or "", reverse=True)
        return documents

    def get_document(self, file_hash: str) -> Optional[dict[str, Any]]:
        for document in self.list_documents():
            if document["id"] == file_hash:
                return document
        return None

    def resolve_stem(self, file_hash: str) -> Optional[str]:
        """Return the output stem for a hash without building the full list.

        Resolves via the ledger first, then a cached scan of the input dir, so
        preview/download routes avoid re-hashing every input on each request.
        """
        path = self._resolve_source(file_hash)
        return path.stem if path is not None else None

    # --- Upload ------------------------------------------------------------
    def save_upload(self, filename: str, data: bytes) -> dict[str, Any]:
        """Persist an in-memory upload. Convenience wrapper over the streamer."""
        import io

        return self.save_upload_stream(filename, io.BytesIO(data))

    def save_upload_stream(
        self,
        filename: str,
        reader: BinaryIO,
        max_bytes: Optional[int] = None,
    ) -> dict[str, Any]:
        """Stream an upload to the input directory atomically.

        Copies ``reader`` in chunks to a temp file in the same directory, then
        ``os.replace`` into place so the watcher only ever sees a single,
        complete file. Colliding stems are de-duplicated so outputs (keyed by
        stem) are never clobbered. Aborts past ``max_bytes`` (default from
        settings) without leaving a partial file behind.
        """
        suffix = Path(filename).suffix.lower()
        if suffix not in self._settings.supported_suffixes:
            raise StorageError(
                f"Unsupported file type '{suffix}'. Allowed: "
                f"{', '.join(self._settings.supported_suffixes)}"
            )

        limit = (
            max_bytes
            if max_bytes is not None
            else self._settings.web_max_upload_mb * 1024 * 1024
        )

        self._repository.ensure_directories()
        input_dir = self._settings.input_dir
        target = self._unique_target(input_dir, filename)

        total = 0
        try:
            fd, tmp_name = tempfile.mkstemp(
                dir=str(input_dir), prefix=".upload-", suffix=suffix
            )
            try:
                with os.fdopen(fd, "wb") as handle:
                    while True:
                        chunk = reader.read(_UPLOAD_CHUNK)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > limit:
                            raise UploadTooLargeError(
                                f"Upload exceeds the {self._settings.web_max_upload_mb} "
                                "MB limit."
                            )
                        handle.write(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
                if total == 0:
                    raise StorageError("Uploaded file is empty.")
                os.replace(tmp_name, target)
            finally:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)
        except OSError as exc:
            raise StorageError(f"Cannot save upload {filename}: {exc}") from exc

        logger.info("web.upload", filename=target.name, bytes=total)
        self._enqueue(target)
        return {"filename": target.name, "stem": target.stem}

    # --- Output access -----------------------------------------------------
    def read_markdown(self, stem: str) -> str:
        return self._read_text(self._markdown_path(self._safe_stem(stem)))

    def read_metadata(self, stem: str) -> str:
        return self._read_text(self._metadata_path(self._safe_stem(stem)))

    def markdown_path(self, stem: str) -> Optional[Path]:
        path = self._markdown_path(self._safe_stem(stem))
        return path if path.is_file() else None

    def metadata_path(self, stem: str) -> Optional[Path]:
        path = self._metadata_path(self._safe_stem(stem))
        return path if path.is_file() else None

    # --- Management --------------------------------------------------------
    def retry(self, file_hash: str) -> dict[str, Any]:
        """Clear a document's ledger entry and re-enqueue its source file."""
        path = self._resolve_source(file_hash)
        if path is None or not path.is_file():
            raise StorageError(
                "Source file is no longer available; cannot reprocess."
            )
        self._state.delete(file_hash)
        self._invalidate_hash(path)
        self._enqueue(path)
        logger.info("web.retry", path=str(path), hash=file_hash[:12])
        return {"resubmitted": path.name}

    # Reprocessing a completed doc is mechanically identical to a retry.
    reprocess = retry

    def delete(self, file_hash: str) -> dict[str, Any]:
        """Remove a document everywhere: input file, outputs, and ledger."""
        path = self._resolve_source(file_hash)
        removed: list[str] = []

        if path is not None:
            stem = path.stem
            if path.is_file():
                path.unlink(missing_ok=True)
                self._invalidate_hash(path)
                removed.append(str(path))
            for output in (self._markdown_path(stem), self._metadata_path(stem)):
                if output.is_file():
                    output.unlink(missing_ok=True)
                    removed.append(str(output))

        if self._state.delete(file_hash):
            removed.append("ledger entry")

        logger.info("web.delete", hash=file_hash[:12], removed=removed)
        return {"removed": removed}

    # --- Internals ---------------------------------------------------------
    def _enqueue(self, path: Path) -> None:
        if self._submit_job is not None:
            try:
                self._submit_job(path)
            except Exception as exc:  # never let UI actions crash on the queue
                logger.error("web.enqueue.failed", path=str(path), error=repr(exc))

    def _resolve_source(self, file_hash: str) -> Optional[Path]:
        record = self._state.get(file_hash)
        if record is not None:
            return Path(record.source_path)
        for path in self._repository.iter_input_files():
            try:
                if self._cached_hash(path) == file_hash:
                    return path
            except StorageError:
                continue
        return None

    def _record_to_dict(self, file_hash: str, record: DocumentRecord) -> dict[str, Any]:
        stem = Path(record.source_path).stem
        return {
            "id": file_hash,
            "name": Path(record.source_path).name,
            "stem": stem,
            "state": record.state.value,
            "attempts": record.attempts,
            "error": record.error,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "has_markdown": self._markdown_path(stem).is_file(),
            "has_metadata": self._metadata_path(stem).is_file(),
        }

    def _queued_to_dict(self, file_hash: str, path: Path) -> dict[str, Any]:
        return {
            "id": file_hash,
            "name": path.name,
            "stem": path.stem,
            "state": QUEUED,
            "attempts": 0,
            "error": None,
            "created_at": None,
            "updated_at": None,
            "has_markdown": self._markdown_path(path.stem).is_file(),
            "has_metadata": self._metadata_path(path.stem).is_file(),
        }

    def _markdown_path(self, stem: str) -> Path:
        return self._settings.markdown_dir / f"{stem}.md"

    def _metadata_path(self, stem: str) -> Path:
        return self._settings.metadata_dir / f"{stem}.json"

    @staticmethod
    def _safe_stem(stem: str) -> str:
        """Reject stems that could escape the output directories."""
        if not stem or stem in {".", ".."} or set(stem) & set("/\\"):
            raise StorageError(f"Invalid document name: {stem!r}")
        return stem

    @staticmethod
    def _read_text(path: Path) -> str:
        if not path.is_file():
            raise StorageError(f"File not found: {path}")
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise StorageError(f"Cannot read {path}: {exc}") from exc

    @staticmethod
    def _unique_target(input_dir: Path, filename: str) -> Path:
        safe = Path(filename).name  # strip any path components
        candidate = input_dir / safe
        if not candidate.exists():
            return candidate
        stem, suffix = Path(safe).stem, Path(safe).suffix
        index = 1
        while True:
            candidate = input_dir / f"{stem} ({index}){suffix}"
            if not candidate.exists():
                return candidate
            index += 1

    def _cached_hash(self, path: Path) -> str:
        key = str(path)
        stat = path.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
        with self._cache_lock:
            cached = self._hash_cache.get(key)
            if cached is not None and cached[:2] == signature:
                return cached[2]
        file_hash = self._repository.compute_hash(path)
        with self._cache_lock:
            self._hash_cache[key] = (signature[0], signature[1], file_hash)
        return file_hash

    def _invalidate_hash(self, path: Path) -> None:
        with self._cache_lock:
            self._hash_cache.pop(str(path), None)
