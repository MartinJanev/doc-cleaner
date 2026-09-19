"""FastAPI application for the document pipeline web interface.

The app is intentionally thin: every request delegates to ``DocumentService``,
and domain errors from ``core.exceptions`` are translated into HTTP responses.
When a ``WatcherRunner`` is supplied, a lifespan hook starts it in the
background on startup and stops it on shutdown, so a single process serves the
UI and runs the pipeline.

The response models below exist so ``/docs`` describes a real schema rather than
an opaque object -- they are the contract the frontend is written against.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from docpipe.core.exceptions import PipelineError, StorageError, UploadTooLargeError
from docpipe.core.logging import get_logger
from docpipe.web.service import DocumentService

if TYPE_CHECKING:  # the web layer must not import the watcher at runtime
    from docpipe.watcher.runner import WatcherRunner

logger = get_logger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"

# Everything the page needs is served from this origin, so the policy can be
# strict. img-src in particular stops a cleaned document's remote <img> from
# phoning home when it is previewed, which would quietly break the "nothing
# leaves your machine" promise.
_CSP = (
    "default-src 'self'; "
    "img-src 'self' data:; "
    "style-src 'self'; "
    "script-src 'self'; "
    "connect-src 'self'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)
_SECURITY_HEADERS = {
    "Content-Security-Policy": _CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class DocumentSummary(BaseModel):
    """One row of the document list."""

    id: str = Field(description="SHA-256 of the source file's contents.")
    name: str = Field(description="Source file name.")
    key: str = Field(description="Output path, relative to the output roots.")
    state: str = Field(description="Ledger state, or QUEUED if not yet recorded.")
    attempts: int
    error: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    has_markdown: bool
    has_metadata: bool


class DocumentList(BaseModel):
    documents: list[DocumentSummary]


class UploadResult(BaseModel):
    filename: str = Field(description="Name the upload was stored under.")
    key: str = Field(description="Output key the results will be written to.")


class ManageResult(BaseModel):
    """Result of a retry, reprocess or delete."""

    resubmitted: str | None = None
    removed: list[str] | None = None


class HealthStatus(BaseModel):
    status: str = Field(description='"ok", or "degraded" when the model is unusable.')
    version: str
    model: str
    ollama: str | None = Field(
        default=None,
        description="None when Ollama is ready, otherwise what to do about it.",
    )


def create_app(
    service: DocumentService,
    runner: WatcherRunner | None = None,
) -> FastAPI:
    """Build the FastAPI app around a service (and optional watcher runner)."""

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
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

    @app.middleware("http")
    async def guard_and_harden(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Reject cross-site writes, and set security headers on every response.

        Uploads and retries are CORS-"simple" requests, so without this a page
        the user happens to visit could drive their local pipeline. Browsers
        report the origin in Sec-Fetch-Site; non-browser clients (curl, scripts)
        send nothing and are left alone, which is the right trade-off for a
        localhost tool.
        """
        if (
            request.method in _UNSAFE_METHODS
            and request.headers.get("sec-fetch-site") == "cross-site"
        ):
            logger.warning("web.blocked.cross_site", path=request.url.path)
            return PlainTextResponse(
                "Cross-site requests are not allowed.", status_code=403
            )
        response = await call_next(request)
        response.headers.update(_SECURITY_HEADERS)
        return response

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse((_STATIC_DIR / "index.html").read_text(encoding="utf-8"))

    @app.get("/favicon.svg", include_in_schema=False)
    def favicon() -> FileResponse:
        return FileResponse(_STATIC_DIR / "favicon.svg", media_type="image/svg+xml")

    @app.get("/health")
    def health() -> HealthStatus:
        return HealthStatus(**service.health())

    @app.get("/api/documents")
    def list_documents() -> DocumentList:
        documents = [DocumentSummary(**doc) for doc in service.list_documents()]
        return DocumentList(documents=documents)

    @app.get("/api/documents/{file_hash}")
    def get_document(file_hash: str) -> DocumentSummary:
        document = service.get_document(file_hash)
        if document is None:
            raise HTTPException(status_code=404, detail="Document not found")
        return DocumentSummary(**document)

    @app.post("/api/upload")
    async def upload(file: UploadFile) -> UploadResult:
        try:
            result = await run_in_threadpool(
                service.save_upload_stream, file.filename or "upload", file.file
            )
        except UploadTooLargeError as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        except StorageError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return UploadResult(**result)

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
    def retry(file_hash: str) -> ManageResult:
        return ManageResult(**_manage(service.retry, file_hash))

    @app.post("/api/documents/{file_hash}/reprocess")
    def reprocess(file_hash: str) -> ManageResult:
        return ManageResult(**_manage(service.reprocess, file_hash))

    @app.delete("/api/documents/{file_hash}")
    def delete(file_hash: str) -> ManageResult:
        return ManageResult(**_manage(service.delete, file_hash))

    if _STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    return app


def _resolve_key(service: DocumentService, file_hash: str) -> str:
    key = service.resolve_key(file_hash)
    if key is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return key


def _preview(service: DocumentService, file_hash: str, kind: str) -> str:
    key = _resolve_key(service, file_hash)
    try:
        if kind == "markdown":
            return service.read_markdown(key)
        return service.read_metadata(key)
    except StorageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _download(service: DocumentService, file_hash: str, kind: str) -> FileResponse:
    key = _resolve_key(service, file_hash)
    path = (
        service.markdown_path(key) if kind == "markdown" else service.metadata_path(key)
    )
    if path is None:
        raise HTTPException(status_code=404, detail=f"No {kind} output available")
    return FileResponse(path=str(path), filename=path.name)


def _manage(action: Callable[[str], dict[str, Any]], file_hash: str) -> dict[str, Any]:
    try:
        return action(file_hash)
    except StorageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PipelineError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
