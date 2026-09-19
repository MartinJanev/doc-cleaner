"""Composition root.

The single place where concrete dependencies are instantiated and injected.
Everything below this module receives its collaborators via constructor
parameters, keeping the rest of the codebase free of global state and easy to
test.
"""

from __future__ import annotations

from dataclasses import dataclass

from docpipe.core.config import Settings
from docpipe.core.logging import (
    configure_logging,
    configure_uvicorn_access_logging,
    get_logger,
)
from docpipe.core.ports import Refiner
from docpipe.docling_comparator import DoclingComparator
from docpipe.pipeline.processor import DocumentProcessor
from docpipe.services.extraction_service import ExtractionService
from docpipe.services.llm_service import LLMService
from docpipe.storage.file_repository import FileRepository
from docpipe.storage.state_store import JsonStateStore
from docpipe.watcher.job_queue import JobQueue
from docpipe.watcher.runner import WatcherRunner


@dataclass
class Components:
    """The fully-wired collaborators shared by the watcher and the web UI."""

    repository: FileRepository
    state_store: JsonStateStore
    job_queue: JobQueue
    processor: DocumentProcessor
    runner: WatcherRunner
    llm_service: Refiner
    comparator: DoclingComparator


def build_components(settings: Settings) -> Components:
    """Instantiate and inject every concrete dependency in one place."""
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
        web_enabled=settings.web_enabled,
    )

    repository = FileRepository(
        input_dir=settings.input_dir,
        markdown_dir=settings.markdown_dir,
        metadata_dir=settings.metadata_dir,
        supported_suffixes=settings.supported_suffixes,
        raw_dir=settings.raw_dir,
        comparison_dir=settings.comparison_dir,
        keep_raw=settings.compare_enabled,
    )
    state_store = JsonStateStore(state_file=settings.state_file)

    extraction_service = ExtractionService(converter=DocumentConverter())
    ollama_client = Client(host=settings.ollama_host, timeout=settings.llm_timeout_s)
    llm_service = LLMService(
        client=ollama_client,
        model=settings.model_tag,
        max_chars=settings.llm_max_chars,
        chunk_chars=settings.llm_chunk_chars,
        num_ctx=settings.llm_num_ctx,
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
    runner = WatcherRunner(
        input_dir=settings.input_dir,
        processor=processor,
        repository=repository,
        job_queue=job_queue,
        debounce_seconds=settings.debounce_seconds,
    )
    comparator = DoclingComparator(
        repository=repository,
        model=settings.model_tag,
        interval_s=settings.compare_interval_s,
    )

    return Components(
        repository=repository,
        state_store=state_store,
        job_queue=job_queue,
        processor=processor,
        runner=runner,
        llm_service=llm_service,
        comparator=comparator,
    )


def main() -> None:
    settings = Settings()
    configure_logging(level=settings.log_level, json_logs=settings.log_json)
    components = build_components(settings)

    problem = components.llm_service.preflight()
    if problem is None:
        get_logger("docpipe.main").info("boot.preflight.ok", model=settings.model_tag)
    else:
        get_logger("docpipe.main").error("boot.preflight.failed", remedy=problem)

    # The comparator is out of band by design, so it hangs off whichever front
    # end happens to be the foreground process rather than off the watcher.
    if settings.compare_enabled:
        components.comparator.start_background()

    try:
        if not settings.web_enabled:
            components.runner.start()
            return

        import uvicorn

        from docpipe.web.app import create_app
        from docpipe.web.service import DocumentService

        service = DocumentService(
            settings=settings,
            repository=components.repository,
            state_store=components.state_store,
            submit_job=components.job_queue.submit,
            preflight=components.llm_service.preflight,
        )
        app = create_app(service, runner=components.runner)

        get_logger("docpipe.main").info(
            "web.serving", host=settings.web_host, port=settings.web_port
        )
        configure_uvicorn_access_logging()
        uvicorn.run(app, host=settings.web_host, port=settings.web_port)
    finally:
        components.comparator.stop()


if __name__ == "__main__":
    main()
