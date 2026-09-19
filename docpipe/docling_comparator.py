"""Measures what the refining model actually contributed.

The pipeline hands Docling's extraction to a local model and keeps only the
model's answer, which makes the model's contribution invisible: nothing
distinguishes a run that stripped page furniture and fixed heading levels from
one that quietly rewrote the document. This module scores the two texts against
each other so that difference is a number somebody can look at.

It runs out of band. ``FileRepository.write_raw`` preserves Docling's output
next to the refined Markdown, and this module sweeps for pairs that have no
score yet. Nothing here sits on the path of producing a document, and anything
processed while comparison was switched off is picked up when it is switched
back on.

The scoring functions below the constants are pure and stdlib-only -- no model,
no network, no filesystem -- for the same reason ``services/prompts.py`` keeps
text handling away from transport: the part with the logic in it stays testable
on its own.
"""

from __future__ import annotations

import difflib
import json
import re
import threading
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from docpipe.core.logging import get_logger
from docpipe.models.documents import utc_now
from docpipe.storage.file_repository import FileRepository

logger = get_logger(__name__)

_TOKEN_RE = re.compile(r"\w+")
_FRONT_MATTER_RE = re.compile(r"\A---\r?\n.*?\r?\n---[ \t]*(?:\r?\n)*", re.DOTALL)
_HEADING_RE = re.compile(r"^ {0,3}#{1,6}\s", re.MULTILINE)
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE)
_LINK_RE = re.compile(r"!?\[[^\]]*\]\([^)]*\)")
_CODE_FENCE_RE = re.compile(r"^\s*```", re.MULTILINE)

SCORE_FIELDS = ("similarity", "jaccard", "retention", "introduced", "char_reduction")

# ponytail: difflib's matcher is superlinear and extraction output runs to tens
# of thousands of tokens, so the ordered comparison is capped to keep a sweep
# bounded. The set-based scores beside it are linear and always see the whole
# document. If a genuine whole-document ordering score is ever needed, reach for
# a Myers-diff library rather than raising this number.
_SEQUENCE_TOKEN_CAP = 20_000


def tokenize(markdown: str) -> list[str]:
    """Split text into lowercased word tokens, ignoring Markdown punctuation."""
    return _TOKEN_RE.findall(markdown.lower())


def strip_front_matter(markdown: str) -> str:
    """Return a Markdown body without its leading YAML front-matter block.

    Refined output carries front matter the extraction never had. Leaving it in
    would count the model's own metadata as invented prose and inflate every
    score that looks for new text.
    """
    return _FRONT_MATTER_RE.sub("", markdown, count=1)


@dataclass(frozen=True)
class DocumentStats:
    """Structural shape of one Markdown document."""

    chars: int
    tokens: int
    headings: int
    table_rows: int
    links: int
    code_blocks: int


@dataclass(frozen=True)
class Scores:
    """How the refined text relates to the extraction it came from.

    ``similarity``     Ordered token overlap: how much survived, in place.
    ``jaccard``        Vocabulary overlap, order-blind.
    ``retention``      Share of the extraction's vocabulary still present.
                       Low means the model dropped a lot.
    ``introduced``     Share of the output's vocabulary absent from the input.
                       This is the hallucination signal: a cleanup pass should
                       sit near zero, and anything higher is text the model
                       wrote rather than kept.
    ``char_reduction`` How much shorter the output is. Negative if the model
                       produced more text than it was given.

    No single number says whether a run was *good*: aggressive boilerplate
    removal and quiet rewriting both read as low ``similarity``. Reading
    ``introduced`` and the structural counts alongside it is what separates
    them.
    """

    similarity: float
    jaccard: float
    retention: float
    introduced: float
    char_reduction: float


@dataclass(frozen=True)
class Comparison:
    """One document's raw-vs-refined measurement, as written to disk."""

    key: str
    file_hash: str
    model: str
    compared_at: str
    raw: DocumentStats
    refined: DocumentStats
    scores: Scores

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def measure(markdown: str) -> DocumentStats:
    """Count the structural features of a Markdown document."""
    return DocumentStats(
        chars=len(markdown),
        tokens=len(tokenize(markdown)),
        headings=len(_HEADING_RE.findall(markdown)),
        table_rows=len(_TABLE_ROW_RE.findall(markdown)),
        links=len(_LINK_RE.findall(markdown)),
        code_blocks=len(_CODE_FENCE_RE.findall(markdown)) // 2,
    )


def score(raw: str, refined: str) -> Scores:
    """Score a refined document against the extraction it was produced from."""
    raw_tokens = tokenize(raw)
    refined_tokens = tokenize(refined)
    raw_set = set(raw_tokens)
    refined_set = set(refined_tokens)
    shared = raw_set & refined_set
    union = raw_set | refined_set

    matcher = difflib.SequenceMatcher(
        None,
        raw_tokens[:_SEQUENCE_TOKEN_CAP],
        refined_tokens[:_SEQUENCE_TOKEN_CAP],
        autojunk=False,
    )
    return Scores(
        similarity=_round(matcher.ratio()),
        jaccard=_round(len(shared) / len(union)) if union else 0.0,
        retention=_round(len(shared) / len(raw_set)) if raw_set else 0.0,
        introduced=(
            _round(len(refined_set - raw_set) / len(refined_set)) if refined_set else 0.0
        ),
        char_reduction=_round(1 - len(refined) / len(raw)) if raw else 0.0,
    )


