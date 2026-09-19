# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `LICENSE` (MIT), `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md` and
  this changelog.
- `pyproject.toml` with bounded dependency ranges, a `dev` extra and a `docpipe`
  console script; `requirements.lock` pins the exact set the Docker image uses.
- Startup preflight that reports an unreachable Ollama server or an unpulled
  model at boot, naming the command that fixes it.
- GitHub Actions running ruff, `mypy --strict`, pytest on Python 3.11/3.12/3.13,
  a dependency-resolution check, `pip-audit` and a Docker build.
- Test suites for `LLMService`, `DocumentProcessor` and `Settings`.

### Fixed

- **Output files could silently overwrite each other.** Inputs are enumerated
  recursively but outputs were keyed by bare filename, so `2024/report.pdf` and
  `2025/report.pdf` both wrote `report.md`. Outputs now mirror the input tree.
- **In-progress uploads were visible to the watcher**, producing ghost rows and
  occasionally handing a partial file to Docling. Upload temp files now use a
  `.part` suffix.
- **`DOCPIPE_LLM_CHUNK_CHARS` larger than `DOCPIPE_LLM_NUM_CTX` truncated model
  output silently**, corrupting the cleaned Markdown. Startup now refuses the
  combination and names both values.
- The README instructed users to pull `mistral-nemo:12b` while the code ran
  `qwen2.5:14b`, so the first document always failed. The default now lives in
  `docpipe/core/config.py` alone and a test keeps the docs in sync.
- `.env.example` was gitignored, so the README link 404'd on a fresh clone. It
  is now tracked and documents every setting.

### Removed

- Dead code: `main.build_runner`, `DocumentService.save_upload`,
  `JsonStateStore.upsert`.
