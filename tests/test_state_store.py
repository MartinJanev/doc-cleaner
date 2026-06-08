"""Unit tests for the JSON state ledger (no external services)."""

from __future__ import annotations

from pathlib import Path

import pytest

from docpipe.models.documents import ProcessingState
from docpipe.storage.state_store import JsonStateStore


@pytest.fixture()
def store(tmp_path: Path) -> JsonStateStore:
    return JsonStateStore(state_file=tmp_path / "state.json")


def test_unknown_file_should_process(store: JsonStateStore) -> None:
    assert store.should_process("hash-a", max_attempts=3) is True


def test_completed_file_is_skipped(store: JsonStateStore) -> None:
    store.start("hash-a", "/in/a.pdf")
    store.mark("hash-a", ProcessingState.COMPLETED)
    assert store.is_completed("hash-a") is True
    assert store.should_process("hash-a", max_attempts=3) is False


def test_failed_respects_attempt_budget(store: JsonStateStore) -> None:
    store.start("hash-a", "/in/a.pdf")  # attempts -> 1
    store.mark("hash-a", ProcessingState.FAILED, error="boom")
    assert store.should_process("hash-a", max_attempts=3) is True

    store.start("hash-a", "/in/a.pdf")  # attempts -> 2
    store.mark("hash-a", ProcessingState.FAILED, error="boom")
    store.start("hash-a", "/in/a.pdf")  # attempts -> 3
    store.mark("hash-a", ProcessingState.FAILED, error="boom")
    assert store.should_process("hash-a", max_attempts=3) is False


def test_state_persists_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    first = JsonStateStore(state_file=path)
    first.start("hash-a", "/in/a.pdf")
    first.mark("hash-a", ProcessingState.COMPLETED)

    reloaded = JsonStateStore(state_file=path)
    assert reloaded.is_completed("hash-a") is True
    record = reloaded.get("hash-a")
    assert record is not None
    assert record.attempts == 1


def test_transitions_record_error_and_clear_on_restart(store: JsonStateStore) -> None:
    store.start("hash-a", "/in/a.pdf")
    store.mark("hash-a", ProcessingState.FAILED, error="kaboom")
    record = store.get("hash-a")
    assert record is not None and record.error == "kaboom"

    restarted = store.start("hash-a", "/in/a.pdf")
    assert restarted.error is None
    assert restarted.state is ProcessingState.PENDING


def test_delete_removes_record_and_allows_reprocess(store: JsonStateStore) -> None:
    store.start("hash-a", "/in/a.pdf")
    store.mark("hash-a", ProcessingState.COMPLETED)
    assert store.should_process("hash-a", max_attempts=3) is False

    assert store.delete("hash-a") is True
    assert store.get("hash-a") is None
    assert store.should_process("hash-a", max_attempts=3) is True
    assert store.delete("hash-a") is False  # already gone


def test_records_returns_snapshot(store: JsonStateStore) -> None:
    store.start("hash-a", "/in/a.pdf")
    store.start("hash-b", "/in/b.docx")

    snapshot = store.records()
    assert set(snapshot) == {"hash-a", "hash-b"}

    snapshot.clear()  # mutating the copy must not affect the store
    assert set(store.records()) == {"hash-a", "hash-b"}
