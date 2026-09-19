"""Filesystem I/O controller.

Single responsibility: read input files, compute content hashes, enumerate the
watched directory, and route output artifacts to their destinations. It knows
nothing about extraction, LLMs, or state.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from pathlib import Path

from docpipe.core.exceptions import StorageError
from docpipe.models.documents import MarkdownDocument, RestructuredDocument

_HASH_CHUNK = 1 << 20  # 1 MiB


class FileRepository:
    """Reads inputs and writes Markdown + metadata outputs."""

    def __init__(
        self,
        input_dir: Path,
        markdown_dir: Path,
        metadata_dir: Path,
        supported_suffixes: tuple[str, ...],
        raw_dir: Path | None = None,
        comparison_dir: Path | None = None,
        keep_raw: bool = False,
    ) -> None:
        self._input_dir = input_dir
        self._markdown_dir = markdown_dir
        self._metadata_dir = metadata_dir
        self._supported = tuple(s.lower() for s in supported_suffixes)
        # Comparison artifacts live beside the real outputs but are optional, so
        # callers that do not care (tests, one-shot CLI runs) can leave them out.
        self._raw_dir = raw_dir if raw_dir is not None else markdown_dir.parent / "raw"
        self._comparison_dir = (
            comparison_dir
            if comparison_dir is not None
            else markdown_dir.parent / "comparison"
        )
        self._keep_raw = keep_raw

    def ensure_directories(self) -> None:
        """Create input/output directories if they do not yet exist."""
        directories = [self._input_dir, self._markdown_dir, self._metadata_dir]
        if self._keep_raw:
            directories += [self._raw_dir, self._comparison_dir]
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)

    # --- Input -------------------------------------------------------------
    def is_supported(self, path: Path) -> bool:
        return path.is_file() and path.suffix.lower() in self._supported

    def iter_input_files(self) -> Iterable[Path]:
        """Yield supported files currently present in the input directory."""
        if not self._input_dir.exists():
            return
        for path in sorted(self._input_dir.rglob("*")):
            if self.is_supported(path):
                yield path

    @staticmethod
    def compute_hash(path: Path) -> str:
        """Return the SHA-256 hex digest of a file's contents."""
        digest = hashlib.sha256()
        try:
            with path.open("rb") as handle:
                for block in iter(lambda: handle.read(_HASH_CHUNK), b""):
                    digest.update(block)
        except OSError as exc:
            raise StorageError(f"Cannot hash {path}: {exc}") from exc
        return digest.hexdigest()

    # --- Output ------------------------------------------------------------
    def output_key(self, source_path: Path) -> str:
        """Return a document's output identity: its path relative to the input dir.

        Inputs are enumerated recursively, so keying outputs by stem alone lets
        ``input/2024/report.pdf`` and ``input/2025/report.pdf`` overwrite each
        other. Mirroring the relative path keeps both, and keeps filenames
        readable. Sources outside the input tree fall back to their bare name.
        """
        for candidate, root in (
            (source_path, self._input_dir),
            (source_path.resolve(), self._input_dir.resolve()),
        ):
            try:
                return candidate.relative_to(root).with_suffix("").as_posix()
            except ValueError:
                continue
        return Path(source_path.name).with_suffix("").as_posix()

    def output_paths(self, key: str) -> tuple[Path, Path]:
        """Return the (markdown, metadata) destinations for an output key."""
        return self._markdown_dir / f"{key}.md", self._metadata_dir / f"{key}.json"

    def raw_path(self, key: str) -> Path:
        """Return where Docling's unrefined extraction is kept for an output key."""
        return self._raw_dir / f"{key}.md"

    def comparison_path(self, key: str) -> Path:
        """Return where the raw-vs-refined comparison is kept for an output key."""
        return self._comparison_dir / f"{key}.json"

    def iter_raw_keys(self) -> Iterable[str]:
        """Yield the output keys that have a preserved raw extraction."""
        if not self._raw_dir.exists():
            return
        for path in sorted(self._raw_dir.rglob("*.md")):
            yield path.relative_to(self._raw_dir).with_suffix("").as_posix()

    def iter_comparisons(self) -> Iterable[Path]:
        """Yield the per-document comparison files currently on disk."""
        if not self._comparison_dir.exists():
            return
        yield from sorted(self._comparison_dir.rglob("*.json"))

    def write_raw(self, document: MarkdownDocument) -> Path | None:
        """Persist Docling's output before the model touches it.

        The comparator needs a 'before' to score the model against, and the
        pipeline otherwise discards this text the moment refining succeeds.
        Returns None when comparison is disabled, in which case nothing reads
        the raw Markdown and writing it would only cost disk.
        """
        if not self._keep_raw:
            return None
        path = self.raw_path(self.output_key(document.source_path))
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.write_text(document.markdown, encoding="utf-8")
        except OSError as exc:
            raise StorageError(f"Cannot write raw extraction to {path}: {exc}") from exc
        return path

    def write_outputs(self, document: RestructuredDocument) -> tuple[Path, Path]:
        """Persist the refined Markdown and a metadata sidecar.

        Returns the (markdown_path, metadata_path) tuple.
        """
        self.ensure_directories()
        key = self.output_key(document.source_path)
        md_path, meta_path = self.output_paths(key)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            md_path.write_text(document.markdown_with_front_matter, encoding="utf-8")
            metadata_payload = {
                **document.metadata.to_dict(),
                "source_file": document.source_path.name,
                "file_hash": document.file_hash,
            }
            meta_path.write_text(
                json.dumps(metadata_payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as exc:
            raise StorageError(f"Cannot write outputs for {key}: {exc}") from exc

        return md_path, meta_path
