# Contributing to doc-cleaner

Thanks for taking a look. Bug reports, docs fixes and small focused PRs are all
welcome.

## Getting set up

```bash
git clone https://github.com/martinjanev/doc-cleaner
cd doc-cleaner
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

That pulls Docling and its scientific stack, which is a large download. If you
are only touching pure-logic code, the fast path is enough:

```bash
pip install -r requirements-test.txt
pip install --no-deps -e .
```

To actually run the pipeline you also need [Ollama](https://ollama.com) with a
model pulled — see the README. The tests do **not** need it.

## The checks CI runs

```bash
ruff check .          # lint
ruff format --check . # formatting
mypy                  # strict type checking of docpipe/
pytest                # the full suite, ~0.3s
```

`pre-commit install` wires the first two into your commits.

Python 3.11 is the floor and what the Docker image ships; CI also runs 3.12 and
3.13. If your local interpreter is newer than 3.11, CI is the authority.

## House rules

These are not style preferences, they are what keeps the project small:

- **The composition root is `docpipe/main.py`.** It is the only place concrete
  classes are instantiated. Everything else receives collaborators as
  constructor parameters. Do not import a concrete dependency inside a lower
  layer.
- **Heavy imports stay lazy.** `docling` and `ollama` are imported inside
  `build_components` or under `if TYPE_CHECKING:`. CI enforces this by
  installing the package with `--no-deps`, so a top-level `import docling`
  fails the build.
- **Configuration only through `docpipe/core/config.py`.** Nothing else reads
  `os.environ`. New knobs are pydantic `Field`s with a description, and a test
  fails if you forget to document them in `.env.example`.
- **Strict type hints everywhere**, with `from __future__ import annotations`
  at the top of every module. `mypy --strict` must pass.
- **Logging is structlog with event names, not sentences**:
  `logger.info("llm.refine.done", path=..., chars=...)`.
- **Tests inject fakes, never a live model.** See `tests/test_llm_service.py`
  and `tests/test_processor.py` for the pattern.
- **Output paths come from `FileRepository.output_key()` / `output_paths()`.**
  Never rebuild one by hand from `settings.markdown_dir`.

## Pull requests

Keep the diff to what the change needs. If you spot unrelated problems, mention
them in the PR description or open an issue rather than folding them in — it
makes review (and `git bisect`) much easier.

New non-trivial logic should come with the smallest test that fails if the
logic breaks. Trivial one-liners do not need one.
