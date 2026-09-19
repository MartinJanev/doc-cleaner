"""Unit tests for the raw-vs-refined comparison.

The scoring half is pure, so it is exercised on literal strings. The sweep half
touches only the filesystem -- no Docling, no Ollama -- so it runs against
tmp_path directly.
"""

from __future__ import annotations

import json
from pathlib import Path

from docpipe.docling_comparator import (
    DoclingComparator,
    compare,
    measure,
    score,
    strip_front_matter,
)
from docpipe.models.documents import MarkdownDocument
from docpipe.services.prompts import render_front_matter
from docpipe.storage.file_repository import FileRepository

BODY = (
    "# Quarterly Report\n\n"
    "Revenue grew by twelve percent across the northern region.\n"
    "Costs held flat against the previous quarter.\n"
)
FRONT_MATTER = '---\ntitle: "Quarterly Report"\nauthor: "Unknown"\ntags: []\n---\n\n'


def _make_repo(tmp_path: Path, keep_raw: bool = True) -> FileRepository:
    return FileRepository(
        input_dir=tmp_path / "input",
        markdown_dir=tmp_path / "out" / "markdown",
        metadata_dir=tmp_path / "out" / "metadata",
        supported_suffixes=(".pdf", ".docx"),
        raw_dir=tmp_path / "out" / "raw",
        comparison_dir=tmp_path / "out" / "comparison",
        keep_raw=keep_raw,
    )


def _place(repo: FileRepository, key: str, raw: str, refined: str | None) -> None:
    """Put a raw extraction (and optionally its refined output) on disk."""
    raw_path = repo.raw_path(key)
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(raw, encoding="utf-8")
    if refined is None:
        return
    markdown_path, metadata_path = repo.output_paths(key)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(refined, encoding="utf-8")
    metadata_path.write_text(json.dumps({"file_hash": "deadbeef"}), encoding="utf-8")


# --- Scoring ---------------------------------------------------------------
def test_identical_text_scores_as_untouched() -> None:
    scores = score(BODY, BODY)
    assert scores.similarity == 1.0
    assert scores.retention == 1.0
    assert scores.introduced == 0.0
    assert scores.char_reduction == 0.0


def test_front_matter_is_not_counted_as_invented_text() -> None:
    """The model's metadata block is not prose it added to the document."""
    comparison = compare("k", BODY, FRONT_MATTER + BODY, "hash", "test-model")
    assert comparison.scores.introduced == 0.0
    assert comparison.scores.similarity == 1.0


def test_strip_front_matter_leaves_a_plain_body_alone() -> None:
    assert strip_front_matter(BODY) == BODY
    assert strip_front_matter(FRONT_MATTER + BODY) == BODY


def test_strip_front_matter_matches_what_the_pipeline_actually_writes() -> None:
    """Pinned against the real renderer so a format change cannot drift past."""
    rendered = render_front_matter({"title": "T", "tags": ["a"]}, "report.pdf")
    assert strip_front_matter(rendered + BODY) == BODY


def test_removing_boilerplate_drops_retention_but_invents_nothing() -> None:
    raw = "Page 1 of 9\n\n" + BODY + "\nConfidential draft footer\n"
    scores = score(raw, BODY)
    assert scores.introduced == 0.0, "nothing was added, only removed"
    assert scores.retention < 1.0, "the removed words are gone from the output"
    assert scores.char_reduction > 0.0


def test_invented_prose_shows_up_as_introduced() -> None:
    """The signal that separates good cleanup from quiet rewriting."""
    rewritten = BODY + "\nThe board unanimously approved an acquisition.\n"
    assert score(BODY, rewritten).introduced > 0.0


def test_empty_extraction_does_not_divide_by_zero() -> None:
    scores = score("", BODY)
    assert scores.retention == 0.0
    assert scores.char_reduction == 0.0
    assert scores.introduced == 1.0


def test_measure_counts_structure() -> None:
    markdown = (
        "# One\n## Two\n\n"
        "| a | b |\n| - | - |\n| 1 | 2 |\n\n"
        "See [the docs](https://example.com).\n\n"
        "```python\nprint(1)\n```\n"
    )
    stats = measure(markdown)
    assert stats.headings == 2
    assert stats.table_rows == 3
    assert stats.links == 1
    assert stats.code_blocks == 1


