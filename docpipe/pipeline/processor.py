"""End-to-end orchestration for a single document.

``DocumentProcessor`` is the only place that knows the full sequence of stages
and the state transitions between them. Each stage lives in its own service and
is injected; the processor wires them together and owns the per-document error
boundary so one bad file never takes down the daemon.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from docpipe.core.exceptions import PipelineError
from docpipe.core.logging import get_logger
from docpipe.models.documents import ProcessingState, RestructuredDocument
from docpipe.services.extraction_service import ExtractionService
from docpipe.services.llm_service import LLMService
from docpipe.storage.file_repository import FileRepository
from docpipe.storage.state_store import JsonStateStore

logger = get_logger(__name__)


class DocumentProcessor:
    """Runs extraction -> refining -> persistence for one file."""

    def __init__(
        self,
        extraction_service: ExtractionService,
        llm_service: LLMService,
        file_repository: FileRepository,
        state_store: JsonStateStore,
        max_attempts: int,
    ) -> None:
        self._extraction = extraction_service
        self._llm = llm_service
        self._repository = file_repository
        self._state = state_store
        self._max_attempts = max_attempts

    def process(self, path: Path) -> Optional[RestructuredDocument]:
        """Process a single document, returning the artifact or None on skip/fail.

        Idempotent: a file whose content hash is already COMPLETED is skipped.
        Any failure is recorded as FAILED and swallowed (logged) so the worker
        pool keeps running.
        """
        if not self._repository.is_supported(path):
            logger.debug("processor.skip.unsupported", path=str(path))
            return None

        try:
            file_hash = self._repository.compute_hash(path)
        except PipelineError as exc:
            logger.error("processor.hash.failed", path=str(path), error=str(exc))
            return None

        if not self._state.should_process(file_hash, self._max_attempts):
            logger.info("processor.skip.completed", path=str(path), hash=file_hash[:12])
            return None

        log = logger.bind(path=str(path), hash=file_hash[:12])
        self._state.start(file_hash, str(path))

        try:
            document = self._extraction.extract(path, file_hash)
            self._state.mark(file_hash, ProcessingState.EXTRACTED)

            restructured = self._llm.refine(document)
            self._state.mark(file_hash, ProcessingState.RESTRUCTURED)

            md_path, meta_path = self._repository.write_outputs(restructured)
            self._state.mark(file_hash, ProcessingState.COMPLETED)

            log.info(
                "processor.completed",
                markdown=str(md_path),
                metadata=str(meta_path),
            )
            return restructured
        except PipelineError as exc:
            self._state.mark(file_hash, ProcessingState.FAILED, error=str(exc))
            log.error("processor.failed", error=str(exc))
            return None
        except Exception as exc:  # defensive: never let the worker thread die
            self._state.mark(file_hash, ProcessingState.FAILED, error=repr(exc))
            log.error("processor.unexpected", error=repr(exc))
            return None
