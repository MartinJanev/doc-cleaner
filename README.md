# doc-cleaner

A local-first, layout-aware **document ingestion pipeline** for Retrieval-Augmented Generation (RAG). It watches a directory, extracts layout-faithful Markdown from PDFs/DOCX with **IBM Docling**, refines it through a **local Ollama LLM** (cleanup + YAML metadata), and writes clean, LLM-optimized Markdown plus structured metadata. No cloud APIs.

## How it works

```
data/input/*.pdf|*.docx
        |
        v
  watchdog (debounced)  --->  JobQueue (thread pool)
        |                           |
        |                           v
        |                    DocumentProcessor
        |                  /        |          \
        |          Docling     Ollama LLM     FileRepository
        |        (extract)   (refine + YAML)   (write output)
        |                           |
        v                           v
  JSON state ledger        data/output/markdown/*.md
  (.pipeline_state.json)   data/output/metadata/*.json
```

Each document moves through `PENDING -> EXTRACTED -> RESTRUCTURED -> COMPLETED` (or `FAILED`). State is keyed by **file content hash**, so re-saving an identical file is a no-op and crash recovery is automatic.

## Architecture

Layered, dependency-injected, single-responsibility:

| Layer | Module | Responsibility |
| --- | --- | --- |
| core | `docpipe/core/config.py` | Env-driven `Settings` (pydantic) |
| core | `docpipe/core/logging.py` | structlog JSON logging |
| core | `docpipe/core/exceptions.py` | Typed error hierarchy |
| models | `docpipe/models/documents.py` | Domain models + `ProcessingState` |
| services | `docpipe/services/extraction_service.py` | Wraps Docling |
| services | `docpipe/services/llm_service.py` + `prompts.py` | Ollama orchestration + YAML front matter |
| storage | `docpipe/storage/state_store.py` | Thread-safe JSON ledger (atomic writes) |
| storage | `docpipe/storage/file_repository.py` | Input reads, output routing, hashing |
| pipeline | `docpipe/pipeline/processor.py` | End-to-end orchestration + error boundary |
| watcher | `docpipe/watcher/*` | Debounced events, job queue, runner |
| root | `docpipe/main.py` | Composition root (DI wiring) |

## Quick start (local)

1. Install and start [Ollama](https://ollama.com), then pull a model:

```bash
ollama pull llama3
```

2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Run the pipeline:

```bash
python -m docpipe.main
```

4. Drop a PDF or DOCX into `data/input/`. Watch `data/output/markdown/` and `data/output/metadata/` fill in.

Configuration is via environment variables (prefix `DOCPIPE_`); see [.env.example](.env.example).

## Docker

The container runs the pipeline and talks to **Ollama on the host** via `host.docker.internal`.

```bash
docker compose up --build
```

`./data` is mounted into the container, so inputs/outputs/ledger persist on the host. A named volume caches Docling's models between runs.

## Resource notes

- Docling downloads layout models on first run and is memory-hungry; running it alongside Ollama on one machine is heavy. Keep `DOCPIPE_MAX_WORKERS` low (default `1`) and plan for ~8-16 GB RAM.
- Large documents are truncated to `DOCPIPE_LLM_MAX_CHARS` for the metadata/refining pass to stay within the model context.
- Files still being copied are debounced and size-checked before processing.

## Tests

Pure-logic units (ledger, file routing, prompts) run without Docling/Ollama:

```bash
pytest -q
```