# --- Sweeping --------------------------------------------------------------
def test_sweep_scores_a_pending_pair_and_writes_the_record(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _place(repo, "report", BODY, FRONT_MATTER + BODY)
    comparator = DoclingComparator(repo, model="test-model", interval_s=0.01)

    assert comparator.sweep() == 1

    record = json.loads(repo.comparison_path("report").read_text(encoding="utf-8"))
    assert record["key"] == "report"
    assert record["file_hash"] == "deadbeef"
    assert record["model"] == "test-model"
    assert record["scores"]["similarity"] == 1.0


def test_sweep_is_idempotent(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _place(repo, "report", BODY, FRONT_MATTER + BODY)
    comparator = DoclingComparator(repo, model="test-model")

    assert comparator.sweep() == 1
    assert comparator.sweep() == 0, "an already-scored document is not rescored"


def test_sweep_rescores_when_the_output_is_rewritten(tmp_path: Path) -> None:
    """Reprocessing a document has to invalidate its old score."""
    repo = _make_repo(tmp_path)
    _place(repo, "report", BODY, FRONT_MATTER + BODY)
    comparator = DoclingComparator(repo, model="test-model")
    comparator.sweep()

    markdown_path, _ = repo.output_paths("report")
    markdown_path.write_text(FRONT_MATTER + BODY + "\nA new closing line.\n")
    comparison_path = repo.comparison_path("report")
    import os

    stale = comparison_path.stat().st_mtime - 60
    os.utime(comparison_path, (stale, stale))

    assert comparator.sweep() == 1
    record = json.loads(comparison_path.read_text(encoding="utf-8"))
    assert record["scores"]["introduced"] > 0.0


def test_sweep_skips_documents_that_never_reached_the_model(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _place(repo, "half-done", BODY, refined=None)
    assert DoclingComparator(repo, model="test-model").sweep() == 0


def test_sweep_handles_nested_keys(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _place(repo, "2024/report", BODY, FRONT_MATTER + BODY)
    _place(repo, "2025/report", BODY, FRONT_MATTER + BODY)
    assert DoclingComparator(repo, model="test-model").sweep() == 2
    assert repo.comparison_path("2024/report").exists()
    assert repo.comparison_path("2025/report").exists()


def test_report_aggregates_and_ranks_by_introduced_text(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _place(repo, "clean", BODY, FRONT_MATTER + BODY)
    _place(repo, "rewritten", BODY, FRONT_MATTER + BODY + "\nAn invented sentence.\n")
    comparator = DoclingComparator(repo, model="test-model")
    comparator.sweep()

    report = comparator.report()
    assert report["documents"] == 2
    assert report["models"] == ["test-model"]
    assert set(report["means"]) == {
        "similarity",
        "jaccard",
        "retention",
        "introduced",
        "char_reduction",
    }
    assert report["most_changed"][0]["key"] == "rewritten"


def test_report_is_empty_before_anything_is_scored(tmp_path: Path) -> None:
    report = DoclingComparator(_make_repo(tmp_path), model="test-model").report()
    assert report == {"documents": 0, "models": [], "means": {}, "most_changed": []}


# --- Preserving the extraction ---------------------------------------------
def test_write_raw_mirrors_the_input_subpath(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    document = MarkdownDocument(
        source_path=tmp_path / "input" / "2024" / "report.pdf",
        file_hash="abc",
        markdown=BODY,
    )
    written = repo.write_raw(document)
    assert written == tmp_path / "out" / "raw" / "2024" / "report.md"
    assert written.read_text(encoding="utf-8") == BODY


def test_write_raw_is_a_no_op_when_comparison_is_disabled(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, keep_raw=False)
    document = MarkdownDocument(
        source_path=tmp_path / "input" / "report.pdf",
        file_hash="abc",
        markdown=BODY,
    )
    assert repo.write_raw(document) is None
    assert not (tmp_path / "out" / "raw").exists()


def test_background_sweep_runs_and_stops(tmp_path: Path) -> None:
    """The lifecycle the daemon actually uses: start, score, stop."""
    import time

    repo = _make_repo(tmp_path)
    _place(repo, "report", BODY, FRONT_MATTER + BODY)
    comparator = DoclingComparator(repo, model="test-model", interval_s=0.01)

    comparator.start_background()
    try:
        deadline = time.monotonic() + 5.0
        while not repo.comparison_path("report").exists():
            if time.monotonic() > deadline:
                raise AssertionError("the background sweep never scored the document")
            time.sleep(0.01)
    finally:
        comparator.stop()

    assert comparator._thread is None
    assert comparator.sweep() == 0, "the background pass already scored it"
