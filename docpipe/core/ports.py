"""Structural interfaces between the pipeline and its stages.

``DocumentProcessor`` only needs two things from the outside world: something
that turns a file into Markdown, and something that cleans that Markdown and
derives metadata. Typing against these ``Protocol``s rather than the concrete
``ExtractionService`` / ``LLMService`` is what lets a second backend -- an
OpenAI-compatible server, llama.cpp, a different extractor -- drop in without
editing the orchestration layer or the composition root's wiring shape.

They are structural, so implementations do not inherit from anything: matching
the method signature is enough. There is no runtime cost and no base class to
keep in sync.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from docpipe.models.documents import MarkdownDocument, RestructuredDocument


@runtime_checkable
class Extractor(Protocol):
    """Turns a source document into raw Markdown."""

    def extract(self, path: Path, file_hash: str) -> MarkdownDocument:
        """Convert ``path`` to Markdown.

        Raises:
            ExtractionError: If conversion fails or produces nothing usable.
        """
        ...


@runtime_checkable
class Refiner(Protocol):
    """Cleans extracted Markdown and derives document metadata."""

    def refine(self, document: MarkdownDocument) -> RestructuredDocument:
        """Clean ``document`` and attach YAML front matter.

        Raises:
            LLMServiceError: On transport failure or unparseable output.
        """
        ...

    def preflight(self) -> str | None:
        """Return ``None`` when the backend is ready, else why it is not.

        The message is shown to the operator at boot and on ``/health``, so it
        should name the command that fixes the problem.
        """
        ...
