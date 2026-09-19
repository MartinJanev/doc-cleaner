"""Tests for settings validation and configuration documentation."""

from __future__ import annotations

from pathlib import Path

import pytest

from docpipe.core.config import Settings

_ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"


def test_env_example_documents_every_setting() -> None:
    """Every Settings field must appear in .env.example.

    The model default, the chunk/context knobs, and the suffix list have all
    drifted between config.py and the docs before. This pins them together so a
    new field cannot be added without documenting it.
    """
    documented = _ENV_EXAMPLE.read_text(encoding="utf-8")
    missing = [
        f"DOCPIPE_{name.upper()}"
        for name in Settings.model_fields
        if f"DOCPIPE_{name.upper()}=" not in documented
    ]
    assert not missing, f"undocumented settings in .env.example: {missing}"


def test_env_example_model_tag_matches_the_code_default() -> None:
    """The documented model must be the one the pipeline actually runs.

    A mismatch here means the README tells a new user to pull the wrong model
    and their first document fails.
    """
    documented = _ENV_EXAMPLE.read_text(encoding="utf-8")
    assert f"DOCPIPE_MODEL_TAG={Settings().model_tag}" in documented


def test_suffixes_accept_comma_separated_env_string() -> None:
    assert Settings(supported_suffixes="pdf, .DOCX").supported_suffixes == (
        ".pdf",
        ".docx",
    )


def test_chunk_chars_must_fit_the_context_window() -> None:
    """A chunk bigger than num_ctx truncates model output with no error.

    Ollama silently cuts the response short, so the corruption only shows up in
    the written Markdown. Failing at startup is the whole point of this guard.
    """
    with pytest.raises(ValueError, match="DOCPIPE_LLM_NUM_CTX"):
        Settings(llm_chunk_chars=30_000, llm_num_ctx=8_192)


def test_compatible_chunk_and_context_sizes_are_accepted() -> None:
    settings = Settings(llm_chunk_chars=30_000, llm_num_ctx=16_384)
    assert settings.llm_chunk_chars == 30_000


def test_defaults_are_self_consistent() -> None:
    """The shipped defaults must satisfy the guard they enforce."""
    assert Settings().llm_chunk_chars // 2 <= Settings().llm_num_ctx