def compare(
    key: str,
    raw_markdown: str,
    refined_markdown: str,
    file_hash: str,
    model: str,
) -> Comparison:
    """Build the full comparison record for one document."""
    body = strip_front_matter(refined_markdown)
    return Comparison(
        key=key,
        file_hash=file_hash,
        model=model,
        compared_at=utc_now(),
        raw=measure(raw_markdown),
        refined=measure(body),
        scores=score(raw_markdown, body),
    )


def _round(value: float) -> float:
    """Keep the written JSON stable and diffable."""
    return round(value, 4)


class DoclingComparator:
    """Sweeps for unscored documents and records how much the model changed them.

    Every method is safe to call from the CLI with the daemon stopped: the work
    is pure filesystem reads, so scoring needs neither Docling nor Ollama.
    """

    def __init__(
        self,
        repository: FileRepository,
        model: str,
        interval_s: float = 60.0,
    ) -> None:
        self._repository = repository
        self._model = model
        self._interval_s = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # --- Scoring -----------------------------------------------------------
    def pending_keys(self) -> Iterator[str]:
        """Yield keys whose comparison is missing or older than their output."""
        for key in self._repository.iter_raw_keys():
            markdown_path, _ = self._repository.output_paths(key)
            if not markdown_path.exists():
                continue  # still refining, or refining failed: nothing to score
            comparison_path = self._repository.comparison_path(key)
            if (
                comparison_path.exists()
                and comparison_path.stat().st_mtime >= markdown_path.stat().st_mtime
            ):
                continue  # already scored against this version of the output
            yield key

    def compare_key(self, key: str) -> Comparison | None:
        """Score one document and write its comparison file.

        Returns None if the pair could not be read; a measurement job has no
        business interrupting anything, so the failure is logged and skipped.
        """
        raw_path = self._repository.raw_path(key)
        markdown_path, metadata_path = self._repository.output_paths(key)
        try:
            raw_markdown = raw_path.read_text(encoding="utf-8")
            refined_markdown = markdown_path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("compare.read.failed", key=key, error=str(exc))
            return None

        comparison = compare(
            key=key,
            raw_markdown=raw_markdown,
            refined_markdown=refined_markdown,
            file_hash=_read_file_hash(metadata_path),
            # The configured model, not a recorded one -- nothing downstream
            # stores which tag produced a given output. In the daemon the sweep
            # follows processing by seconds, so this is accurate in practice;
            # after changing DOCPIPE_MODEL_TAG, old scores keep the old tag only
            # until their document is reprocessed.
            model=self._model,
        )

        destination = self._repository.comparison_path(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            destination.write_text(
                json.dumps(comparison.to_dict(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("compare.write.failed", key=key, error=str(exc))
            return None

        logger.info(
            "compare.scored",
            key=key,
            similarity=comparison.scores.similarity,
            introduced=comparison.scores.introduced,
        )
        return comparison

    def sweep(self) -> int:
        """Score every pending document. Returns how many were scored."""
        scored = sum(1 for key in list(self.pending_keys()) if self.compare_key(key))
        if scored:
            logger.info("compare.sweep.done", scored=scored)
        return scored

    def report(self) -> dict[str, Any]:
        """Aggregate every comparison on disk into fleet-wide averages."""
        records = [
            record
            for record in self._load_records()
            if isinstance(record.get("scores"), dict)
        ]
        if not records:
            return {"documents": 0, "models": [], "means": {}, "most_changed": []}

        means = {
            field: _round(
                sum(float(r["scores"].get(field, 0.0)) for r in records) / len(records)
            )
            for field in SCORE_FIELDS
        }
        ranked = sorted(
            records,
            key=lambda r: float(r["scores"].get("introduced", 0.0)),
            reverse=True,
        )
        return {
            "documents": len(records),
            "models": sorted({str(r.get("model", "unknown")) for r in records}),
            "means": means,
            "most_changed": [
                {
                    "key": str(r.get("key", "?")),
                    "introduced": r["scores"].get("introduced"),
                    "similarity": r["scores"].get("similarity"),
                }
                for r in ranked[:5]
            ],
        }

    def _load_records(self) -> Iterator[dict[str, Any]]:
        for path in self._repository.iter_comparisons():
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning(
                    "compare.report.unreadable", path=str(path), error=str(exc)
                )
                continue
            if isinstance(record, dict):
                yield record

    # --- Background lifecycle ----------------------------------------------
    def start_background(self) -> None:
        """Start sweeping on an interval in a daemon thread."""
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="docpipe-comparator", daemon=True
        )
        self._thread.start()
        logger.info("compare.started", interval_s=self._interval_s)

    def stop(self) -> None:
        """Stop the sweep thread, waiting briefly for the current pass to end."""
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=5.0)
        self._thread = None
        logger.info("compare.stopped")

    def _loop(self) -> None:
        # Waits before the first sweep so startup reconciliation gets a clear
        # run; wait() returns True only when stop() has been called.
        while not self._stop.wait(self._interval_s):
            try:
                self.sweep()
            except Exception as exc:  # measurement must never take the daemon down
                logger.error("compare.sweep.failed", error=repr(exc))


def _read_file_hash(metadata_path: Path) -> str:
    """Recover a document's content hash from its metadata sidecar."""
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return str(payload.get("file_hash", "")) if isinstance(payload, dict) else ""
