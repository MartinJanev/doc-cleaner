"""Composition root.

The single place where concrete dependencies are instantiated and injected.
Everything below this module receives its collaborators via constructor
parameters, keeping the rest of the codebase free of global state and easy to
test.
"""

from __future__ import annotations

from docpipe.core.config import Settings
from docpipe.core.logging import configure_logging, get_logger
from docpipe.pipeline.processor import DocumentProcessor
from docpipe.services.extraction_service import ExtractionService
from docpipe.services.llm_service import LLMService
from docpipe.storage.file_repository import FileRepository
from docpipe.storage.state_store import JsonStateStore
from docpipe.watcher.job_queue import JobQueue
from docpipe.watcher.runner import WatcherRunner


def build_runner(settings: Settings) -> WatcherRunner:
    """Wire up every dependency and return a ready-to-start runner."""
    # Heavy third-party clients are imported lazily so unit tests and --help
    # do not pay the Docling/Ollama import cost.
    from docling.document_converter import DocumentConverter
    from ollama import Client

    logger = get_logger("docpipe.main")
    logger.info(
        "boot.config",
        input_dir=str(settings.input_dir),
        output_dir=str(settings.output_dir),
        model=settings.model_tag,
        ollama_host=settings.ollama_host,
        max_workers=settings.max_workers,
    )

    repository = FileRepository(
        input_dir=settings.input_dir,
        markdown_dir=settings.markdown_dir,
        metadata_dir=settings.metadata_dir,
        supported_suffixes=settings.supported_suffixes,
    )
    state_store = JsonStateStore(state_file=settings.state_file)

    extraction_service = ExtractionService(converter=DocumentConverter())
    ollama_client = Client(host=settings.ollama_host, timeout=settings.llm_timeout_s)
    llm_service = LLMService(
        client=ollama_client,
        model=settings.model_tag,
        max_chars=settings.llm_max_chars,
        temperature=settings.llm_temperature,
    )

    processor = DocumentProcessor(
        extraction_service=extraction_service,
        llm_service=llm_service,
        file_repository=repository,
        state_store=state_store,
        max_attempts=settings.max_attempts,
    )

    job_queue = JobQueue(handler=processor.process, max_workers=settings.max_workers)
    return WatcherRunner(
        input_dir=settings.input_dir,
        processor=processor,
        repository=repository,
        job_queue=job_queue,
        debounce_seconds=settings.debounce_seconds,
    )


def main() -> None:
    settings = Settings()
    configure_logging(level=settings.log_level, json_logs=settings.log_json)
    runner = build_runner(settings)
    runner.start()


if __name__ == "__main__":
    main()
