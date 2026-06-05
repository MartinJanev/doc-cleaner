"""Unit tests for filesystem routing and hashing."""

from __future__ import annotations

import json
from pathlib import Path

from docpipe.models.documents import (
    DocumentMetadata,
    RestructuredDocument,
)
from docpipe.storage.file_repository import FileRepository


def _make_repo(tmp_path: Path) -> FileRepository:
    return FileRepository(
        input_dir=tmp_path / "input",
        markdown_dir=tmp_path / "out" / "markdown",
        metadata_dir=tmp_path / "out" / "metadata",
        supported_suffixes=(".pdf", ".docx"),
    )


def test_is_supported_filters_by_suffix(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (tmp_path / "input").mkdir(parents=True)
    pdf = tmp_path / "input" / "a.pdf"
    txt = tmp_path / "input" / "b.txt"
    pdf.write_bytes(b"%PDF-1.4")
    txt.write_text("hi")
    assert repo.is_supported(pdf) is True
    assert repo.is_supported(txt) is False


def test_iter_input_files_only_supported(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (tmp_path / "input").mkdir(parents=True)
    (tmp_path / "input" / "a.pdf").write_bytes(b"x")
    (tmp_path / "input" / "b.docx").write_bytes(b"y")
    (tmp_path / "input" / "c.txt").write_text("z")
    names = sorted(p.name for p in repo.iter_input_files())
    assert names == ["a.pdf", "b.docx"]


def test_compute_hash_is_content_based(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (tmp_path / "input").mkdir(parents=True)
    f1 = tmp_path / "input" / "a.pdf"
    f2 = tmp_path / "input" / "b.pdf"
    f1.write_bytes(b"same-bytes")
    f2.write_bytes(b"same-bytes")
    assert repo.compute_hash(f1) == repo.compute_hash(f2)


def test_write_outputs_routes_md_and_metadata(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    document = RestructuredDocument(
        source_path=Path("/in/report.pdf"),
        file_hash="abc123",
        metadata=DocumentMetadata(
            title="Report",
            summary="A summary.",
            author="Jane",
            tags=["finance", "q1"],
        ),
        markdown_with_front_matter="---\ntitle: \"Report\"\n---\n# Body\n",
    )
    md_path, meta_path = repo.write_outputs(document)

    assert md_path == tmp_path / "out" / "markdown" / "report.md"
    assert meta_path == tmp_path / "out" / "metadata" / "report.json"
    assert "# Body" in md_path.read_text()

    meta = json.loads(meta_path.read_text())
    assert meta["title"] == "Report"
    assert meta["source_file"] == "report.pdf"
    assert meta["file_hash"] == "abc123"
    assert meta["tags"] == ["finance", "q1"]
