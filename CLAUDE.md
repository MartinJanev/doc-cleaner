# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Local-first RAG preprocessor. Watches `docpipe/data/input`, extracts Markdown from `.pdf`/`.docx` with IBM Docling, refines it through a local Ollama model (cleanup + metadata), and writes `docpipe/data/output/markdown/<stem>.md` (with YAML front matter) plus `docpipe/data/output/metadata/<stem>.json`. A background comparator scores each refined document against the extraction it came from, so the model's contribution is measurable. No cloud calls. Python 3.11+, package in `docpipe/`.

## Commands

A `.venv/` with all dependencies (including Docling's heavy transitive deps) is already present — prefer it over bare `python`.

```bash
.venv/bin/python -m pytest -q                              # full suite (~0.3s, 35 tests)
.venv/bin/python -m pytest tests/test_prompts.py -q        # one file
.venv/bin/python -m pytest tests/test_web_service.py::test_upload_rejects_unsupported_type -q   # one test
.venv/bin/python -m docpipe.main                           # run watcher + web UI (http://127.0.0.1:8000)
pip install -r requirements.txt                            # fresh install
docker compose up --build                                  # containerized; talks to host Ollama
.venv/bin/python -m docpipe.cli compare                    # score raw-vs-refined; needs no Ollama
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

docling_comparator.py (own thread, interval sweep, out of band)
  → scores output/raw/<key>.md against output/markdown/<key>.md
  → writes output/comparison/<key>.json
```

### The ledger is the control plane

`.pipeline_state.json` is keyed by **SHA-256 of file content**, not by path. Consequences that drive most of the design:

- Re-saving an identical file is a no-op; editing a file yields a new hash and reprocesses.
- Crash recovery is just `runner.reconcile()` re-enqueuing input files whose hash is not `COMPLETED`.
- `JsonStateStore.should_process(hash, max_attempts)` is the single gate: unknown → yes, `COMPLETED` → no, `FAILED` → only within the retry budget.
- The web layer forces a retry/reprocess by **deleting the ledger entry** (`state_store.delete`) and re-enqueuing; `DocumentService.reprocess` is literally an alias of `retry`.

States: `PENDING → EXTRACTED → RESTRUCTURED → COMPLETED`, or `FAILED`. The web UI adds a synthetic `QUEUED` for input files that exist on disk but have no ledger record yet.

Ledger writes are atomic (`tempfile.mkstemp` + `fsync` + `os.replace`) under a re-entrant lock. Never write the state file directly; always go through `JsonStateStore`.

### Hash vs. output key — the one asymmetry

Identity is the content hash, but **output filenames are keyed by the source's path relative to the input dir** — the *output key*. `report.pdf` → `report.md`; `2024/report.pdf` → `2024/report.md`. Inputs are enumerated recursively, so keying by bare stem let same-named files in different folders silently overwrite each other's outputs.

`FileRepository.output_key(source)` and `FileRepository.output_paths(key)` are the **only** place this mapping lives — `write_outputs` and the whole web layer go through them. Never rebuild an output path from `settings.markdown_dir` by hand. `DocumentService._unique_target` still de-duplicates colliding upload names (`report (1).pdf`) at ingest, since uploads all land flat in the input root.

Keys legitimately contain `/`, so containment against the output roots (`DocumentService._checked_paths`) replaces any separator denylist.

### Measuring the model

The pipeline keeps only the model's answer, which makes the model's contribution
invisible: nothing distinguishes a run that stripped page furniture from one that
quietly rewrote the document. `docpipe/docling_comparator.py` scores the two texts
against each other.

`DocumentProcessor` calls `FileRepository.write_raw(document)` **before** refining, so
a document that fails the model stage still leaves its extraction behind. `write_raw`
is a no-op when `DOCPIPE_COMPARE_ENABLED=false`, which keeps the flag out of the
processor.

`DoclingComparator` runs **out of band** — its own daemon thread on
`DOCPIPE_COMPARE_INTERVAL_S`, started by `main()` rather than by `WatcherRunner`,
because it is measurement and must never sit on the path of producing a document.
Pending work is derived entirely from the filesystem: a key is pending when a raw
file exists, a refined file exists, and the comparison is missing or older than the
refined output. That makes the sweep idempotent, self-healing after a crash, and able
to pick up anything processed while comparison was switched off. It also means
`docpipe compare` works with the daemon stopped and **needs neither Docling nor
Ollama**.

Scoring functions (`tokenize`, `strip_front_matter`, `measure`, `score`, `compare`)
are pure and stdlib-only, for the same reason `services/prompts.py` keeps text away
from transport. Front matter is stripped from the refined side first, or the model's
own metadata block counts as invented prose.

`introduced` is the score that matters: the share of output vocabulary absent from
the input. A cleanup pass should sit near zero. `similarity` alone cannot tell good
boilerplate removal from quiet rewriting — both read as low.

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

- **The model default lives in exactly one place**: `DOCPIPE_MODEL` in `docpipe/core/config.py`. `Dockerfile` and `docker-compose.yml` deliberately do **not** set `DOCPIPE_MODEL_TAG` so they cannot drift. `tests/test_config.py` fails if `.env.example` disagrees with the code, so change the constant and the test tells you what else to update.
- `PROJECT_NOTES.md` is gitignored on purpose (it is the author's private notes) — edits to it will not show up in `git status`. `.env.example` **is** tracked, because the README links to it.
- Keep `DOCPIPE_MAX_WORKERS` at 1 unless there is memory headroom; Docling and Ollama are both memory-hungry and share the machine.
- `DOCPIPE_LLM_NUM_CTX` must fit one chunk plus its cleaned output. Raising `DOCPIPE_LLM_CHUNK_CHARS` without raising `NUM_CTX` used to truncate model output silently; a `model_validator` on `Settings` now refuses to start instead. Keep that guard in sync if the chunking strategy changes.
- Comparison records store `model` from the **currently configured** tag, not a recorded one — nothing downstream stores which model produced a given output. In the daemon the sweep follows processing by seconds, so it is accurate in practice; after changing `DOCPIPE_MODEL_TAG`, old scores keep the old tag until their document is reprocessed.
- `difflib.SequenceMatcher` is superlinear, so the ordered `similarity` score is capped at `_SEQUENCE_TOKEN_CAP` (20k) tokens per side; the set-based scores are linear and always see the whole document. Two 20k-token sides take about a second.
- The web UI bundles its own Markdown renderer (`web/static/js/markdown.js`) to stay fully offline — do not introduce a CDN dependency. The frontend is plain ES modules under `web/static/js/` with **no build step**; a strict CSP (`script-src 'self'`) would block a CDN anyway. Build DOM with the `el()` helper in `js/dom.js` rather than interpolating into `innerHTML`; the one exception is the Markdown renderer's output, which is safe because it escapes before it transforms.

## Repo rules (always active)

Two rule sets govern all work here. They live in `.claude/skills/karpathy-guidelines/SKILL.md` and `.claude/skills/ponytail/SKILL.md`, and a `SessionStart` hook in `.claude/settings.json` injects both into every session automatically — no need to invoke them. Those files are the source of truth; the summary below is a convenience, so edit the skills, not this section. In short:

- **Simplest thing that works.** Before writing code, stop at the first rung that holds: does it need building at all; does it already exist in this codebase; does the stdlib or an already-installed dependency cover it; can it be one line. No abstractions, configurability, or error handling for impossible cases that were not requested.
- **Surgical diffs.** Touch only what the request requires. Do not improve adjacent code, comments, or formatting; match existing style. Remove only the imports/functions *your* change orphaned; mention pre-existing dead code rather than deleting it.
- **Fix root causes, not symptoms.** Grep every caller of a function you touch and fix the shared function once, rather than patching only the path a report names.
- **Surface assumptions and tradeoffs before implementing**; if multiple readings exist, present them instead of picking silently.
- **Define verifiable success criteria.** Non-trivial logic leaves behind one runnable check — the smallest thing that fails if the logic breaks. Trivial one-liners need none.
- Mark a deliberate corner-cut with a known ceiling (global lock, O(n²) scan, naive heuristic) with a `ponytail:` comment naming the ceiling and the upgrade path.
