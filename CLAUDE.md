# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Local-first RAG preprocessor. Watches `data/input`, extracts Markdown from `.pdf`/`.docx` with IBM Docling, refines it through a local Ollama model (cleanup + metadata), and writes `data/output/markdown/<stem>.md` (with YAML front matter) plus `data/output/metadata/<stem>.json`. No cloud calls. Python 3.11+, package in `docpipe/`.

## Commands

A `.venv/` with all dependencies (including Docling's heavy transitive deps) is already present — prefer it over bare `python`.

```bash
.venv/bin/python -m pytest -q                              # full suite (~0.3s, 35 tests)
.venv/bin/python -m pytest tests/test_prompts.py -q        # one file
.venv/bin/python -m pytest tests/test_web_service.py::test_upload_rejects_unsupported_type -q   # one test
.venv/bin/python -m docpipe.main                           # run watcher + web UI (http://127.0.0.1:8000)
pip install -r requirements.txt                            # fresh install
docker compose up --build                                  # containerized; talks to host Ollama
```

There is no linter, formatter, or type-checker configured, and no pytest config file — `pytest` collects `tests/` by default.

Running the pipeline for real needs Ollama up with the model pulled (`ollama pull qwen2.5:14b`). The tests do not.

## Architecture

### Composition root

`docpipe/main.py` is the **only** place concrete classes are instantiated. Everything else takes collaborators as constructor parameters. When adding a service, wire it in `build_components` and inject it — do not import a concrete dependency inside a lower layer.

`build_components` returns a `Components` bundle shared by both front ends: the watcher and the web UI run **in the same process** against the same `FileRepository`, `JsonStateStore`, and `JobQueue`. That is why the web layer can read live state and enqueue work directly with no IPC. With `DOCPIPE_WEB_ENABLED=false`, `main()` runs `runner.start()` (blocking, signal-handled) instead; with the web enabled, uvicorn is the foreground process and a FastAPI lifespan hook calls `runner.start_background()` / `runner.stop()`.

### Flow

```
watcher/runner.py (reconcile on startup + watchdog observer)
  → watcher/event_handler.py (per-path debounce + size-stability check)
  → watcher/job_queue.py (thread pool, in-flight dedupe by path string)
  → pipeline/processor.py (the only module that knows the full stage sequence)
      → services/extraction_service.py (Docling)
      → services/llm_service.py + prompts.py (Ollama)
      → storage/file_repository.py (outputs) + storage/state_store.py (ledger)
```

### The ledger is the control plane

`.pipeline_state.json` is keyed by **SHA-256 of file content**, not by path. Consequences that drive most of the design:

- Re-saving an identical file is a no-op; editing a file yields a new hash and reprocesses.
- Crash recovery is just `runner.reconcile()` re-enqueuing input files whose hash is not `COMPLETED`.
- `JsonStateStore.should_process(hash, max_attempts)` is the single gate: unknown → yes, `COMPLETED` → no, `FAILED` → only within the retry budget.
- The web layer forces a retry/reprocess by **deleting the ledger entry** (`state_store.delete`) and re-enqueuing; `DocumentService.reprocess` is literally an alias of `retry`.

States: `PENDING → EXTRACTED → RESTRUCTURED → COMPLETED`, or `FAILED`. The web UI adds a synthetic `QUEUED` for input files that exist on disk but have no ledger record yet.

Ledger writes are atomic (`tempfile.mkstemp` + `fsync` + `os.replace`) under a re-entrant lock. Never write the state file directly; always go through `JsonStateStore`.

### Hash vs. stem — the one asymmetry

Identity is the content hash, but **output filenames are keyed by the source stem** (`report.pdf` → `report.md` / `report.json`). Two different files with the same stem would clobber each other's outputs, so `DocumentService._unique_target` de-duplicates upload names (`report (1).pdf`) at ingest. Keep that invariant in mind when touching output paths or upload handling.

### Error boundary

Everything failable raises from the `docpipe/core/exceptions.py` hierarchy (`PipelineError` → `ConfigurationError`, `ExtractionError`, `LLMServiceError`, `StorageError` → `UploadTooLargeError`). `DocumentProcessor.process` catches `PipelineError` **and** bare `Exception` per document, records `FAILED` with the message, and returns `None` — one bad file must never kill a worker thread or the daemon. `JobQueue` has a second such net. The FastAPI layer (`web/app.py`) is a thin translator: it maps these exception types onto HTTP status codes and holds no logic of its own.

### LLM stage

`LLMService.refine` branches on document size:

- `len(markdown) <= llm_chunk_chars` → one `SYSTEM_PROMPT` call that returns cleaned body *and* metadata together.
- larger → `_clean_in_chunks` cleans fragment by fragment with `CLEAN_SYSTEM_PROMPT` (so the model never has to emit a whole long document in one response), then a separate `METADATA_SYSTEM_PROMPT` pass over the assembled body.

All calls use Ollama's `format="json"` and go through `parse_llm_json`, which tolerates ```json fences and stray prose by extracting the outermost balanced `{...}`. Prompt text, `chunk_markdown`, `parse_llm_json`, and `render_front_matter` live in `services/prompts.py` precisely so they are testable without a model — keep transport in `llm_service.py` and text/parsing in `prompts.py`.

## Conventions

- **Strict type hints** on every signature; `from __future__ import annotations` at the top of every module.
- **Heavy imports stay lazy.** `docling` and `ollama` are imported inside `main.build_components` or under `if TYPE_CHECKING:` (see `extraction_service.py`, `llm_service.py`). Breaking this makes the test suite slow and couples pure-logic tests to model libraries.
- **Config only through `docpipe/core/config.py`.** Pydantic `Settings`, `DOCPIPE_` env prefix, reads `.env`. Nothing else may touch `os.environ`. Add new knobs as `Field`s with a description, and document them in `.env.example`.
- **Logging is structlog with event-name strings**, not sentences: `logger.info("llm.refine.done", path=..., chars=...)`. Use `get_logger(__name__)` and `logger.bind(...)` for per-document context.
- Domain models in `models/documents.py` are frozen dataclasses with no behaviour beyond serialization, so they can cross every layer.

## Testing

`tests/` covers only the pure-logic units — state store, file repository, prompts, and the web service/routes via `fastapi.testclient` — with no Docling or Ollama. Preserve that: inject fakes rather than reaching for a live model. `DocumentService` takes `submit_job` as an optional callable specifically so tests can pass a list-appending stub instead of a real `JobQueue`.

## Gotchas

- **Default model differs across files.** The effective default is `DOCPIPE_MODEL = "qwen2.5:14b"` in `docpipe/core/config.py`; `README.md`, `.env.example`, `Dockerfile`, and `docker-compose.yml` all say `mistral-nemo:12b` via `DOCPIPE_MODEL_TAG`. Env always wins — check `config.py` for what actually runs, and update both sides if you change it.
- `PROJECT_NOTES.md` and `.env.example` are listed in `.gitignore` and are untracked — edits to them will not show up in `git status`. (`.gitignore` also carries a now-dead `AGENTS.md` entry; that file was superseded by this one.)
- Keep `DOCPIPE_MAX_WORKERS` at 1 unless there is memory headroom; Docling and Ollama are both memory-hungry and share the machine.
- `DOCPIPE_LLM_NUM_CTX` must comfortably fit one chunk plus its cleaned output. Raising `DOCPIPE_LLM_CHUNK_CHARS` without raising `NUM_CTX` silently truncates model output.
- The web UI bundles its own Markdown renderer (`web/static/markdown.js`) to stay fully offline — do not introduce a CDN dependency.

## Repo rules (always active)

Two rule sets govern all work here. They live in `.claude/skills/karpathy-guidelines/SKILL.md` and `.claude/skills/ponytail/SKILL.md`, and a `SessionStart` hook in `.claude/settings.json` injects both into every session automatically — no need to invoke them. Those files are the source of truth; the summary below is a convenience, so edit the skills, not this section. In short:

- **Simplest thing that works.** Before writing code, stop at the first rung that holds: does it need building at all; does it already exist in this codebase; does the stdlib or an already-installed dependency cover it; can it be one line. No abstractions, configurability, or error handling for impossible cases that were not requested.
- **Surgical diffs.** Touch only what the request requires. Do not improve adjacent code, comments, or formatting; match existing style. Remove only the imports/functions *your* change orphaned; mention pre-existing dead code rather than deleting it.
- **Fix root causes, not symptoms.** Grep every caller of a function you touch and fix the shared function once, rather than patching only the path a report names.
- **Surface assumptions and tradeoffs before implementing**; if multiple readings exist, present them instead of picking silently.
- **Define verifiable success criteria.** Non-trivial logic leaves behind one runnable check — the smallest thing that fails if the logic breaks. Trivial one-liners need none.
- Mark a deliberate corner-cut with a known ceiling (global lock, O(n²) scan, naive heuristic) with a `ponytail:` comment naming the ceiling and the upgrade path.
