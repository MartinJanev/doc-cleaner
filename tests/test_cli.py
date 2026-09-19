"""Tests for the command-line entry point.

Only the paths that need neither Docling nor Ollama are covered here: argument
parsing, the ledger summary, and the early exits. ``process`` checks the path
before importing Docling, which is what keeps this suite fast.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from docpipe.cli import build_parser, main
from docpipe.models.documents import ProcessingState
from docpipe.storage.state_store import JsonStateStore


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point every DOCPIPE_ path at tmp_path so the CLI can touch nothing real."""
    monkeypatch.setenv("DOCPIPE_INPUT_DIR", str(tmp_path / "input"))
    monkeypatch.setenv("DOCPIPE_OUTPUT_DIR", str(tmp_path / "output"))
    monkeypatch.setenv("DOCPIPE_STATE_FILE", str(tmp_path / "state.json"))


def test_parser_exposes_the_documented_subcommands() -> None:
    parser = build_parser()
    assert parser.parse_args(["run", "--once"]).once is True
    assert parser.parse_args(["process", "a.pdf", "--dry-run"]).dry_run is True
    assert parser.parse_args(["status"]).command == "status"


def test_version_exits_cleanly() -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0


def test_status_on_an_empty_ledger(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["status"]) == 0
    assert "No documents recorded yet" in capsys.readouterr().out


def test_status_counts_states_and_lists_failures(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = JsonStateStore(state_file=tmp_path / "state.json")
    store.start("hash-ok", str(tmp_path / "input" / "good.pdf"))
    store.mark("hash-ok", ProcessingState.COMPLETED)
    store.start("hash-bad", str(tmp_path / "input" / "bad.pdf"))
    store.mark("hash-bad", ProcessingState.FAILED, error="docling exploded")

    # A ledger containing failures exits non-zero so scripts can notice.
    assert main(["status"]) == 1

    out = capsys.readouterr().out
    assert "COMPLETED      1" in out
    assert "FAILED         1" in out
    assert "bad.pdf: docling exploded" in out


def test_process_reports_a_missing_file_without_importing_docling(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["process", str(tmp_path / "nope.pdf")]) == 2
    assert "No such file" in capsys.readouterr().err
