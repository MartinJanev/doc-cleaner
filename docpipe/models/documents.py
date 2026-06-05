"""Domain models shared across the pipeline layers.

These are deliberately plain dataclasses (and one Enum) with no behaviour beyond
serialization helpers, so they can cross every layer boundary without dragging
in service dependencies.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional


class ProcessingState(str, Enum):
    """Lifecycle states recorded in the ledger for each document."""

    PENDING = "PENDING"
    EXTRACTED = "EXTRACTED"
    RESTRUCTURED = "RESTRUCTURED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


def _utc_now() -> str:
    """ISO-8601 UTC timestamp string."""
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class MarkdownDocument:
    """Raw Markdown produced by the extraction stage."""

    source_path: Path
    file_hash: str
    markdown: str


@dataclass(frozen=True)
class DocumentMetadata:
    """Top-level properties extracted by the LLM."""

    title: str
    summary: str
    author: str
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RestructuredDocument:
    """Final artifact: cleaned Markdown body plus parsed metadata."""

    source_path: Path
    file_hash: str
    metadata: DocumentMetadata
    markdown_with_front_matter: str


@dataclass
class DocumentRecord:
    """A single ledger entry tracking one document by content hash."""

    file_hash: str
    source_path: str
    state: ProcessingState = ProcessingState.PENDING
    attempts: int = 0
    error: Optional[str] = None
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["state"] = self.state.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DocumentRecord":
        return cls(
            file_hash=data["file_hash"],
            source_path=data["source_path"],
            state=ProcessingState(data.get("state", ProcessingState.PENDING.value)),
            attempts=int(data.get("attempts", 0)),
            error=data.get("error"),
            created_at=data.get("created_at", _utc_now()),
            updated_at=data.get("updated_at", _utc_now()),
        )
