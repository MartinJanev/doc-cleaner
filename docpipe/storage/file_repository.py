"""Filesystem I/O controller.

Single responsibility: read input files, compute content hashes, enumerate the
watched directory, and route output artifacts to their destinations. It knows
nothing about extraction, LLMs, or state.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Iterable

from docpipe.core.exceptions import StorageError
from docpipe.models.documents import RestructuredDocument

_HASH_CHUNK = 1 << 20  # 1 MiB


class FileRepository:
    """Reads inputs and writes Markdown + metadata outputs."""

    def __init__(
        self,
        input_dir: Path,
        markdown_dir: Path,
        metadata_dir: Path,
        supported_suffixes: tuple[str, ...],
    ) -> None:
        self._input_dir = input_dir
        self._markdown_dir = markdown_dir
        self._metadata_dir = metadata_dir
        self._supported = tuple(s.lower() for s in supported_suffixes)

    def ensure_directories(self) -> None:
        """Create input/output directories if they do not yet exist."""
        for directory in (self._input_dir, self._markdown_dir, self._metadata_dir):
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
