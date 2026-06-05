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

**4. Drop a file in and watch it work.**

Copy a PDF or DOCX into `data/input/`, then watch `data/output/markdown/` and `data/output/metadata/` fill up.

Configuration is done through environment variables (all prefixed with `DOCPIPE_`). See [.env.example](.env.example) for the full list.

## Running with Docker

The container runs the pipeline and connects to **Ollama on your host** via `host.docker.internal`:

```bash
docker compose up --build
```

Your local `./data` folder is mounted into the container, so inputs, outputs, and the state ledger all persist on the host. A named volume caches Docling's models between runs so it doesn't re-download them every time.

## Resource notes

Docling and Ollama are both memory-hungry, and running them side by side on one machine is demanding. A few things worth knowing:

- Docling downloads layout models on first run. Plan for roughly **8–16 GB of RAM**, and keep `DOCPIPE_MAX_WORKERS` low (default `1`).
- Large documents are truncated to `DOCPIPE_LLM_MAX_CHARS` for the refining pass so they fit in the model's context window.
- Files that are still being copied in are debounced and size-checked before processing, so partial files won't be picked up.

## Tests

The pure-logic units (ledger, file routing, prompts) run without Docling or Ollama:

```bash
pytest -q
```
