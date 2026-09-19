# doc-cleaner

[![CI](https://github.com/martinjanev/doc-cleaner/actions/workflows/ci.yml/badge.svg)](https://github.com/martinjanev/doc-cleaner/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Checked with mypy](https://img.shields.io/badge/mypy-strict-blue)](https://mypy-lang.org/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

Turn messy PDFs and Word documents into clean, LLM-ready Markdown — entirely on your own machine.

doc-cleaner watches a folder, and whenever you drop in a `.pdf` or `.docx`, it:

1. **Extracts** layout-faithful Markdown with [IBM Docling](https://github.com/DS4SD/docling).
2. **Refines** that Markdown with a **local [Ollama](https://ollama.com) model** — tidying the text and generating YAML metadata.
3. **Saves** the cleaned Markdown and a structured metadata file to an output folder.

It's built for RAG (Retrieval-Augmented Generation) pipelines that need tidy source text. Nothing leaves your machine — there are no cloud APIs.

## How it works

Drop a file in, get clean Markdown out:

```
data/input/*.pdf | *.docx
        │
        ▼
  watcher (debounced)  ──►  job queue (thread pool)
        │                          │
        │                          ▼
        │                   DocumentProcessor
        │                 ╱        │         ╲
        │           Docling     Ollama      FileRepository
        │          (extract)  (refine +    (write output)
        │                       metadata)
        ▼                          ▼
  state ledger             data/output/markdown/*.md
  (.pipeline_state.json)   data/output/metadata/*.json
```

Every document moves through a sequence of states:

```
PENDING → EXTRACTED → RESTRUCTURED → COMPLETED   (or → FAILED)
```

Progress is tracked in a JSON ledger keyed by the **file's content hash**. Two nice consequences:

- **Re-saving an identical file does nothing** — it's already been processed.
- **Crashes recover automatically** — on restart, unfinished work picks up where it left off, and failed files retry up to a configurable limit.

## What you get

For an input file named `report.pdf`, doc-cleaner writes two files:

- `data/output/markdown/report.md` — cleaned Markdown with a YAML front-matter header.
- `data/output/metadata/report.json` — structured metadata (plus the source filename and content hash).

Subfolders are mirrored, so `data/input/2024/report.pdf` writes to `data/output/markdown/2024/report.md`. Two files with the same name in different folders keep separate outputs.

## Architecture

The codebase is layered, dependency-injected, and single-responsibility. Concrete classes are wired together in one place (`docpipe/main.py`); everything else receives its collaborators through constructors.

| Layer | Module | Responsibility |
| --- | --- | --- |
| core | `core/config.py` | Environment-driven settings (pydantic) |
| core | `core/logging.py` | structlog logging |
| core | `core/exceptions.py` | Typed error hierarchy |
| models | `models/documents.py` | Domain models + `ProcessingState` |
| services | `services/extraction_service.py` | Docling extraction |
| services | `services/llm_service.py` + `prompts.py` | Ollama refining + YAML front matter |
| storage | `storage/state_store.py` | Thread-safe JSON ledger (atomic writes) |
| storage | `storage/file_repository.py` | Input reads, output routing, hashing |
| pipeline | `pipeline/processor.py` | End-to-end orchestration + error boundary |
| watcher | `watcher/*` | Debounced events, job queue, runner |
| root | `main.py` | Composition root (DI wiring) |

(All paths are under `docpipe/`.)

## Quick start

**1. Install Ollama and pull a model.**

Install [Ollama](https://ollama.com), make sure it's running, then pull the default model:

```bash
ollama pull qwen2.5:14b
```

That default lives in one place — `DOCPIPE_MODEL` in [`docpipe/core/config.py`](docpipe/core/config.py). If 14B is too heavy for your machine, pull a smaller one and set `DOCPIPE_MODEL_TAG` to match:

| Model | RAM | Notes |
| --- | --- | --- |
| `qwen2.5:14b` | 16 GB+ | Default. Best cleanup and metadata quality. |
| `mistral-nemo:12b` | 12 GB+ | Strong mid-size alternative. |
| `qwen2.5:7b` | 8 GB+ | Reliable JSON on modest hardware. |
| `llama3.1:8b` | 8 GB+ | Similar footprint, slightly weaker JSON. |

doc-cleaner checks on startup that Ollama is reachable and that your model is pulled, and tells you exactly what to run if it isn't.

**2. Install doc-cleaner** (Python 3.11+):

```bash
pip install -e .          # add ".[dev]" for the test and lint tooling
```

**3. Start the pipeline:**

```bash
python -m docpipe.main
```

This single command starts both the file watcher and the web interface.

**4. Use it — two ways:**

- **Web UI:** open [http://127.0.0.1:8000](http://127.0.0.1:8000) to upload documents, watch processing status live, preview/download cleaned Markdown and metadata, and retry/reprocess/delete documents.
- **Drop folder:** copy a PDF or DOCX into `data/input/`, then watch `data/output/markdown/` and `data/output/metadata/` fill up.

Both share the same pipeline, so files added either way show up in the UI.

Configuration is done through environment variables (all prefixed with `DOCPIPE_`). See [.env.example](.env.example) for the full list.

## Web interface

The web UI runs in the same process as the watcher, so there's nothing extra to launch. It gives you:

- **Upload** — drag and drop `.pdf` / `.docx` files straight into the pipeline (streamed to disk, so large files don't blow up memory).
- **Live status** — a dashboard with a per-document progress stepper (`Extract → Refine → Save`) and running counts, updated live as documents move through `PENDING → EXTRACTED → RESTRUCTURED → COMPLETED` (or `FAILED`, with the error).
- **Browse** — a side drawer that renders the cleaned Markdown (with its YAML front matter), shows metadata as readable fields, and offers one-click `.md` / `.json` downloads.
- **Manage** — retry failed documents, reprocess completed ones, or delete a document (with a confirmation) along with its outputs and history.

Markdown is rendered by a small bundled script, so the UI stays fully offline — nothing is fetched from a CDN.

It's controlled by these settings:

| Variable | Default | Purpose |
| --- | --- | --- |
| `DOCPIPE_WEB_ENABLED` | `true` | Set `false` for a headless, watcher-only daemon. |
| `DOCPIPE_WEB_HOST` | `127.0.0.1` | Bind address. Use `0.0.0.0` to expose it (e.g. in Docker). |
| `DOCPIPE_WEB_PORT` | `8000` | Port the UI listens on. |
| `DOCPIPE_WEB_MAX_UPLOAD_MB` | `200` | Maximum size accepted for a single upload. |

## Running with Docker

The container runs the pipeline and connects to **Ollama on your host** via `host.docker.internal`:

```bash
docker compose up --build
```

Your local `./data` folder is mounted into the container, so inputs, outputs, and the state ledger all persist on the host. A named volume caches Docling's models between runs so it doesn't re-download them every time.

To reach the web UI from the host, bind it to all interfaces and publish the port — set `DOCPIPE_WEB_HOST=0.0.0.0` and map `-p 8000:8000` (or the equivalent `ports:` entry in your compose file).

## Resource notes

Docling and Ollama are both memory-hungry, and running them side by side on one machine is demanding. A few things worth knowing:

- Docling downloads layout models on first run. Plan for roughly **8–16 GB of RAM**, and keep `DOCPIPE_MAX_WORKERS` low (default `1`).
- Long documents are **cleaned in chunks** rather than truncated: anything larger than `DOCPIPE_LLM_CHUNK_CHARS` is refined fragment by fragment, then analyzed for metadata in a final pass. This keeps multi-page files from being cut off. Larger chunks need a larger `DOCPIPE_LLM_NUM_CTX` (the Ollama context window) and more RAM.
- Files that are still being copied in are debounced and size-checked before processing, so partial files won't be picked up.

## Development

The test suite covers the ledger, file routing, prompts, the LLM stage and the
processor — all with fakes, so it needs neither Docling nor Ollama and runs in
well under a second:

```bash
pip install -r requirements-test.txt && pip install --no-deps -e .
pytest
```

The full gate, which is what CI runs:

```bash
ruff check . && ruff format --check . && mypy && pytest
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the house rules, and
[SECURITY.md](SECURITY.md) for the threat model before you expose the web UI to
anything beyond localhost.

## License

[MIT](LICENSE) © Martin Janev
