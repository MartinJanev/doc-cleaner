# doc-cleaner

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
ollama pull llama3
```

**2. Install Python dependencies** (Python 3.11+):

```bash
pip install -r requirements.txt
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

## Tests

The pure-logic units (ledger, file routing, prompts) run without Docling or Ollama:

```bash
pytest -q
```
