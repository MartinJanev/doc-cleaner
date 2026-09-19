"""Tests for the web service + FastAPI routes (no Docling/Ollama needed)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from docpipe.core.config import Settings
from docpipe.core.exceptions import StorageError
from docpipe.models.documents import ProcessingState
from docpipe.storage.file_repository import FileRepository
from docpipe.storage.state_store import JsonStateStore
from docpipe.web.app import create_app
from docpipe.web.service import DocumentService


@pytest.fixture()
def env(tmp_path: Path):
    settings = Settings(
        input_dir=tmp_path / "input",
        output_dir=tmp_path / "output",
        state_file=tmp_path / "state.json",
        web_max_upload_mb=1,
    )
    repository = FileRepository(
        input_dir=settings.input_dir,
        markdown_dir=settings.markdown_dir,
        metadata_dir=settings.metadata_dir,
        supported_suffixes=settings.supported_suffixes,
    )
    repository.ensure_directories()
    state_store = JsonStateStore(state_file=settings.state_file)
    submitted: list[Path] = []
    service = DocumentService(
        settings=settings,
        repository=repository,
        state_store=state_store,
        submit_job=lambda path: submitted.append(path) or True,
    )
    client = TestClient(create_app(service))
    return {
        "settings": settings,
        "repository": repository,
        "state_store": state_store,
        "service": service,
        "submitted": submitted,
        "client": client,
    }


def _seed_completed(env, name: str = "report.pdf") -> str:
    """Create an input file + COMPLETED ledger entry + output artifacts."""
    settings = env["settings"]
    repository = env["repository"]
    state_store = env["state_store"]

    source = settings.input_dir / name
    source.write_bytes(b"%PDF-1.4 fake")
    file_hash = repository.compute_hash(source)
    state_store.start(file_hash, str(source))
    state_store.mark(file_hash, ProcessingState.COMPLETED)

    stem = source.stem
    (settings.markdown_dir / f"{stem}.md").write_text(
        "# Clean\n\nBody text.", encoding="utf-8"
    )
    (settings.metadata_dir / f"{stem}.json").write_text(
        '{"title": "Clean"}', encoding="utf-8"
    )
    return file_hash


def test_upload_lands_complete_file(env) -> None:
    client = env["client"]
    res = client.post(
        "/api/upload",
        files={"file": ("doc.pdf", b"%PDF-1.4 data", "application/pdf")},
    )
    assert res.status_code == 200
    assert res.json()["filename"] == "doc.pdf"

    saved = env["settings"].input_dir / "doc.pdf"
    assert saved.is_file()
    assert saved.read_bytes() == b"%PDF-1.4 data"
    # no leftover temp files
    leftovers = list(env["settings"].input_dir.iterdir())
    assert not any(p.name.startswith(".upload-") for p in leftovers)


def test_upload_rejects_unsupported_type(env) -> None:
    res = env["client"].post(
        "/api/upload",
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )
    assert res.status_code == 400


def test_upload_rejects_empty_file(env) -> None:
    res = env["client"].post(
        "/api/upload",
        files={"file": ("empty.pdf", b"", "application/pdf")},
    )
    assert res.status_code == 400
    assert not any(env["settings"].input_dir.iterdir())


def test_upload_rejects_oversize_file(env) -> None:
    # Fixture cap is 1 MB; send 2 MB.
    big = b"x" * (2 * 1024 * 1024)
    res = env["client"].post(
        "/api/upload",
        files={"file": ("big.pdf", big, "application/pdf")},
    )
    assert res.status_code == 413
    # The aborted upload must not leave a file (temp or final) behind.
    assert not any(env["settings"].input_dir.iterdir())


def test_upload_dedupes_colliding_names(env) -> None:
    client = env["client"]
    client.post("/api/upload", files={"file": ("doc.pdf", b"one", "application/pdf")})
    second = client.post(
        "/api/upload", files={"file": ("doc.pdf", b"two", "application/pdf")}
    )
    assert second.json()["filename"] == "doc (1).pdf"
    names = {p.name for p in env["settings"].input_dir.iterdir()}
    assert {"doc.pdf", "doc (1).pdf"} <= names


def test_list_shows_queued_input(env) -> None:
    (env["settings"].input_dir / "fresh.pdf").write_bytes(b"%PDF data")
    docs = env["client"].get("/api/documents").json()["documents"]
    assert len(docs) == 1
    assert docs[0]["name"] == "fresh.pdf"
    assert docs[0]["state"] == "QUEUED"


def test_list_and_preview_completed(env) -> None:
    file_hash = _seed_completed(env)
    docs = env["client"].get("/api/documents").json()["documents"]
    doc = next(d for d in docs if d["id"] == file_hash)
    assert doc["state"] == "COMPLETED"
    assert doc["has_markdown"] is True
    assert doc["has_metadata"] is True

    preview = env["client"].get(f"/api/documents/{file_hash}/markdown")
    assert preview.status_code == 200
    assert "Body text." in preview.text


def test_resolve_key_for_ledger_and_queued(env) -> None:
    file_hash = _seed_completed(env, name="report.pdf")
    assert env["service"].resolve_key(file_hash) == "report"

    queued = env["settings"].input_dir / "fresh.pdf"
    queued.write_bytes(b"%PDF queued")
    queued_hash = env["repository"].compute_hash(queued)
    assert env["service"].resolve_key(queued_hash) == "fresh"

    assert env["service"].resolve_key("does-not-exist") is None


def test_download_endpoints_return_files(env) -> None:
    file_hash = _seed_completed(env)
    md = env["client"].get(f"/download/markdown/{file_hash}")
    assert md.status_code == 200
    assert "# Clean" in md.text

    meta = env["client"].get(f"/download/metadata/{file_hash}")
    assert meta.status_code == 200
    assert "Clean" in meta.text


def test_preview_unknown_returns_404(env) -> None:
    assert env["client"].get("/api/documents/ghost/markdown").status_code == 404


def test_favicon_served(env) -> None:
    res = env["client"].get("/favicon.svg")
    assert res.status_code == 200
    assert "svg" in res.headers["content-type"]


def test_delete_removes_everything(env) -> None:
    file_hash = _seed_completed(env)
    res = env["client"].delete(f"/api/documents/{file_hash}")
    assert res.status_code == 200

    settings = env["settings"]
    assert not (settings.input_dir / "report.pdf").exists()
    assert not (settings.markdown_dir / "report.md").exists()
    assert not (settings.metadata_dir / "report.json").exists()
    assert env["state_store"].get(file_hash) is None


def test_reprocess_clears_entry_and_submits(env) -> None:
    file_hash = _seed_completed(env)
    res = env["client"].post(f"/api/documents/{file_hash}/reprocess")
    assert res.status_code == 200

    assert env["state_store"].get(file_hash) is None
    assert env["state_store"].should_process(file_hash, max_attempts=3) is True
    assert env["submitted"] == [env["settings"].input_dir / "report.pdf"]


def test_retry_missing_source_fails(env) -> None:
    state_store = env["state_store"]
    state_store.start("ghost", "/gone/missing.pdf")
    state_store.mark("ghost", ProcessingState.FAILED, error="boom")

    res = env["client"].post("/api/documents/ghost/retry")
    assert res.status_code == 400
    assert env["submitted"] == []


def test_upload_temp_file_is_invisible_to_the_watcher(env) -> None:
    """The in-progress upload must not look like a processable input.

    The temp file is created inside the watched directory. Giving it the real
    suffix made the watcher enqueue a half-written file and surfaced a ghost
    ".upload-XXXX" row in the UI.
    """
    import io

    repository = env["repository"]
    seen: list[list[str]] = []

    class PeekingReader(io.BytesIO):
        def read(self, size: int = -1) -> bytes:  # type: ignore[override]
            seen.append([p.name for p in repository.iter_input_files()])
            return super().read(size)

    env["service"].save_upload_stream("doc.pdf", PeekingReader(b"%PDF-1.4 data"))

    assert seen, "reader was never used"
    assert all(names == [] for names in seen), f"temp file was visible: {seen}"
    assert (env["settings"].input_dir / "doc.pdf").is_file()


def test_nested_input_previews_and_downloads_its_own_output(env) -> None:
    """Two same-named files in different folders must not share an output."""
    settings, repository, state_store = (
        env["settings"],
        env["repository"],
        env["state_store"],
    )
    hashes = {}
    for year, body in (("2024", "old report"), ("2025", "new report")):
        source = settings.input_dir / year / "report.pdf"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(f"%PDF-1.4 {year}".encode())
        file_hash = repository.compute_hash(source)
        state_store.start(file_hash, str(source))
        state_store.mark(file_hash, ProcessingState.COMPLETED)
        md_path, _ = repository.output_paths(repository.output_key(source))
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(f"# {body}", encoding="utf-8")
        hashes[year] = file_hash

    client = env["client"]
    assert client.get(f"/api/documents/{hashes['2024']}/markdown").text == "# old report"
    assert client.get(f"/api/documents/{hashes['2025']}/markdown").text == "# new report"
    assert env["service"].resolve_key(hashes["2024"]) == "2024/report"


def test_output_key_cannot_escape_the_output_directory(env) -> None:
    """Defence in depth: keys are internal, but containment is still enforced."""
    with pytest.raises(StorageError):
        env["service"].read_markdown("../../etc/passwd")
    with pytest.raises(StorageError):
        env["service"].read_markdown("")
