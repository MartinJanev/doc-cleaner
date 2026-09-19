"""Command-line entry point.

``run`` is the daemon the project has always had. The other subcommands exist so
the pipeline can be driven, demoed and debugged without a browser -- and, in the
case of ``--dry-run``, without a model at all.

Heavy imports stay inside the subcommand that needs them, so ``docpipe status``
and ``docpipe --help`` stay instant even though Docling is a multi-second
import.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from docpipe import __version__
from docpipe.core.config import Settings
from docpipe.core.logging import configure_logging, get_logger
from docpipe.models.documents import ProcessingState

if TYPE_CHECKING:  # heavy imports stay inside the subcommands that need them
    from docpipe.storage.file_repository import FileRepository


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="docpipe",
        description="Local-first PDF/DOCX to clean Markdown pipeline for RAG.",
    )
    parser.add_argument("--version", action="version", version=f"docpipe {__version__}")
    subcommands = parser.add_subparsers(dest="command")

    run = subcommands.add_parser(
        "run", help="Watch the input directory (and serve the web UI)."
    )
    run.add_argument(
        "--once",
        action="store_true",
        help="Process everything already in the input directory, then exit.",
    )

    process = subcommands.add_parser("process", help="Process a single file and exit.")
    process.add_argument("path", type=Path, help="The .pdf or .docx to process.")
    process.add_argument(
        "--dry-run",
        action="store_true",
        help="Extract only, skipping the LLM. Needs no Ollama server.",
    )

    subcommands.add_parser("status", help="Summarise the processing ledger.")

    compare = subcommands.add_parser(
        "compare",
        help="Score how much the model changed Docling's extraction.",
    )
    compare.add_argument(
        "--report-only",
        action="store_true",
        help="Summarise existing scores without scoring anything new.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings()
    configure_logging(level=settings.log_level, json_logs=settings.log_json)

    if args.command == "status":
        return _status(settings)
    if args.command == "compare":
        return _compare(settings, report_only=args.report_only)
    if args.command == "process":
        return _process(settings, args.path, dry_run=args.dry_run)
    if args.command == "run" and args.once:
        return _run_once(settings)

    # No subcommand, or a plain `run`: the daemon, unchanged.
    from docpipe.main import main as run_daemon

    run_daemon()
    return 0


def _status(settings: Settings) -> int:
    """Print ledger counts and any recent failures."""
    from docpipe.storage.state_store import JsonStateStore

    records = JsonStateStore(state_file=settings.state_file).records()
    if not records:
        print(f"No documents recorded yet ({settings.state_file}).")
        return 0

    counts = {state: 0 for state in ProcessingState}
    for record in records.values():
        counts[record.state] += 1

    print(f"{len(records)} document(s) in {settings.state_file}\n")
    for state, count in counts.items():
        if count:
            print(f"  {state.value:<14} {count}")

    failures = [r for r in records.values() if r.state is ProcessingState.FAILED]
    if failures:
        print("\nFailures:")
        for record in sorted(failures, key=lambda r: r.updated_at, reverse=True)[:10]:
            print(f"  {Path(record.source_path).name}: {record.error}")
    return 1 if failures else 0


def _process(settings: Settings, path: Path, dry_run: bool) -> int:
    """Run one file through the pipeline synchronously."""
    logger = get_logger("docpipe.cli")

    if not path.is_file():
        print(f"No such file: {path}", file=sys.stderr)
        return 2

    from docling.document_converter import DocumentConverter

    from docpipe.services.extraction_service import ExtractionService

    extraction = ExtractionService(converter=DocumentConverter())
    file_hash = _repository(settings).compute_hash(path)

    if dry_run:
        # The point of --dry-run is to prove extraction works with no model
        # running, so nothing below may touch Ollama.
        document = extraction.extract(path, file_hash)
        print(document.markdown)
        return 0

    from ollama import Client

    from docpipe.pipeline.processor import DocumentProcessor
    from docpipe.services.llm_service import LLMService
    from docpipe.storage.state_store import JsonStateStore

    llm = LLMService(
        client=Client(host=settings.ollama_host, timeout=settings.llm_timeout_s),
        model=settings.model_tag,
        max_chars=settings.llm_max_chars,
        chunk_chars=settings.llm_chunk_chars,
        num_ctx=settings.llm_num_ctx,
        temperature=settings.llm_temperature,
    )
    problem = llm.preflight()
    if problem is not None:
        print(problem, file=sys.stderr)
        return 3

    repository = _repository(settings)
    processor = DocumentProcessor(
        extraction_service=extraction,
        llm_service=llm,
        file_repository=repository,
        state_store=JsonStateStore(state_file=settings.state_file),
        max_attempts=settings.max_attempts,
    )
    result = processor.process(path)
    if result is None:
        logger.error("cli.process.failed", path=str(path))
        print(
            f"Failed (see the log above, or run `docpipe status`): {path}",
            file=sys.stderr,
        )
        return 1

    md_path, meta_path = repository.output_paths(repository.output_key(path))
    print(f"{md_path}\n{meta_path}")
    return 0


def _compare(settings: Settings, report_only: bool) -> int:
    """Score unscored documents, then print the fleet-wide averages.

    Needs neither Docling nor Ollama: comparison reads files the pipeline has
    already written.
    """
    from docpipe.docling_comparator import SCORE_FIELDS, DoclingComparator

    comparator = DoclingComparator(
        repository=_repository(settings),
        model=settings.model_tag,
        interval_s=settings.compare_interval_s,
    )
    if not report_only:
        print(f"Scored {comparator.sweep()} new document(s).")

    report = comparator.report()
    if not report["documents"]:
        print(
            "No comparisons yet. Documents processed before comparison was "
            "enabled have no preserved extraction to score against; reprocess "
            "one to produce the first."
        )
        return 0

    models = ", ".join(report["models"])
    print(f"\n{report['documents']} document(s) compared  ·  model: {models}")
    for field in SCORE_FIELDS:
        print(f"  {field:<15} {report['means'][field]:.4f}")

    print("\nMost changed by the model (highest share of new text):")
    for entry in report["most_changed"]:
        print(
            f"  {entry['introduced']:.4f} introduced  "
            f"{entry['similarity']:.4f} similar   {entry['key']}"
        )
    return 0


def _run_once(settings: Settings) -> int:
    """Drain the input directory, then stop."""
    from docpipe.main import build_components

    components = build_components(settings)
    components.repository.ensure_directories()
    components.job_queue.start()
    enqueued = components.runner.reconcile()
    components.job_queue.join()
    components.job_queue.shutdown()
    print(f"Processed {enqueued} queued document(s).")
    return 0


def _repository(settings: Settings) -> FileRepository:
    from docpipe.storage.file_repository import FileRepository

    return FileRepository(
        input_dir=settings.input_dir,
        markdown_dir=settings.markdown_dir,
        metadata_dir=settings.metadata_dir,
        supported_suffixes=settings.supported_suffixes,
        raw_dir=settings.raw_dir,
        comparison_dir=settings.comparison_dir,
        keep_raw=settings.compare_enabled,
    )


if __name__ == "__main__":
    raise SystemExit(main())
