"""FastAPI application for the document pipeline web interface.

The app is intentionally thin: every request delegates to ``DocumentService``,
and domain errors from ``core.exceptions`` are translated into HTTP responses.
When a ``WatcherRunner`` is supplied, a lifespan hook starts it in the
background on startup and stops it on shutdown, so a single process serves the
UI and runs the pipeline.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from docpipe.core.exceptions import PipelineError, StorageError, UploadTooLargeError
from docpipe.core.logging import get_logger
from docpipe.web.service import DocumentService

logger = get_logger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"


def create_app(
    service: DocumentService,
    runner: "Optional[object]" = None,
) -> FastAPI:
    """Build the FastAPI app around a service (and optional watcher runner)."""

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if runner is not None:
            logger.info("web.lifespan.start_watcher")
            runner.start_background()
        try:
            yield
        finally:
            if runner is not None:
                logger.info("web.lifespan.stop_watcher")
                runner.stop()

    app = FastAPI(title="doc-cleaner", lifespan=lifespan)

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse((_STATIC_DIR / "index.html").read_text(encoding="utf-8"))

    @app.get("/favicon.svg", include_in_schema=False)
    def favicon() -> FileResponse:
        return FileResponse(_STATIC_DIR / "favicon.svg", media_type="image/svg+xml")

    @app.get("/api/documents")
    def list_documents() -> dict[str, object]:
        return {"documents": service.list_documents()}

    @app.get("/api/documents/{file_hash}")
    def get_document(file_hash: str) -> dict[str, object]:
        document = service.get_document(file_hash)
        if document is None:
            raise HTTPException(status_code=404, detail="Document not found")
        return document

    @app.post("/api/upload")
    async def upload(file: UploadFile) -> dict[str, object]:
        try:
            return await run_in_threadpool(
                service.save_upload_stream, file.filename or "upload", file.file
            )
        except UploadTooLargeError as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        except StorageError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/documents/{file_hash}/markdown", response_class=PlainTextResponse)
    def preview_markdown(file_hash: str) -> PlainTextResponse:
        return PlainTextResponse(_preview(service, file_hash, "markdown"))

    @app.get("/api/documents/{file_hash}/metadata", response_class=PlainTextResponse)
    def preview_metadata(file_hash: str) -> PlainTextResponse:
        return PlainTextResponse(_preview(service, file_hash, "metadata"))

    @app.get("/download/markdown/{file_hash}")
    def download_markdown(file_hash: str) -> FileResponse:
        return _download(service, file_hash, "markdown")

    @app.get("/download/metadata/{file_hash}")
    def download_metadata(file_hash: str) -> FileResponse:
        return _download(service, file_hash, "metadata")

    @app.post("/api/documents/{file_hash}/retry")
    def retry(file_hash: str) -> dict[str, object]:
        return _manage(service.retry, file_hash)

    @app.post("/api/documents/{file_hash}/reprocess")
    def reprocess(file_hash: str) -> dict[str, object]:
        return _manage(service.reprocess, file_hash)

    @app.delete("/api/documents/{file_hash}")
    def delete(file_hash: str) -> dict[str, object]:
        return _manage(service.delete, file_hash)

    if _STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    return app


def _resolve_stem(service: DocumentService, file_hash: str) -> str:
    stem = service.resolve_stem(file_hash)
    if stem is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return stem


def _preview(service: DocumentService, file_hash: str, kind: str) -> str:
    stem = _resolve_stem(service, file_hash)
    try:
        if kind == "markdown":
            return service.read_markdown(stem)
        return service.read_metadata(stem)
    except StorageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _download(service: DocumentService, file_hash: str, kind: str) -> FileResponse:
    stem = _resolve_stem(service, file_hash)
    path = (
        service.markdown_path(stem)
        if kind == "markdown"
        else service.metadata_path(stem)
    )
    if path is None:
        raise HTTPException(status_code=404, detail=f"No {kind} output available")
    return FileResponse(path=str(path), filename=path.name)


def _manage(action, file_hash: str) -> dict[str, object]:
    try:
        return action(file_hash)
    except StorageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PipelineError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
