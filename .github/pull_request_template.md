## What this changes

<!-- One or two sentences. Link the issue if there is one. -->

## Why

<!-- The problem this solves. -->

## Checklist

- [ ] `ruff check . && ruff format --check . && mypy && pytest` all pass
- [ ] New non-trivial logic has the smallest test that fails without it
- [ ] New settings are pydantic `Field`s and documented in `.env.example`
- [ ] The diff is limited to what this change needs
