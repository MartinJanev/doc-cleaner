"""Layout-aware extraction wrapping IBM Docling.

The Docling ``DocumentConverter`` is injected so it can be configured (or
mocked) by the composition root. Docling natively reconstructs multi-column
layouts and emits tables as Markdown pipe tables, which we preserve verbatim.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from docpipe.core.exceptions import ExtractionError
from docpipe.core.logging import get_logger
from docpipe.models.documents import MarkdownDocument

if TYPE_CHECKING:  # avoid importing the heavy dependency at module load time
    from docling.document_converter import DocumentConverter

logger = get_logger(__name__)


class ExtractionService:
    """Converts a source document into layout-faithful Markdown."""

    def __init__(self, converter: "DocumentConverter") -> None:
        self._converter = converter

    def extract(self, path: Path, file_hash: str) -> MarkdownDocument:
        """Run Docling on ``path`` and return its Markdown representation.

        Args:
            path: Absolute path to the source document.
            file_hash: Pre-computed content hash, carried through for the ledger.

        Raises:
            ExtractionError: If conversion fails or yields empty Markdown.
        """
        logger.info("extraction.start", path=str(path))
        try:
            result = self._converter.convert(str(path))
            markdown = result.document.export_to_markdown()
        except Exception as exc:
            raise ExtractionError(f"Docling failed on {path.name}: {exc}") from exc

        if not markdown or not markdown.strip():
            raise ExtractionError(f"Docling produced empty Markdown for {path.name}")

        logger.info("extraction.done", path=str(path), chars=len(markdown))
        return MarkdownDocument(
            source_path=path,
            file_hash=file_hash,
            markdown=markdown,
        )
