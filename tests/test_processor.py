"""Tests for the pipeline orchestration and its per-document error boundary.

Extraction and refining are faked, so this exercises the state machine and the
"one bad file must never kill the worker" guarantee without Docling or Ollama.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from docpipe.core.exceptions import ExtractionError, LLMServiceError
from docpipe.models.documents import (
    DocumentMetadata,
    MarkdownDocument,
    ProcessingState,
    RestructuredDocument,
)
from docpipe.pipeline.processor import DocumentProcessor
from docpipe.storage.file_repository import FileRepository
from docpipe.storage.state_store import JsonStateStore


class FakeExtraction:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0

    def extract(self, path: Path, file_hash: str) -> MarkdownDocument:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return MarkdownDocument(source_path=path, file_hash=file_hash, markdown="# Raw")


class FakeLLM:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0

    def refine(self, document: MarkdownDocument) -> RestructuredDocument:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return RestructuredDocument(
            source_path=document.source_path,
            file_hash=document.file_hash,
            metadata=DocumentMetadata(title="T", summary="S", author="A", tags=["t"]),
            markdown_with_front_matter="---\ntitle: \"T\"\n---\n\n# Clean\n",
        )


@pytest.fixture()
def env(tmp_path: Path) -> dict[str, Any]:
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    repository = FileRepository(
        input_dir=input_dir,
        markdown_dir=tmp_path / "out" / "markdown",
        metadata_dir=tmp_path / "out" / "metadata",
        supported_suffixes=(".pdf",),
    )
    repository.ensure_directories()
    source = input_dir / "report.pdf"
    source.write_bytes(b"%PDF-1.4 fake")
    return {
        "repository": repository,
        "state": JsonStateStore(state_file=tmp_path / "state.json"),
        "source": source,
        "tmp_path": tmp_path,
    }


def build(env: dict[str, Any], extraction: Any, llm: Any) -> DocumentProcessor:
    return DocumentProcessor(
        extraction_service=extraction,
        llm_service=llm,
        file_repository=env["repository"],
        state_store=env["state"],
        max_attempts=3,
    )


def record_for(env: dict[str, Any]) -> Any:
    file_hash = env["repository"].compute_hash(env["source"])
    return env["state"].get(file_hash)


def test_happy_path_reaches_completed_and_writes_both_outputs(env) -> None:
    processor = build(env, FakeExtraction(), FakeLLM())
    result = processor.process(env["source"])

    assert result is not None
    assert record_for(env).state is ProcessingState.COMPLETED
    assert (env["tmp_path"] / "out" / "markdown" / "report.md").is_file()
    assert (env["tmp_path"] / "out" / "metadata" / "report.json").is_file()


def test_extraction_failure_is_recorded_and_swallowed(env) -> None:
    processor = build(env, FakeExtraction(ExtractionError("docling blew up")), FakeLLM())

    assert processor.process(env["source"]) is None  # never raises at the worker
    record = record_for(env)
    assert record.state is ProcessingState.FAILED
    assert "docling blew up" in record.error


def test_llm_failure_is_recorded_and_swallowed(env) -> None:
    processor = build(env, FakeExtraction(), FakeLLM(LLMServiceError("bad json")))

    assert processor.process(env["source"]) is None
    assert record_for(env).state is ProcessingState.FAILED


def test_unexpected_exception_does_not_escape_the_processor(env) -> None:
    """A bug in a stage must mark FAILED, not kill the worker thread."""
    processor = build(env, FakeExtraction(), FakeLLM(ValueError("unexpected")))

    assert processor.process(env["source"]) is None
    record = record_for(env)
    assert record.state is ProcessingState.FAILED
    assert "unexpected" in record.error


def test_completed_document_is_skipped_on_reprocess(env) -> None:
    extraction, llm = FakeExtraction(), FakeLLM()
    processor = build(env, extraction, llm)
    processor.process(env["source"])
    processor.process(env["source"])

    assert extraction.calls == 1  # second run short-circuits on the ledger
    assert llm.calls == 1


def test_failed_document_stops_after_the_attempt_budget(env) -> None:
    extraction = FakeExtraction(ExtractionError("nope"))
    processor = DocumentProcessor(
        extraction_service=extraction,
        llm_service=FakeLLM(),
        file_repository=env["repository"],
        state_store=env["state"],
        max_attempts=2,
    )
    for _ in range(5):
        processor.process(env["source"])

    assert extraction.calls == 2
    assert record_for(env).attempts == 2


def test_unsupported_file_is_ignored(env) -> None:
    extraction = FakeExtraction()
    processor = build(env, extraction, FakeLLM())
    other = env["source"].with_suffix(".txt")
    other.write_bytes(b"plain")

    assert processor.process(other) is None
    assert extraction.calls == 0
