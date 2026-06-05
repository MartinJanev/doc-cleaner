# AGENTS

## Project snapshot
- Local-first document ingestion pipeline (RAG preprocessor). Watches `data/input`, extracts Markdown via IBM Docling, refines it through a local Ollama LLM (cleanup + YAML metadata), and writes to `data/output`.
- No cloud services. Python 3.11+. Package lives in `docpipe/`.
- Entry point: `python -m docpipe.main` (composition root that wires DI and starts the watcher daemon).

## Architecture and data flow
- Layered, single-responsibility, dependency-injected. Concretes are instantiated ONLY in `docpipe/main.py`; everything else receives collaborators via constructors.
- Flow: `watcher/runner.py` -> `watcher/event_handler.py` (debounced) -> `watcher/job_queue.py` (thread pool) -> `pipeline/processor.py` -> `services/extraction_service.py` (Docling) -> `services/llm_service.py` (Ollama) -> `storage/file_repository.py` (output) + `storage/state_store.py` (ledger).
- Domain models in `docpipe/models/documents.py`: `ProcessingState`, `DocumentRecord`, `MarkdownDocument`, `DocumentMetadata`, `RestructuredDocument`.

## Conventions and patterns to follow
- Strict type hints on every function signature.
- Errors map to the hierarchy in `docpipe/core/exceptions.py` (`ExtractionError`, `LLMServiceError`, `StorageError`, `PipelineError`). The processor catches `PipelineError` per document so one failure never stops the daemon.
- Idempotency key = SHA-256 of file content. `JsonStateStore.should_process` skips `COMPLETED` hashes and honors `max_attempts` for `FAILED`.
- State writes are atomic (temp file + `os.replace`) and lock-guarded; never write the ledger directly.
- Heavy imports (`docling`, `ollama`) are lazy (inside `main.build_runner` / under `TYPE_CHECKING`) so tests and `--help` stay fast.
- Config is env-driven via `docpipe/core/config.py` (`DOCPIPE_` prefix). Do not read `os.environ` elsewhere.
- Logging is structlog; use `get_logger(__name__)` and structured kwargs.

## Workflows
- Install deps: `pip install -r requirements.txt`.
- Run pipeline: `python -m docpipe.main` (then drop files into `data/input/`).
- Run tests: `pytest -q` (pure-logic only: state store, file repository, prompts; no Docling/Ollama needed).
- Docker: `docker compose up --build` (talks to host Ollama via `host.docker.internal`).

## Integration points and env knobs
- Ollama: `DOCPIPE_OLLAMA_HOST` (default `http://localhost:11434`), `DOCPIPE_MODEL_TAG` (default `llama3`).
- Filesystem: `DOCPIPE_INPUT_DIR`, `DOCPIPE_OUTPUT_DIR`, `DOCPIPE_STATE_FILE`.
- Concurrency: `DOCPIPE_MAX_WORKERS` (keep low; Docling + Ollama are memory-heavy), `DOCPIPE_DEBOUNCE_SECONDS`, `DOCPIPE_MAX_ATTEMPTS`.
- LLM bounds: `DOCPIPE_LLM_MAX_CHARS`, `DOCPIPE_LLM_TIMEOUT_S`, `DOCPIPE_LLM_TEMPERATURE`.
- Supported inputs: `.pdf`, `.docx` (`DOCPIPE_SUPPORTED_SUFFIXES`).
