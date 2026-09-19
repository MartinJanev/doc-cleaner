"""Tests for the Ollama refining stage, driven by a fake client.

No Ollama server is involved: the fake records the prompts it was given and
replays canned JSON, which is enough to pin the single-pass/chunked branch, the
failure modes, and the startup preflight.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from docpipe.core.exceptions import LLMServiceError
from docpipe.models.documents import MarkdownDocument
from docpipe.services.llm_service import LLMService


class FakeClient:
    """Minimal stand-in for ``ollama.Client``."""

    def __init__(self, replies: list[Any], listed: Any = None) -> None:
        self._replies = list(replies)
        self._listed = listed if listed is not None else {"models": []}
        self.calls: list[dict[str, Any]] = []

    def chat(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        reply = self._replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return {"message": {"content": reply}}

    def list(self) -> Any:
        if isinstance(self._listed, Exception):
            raise self._listed
        return self._listed


def build_service(client: FakeClient, chunk_chars: int = 8_000) -> LLMService:
    return LLMService(
        client=client,
        model="test-model",
        max_chars=12_000,
        chunk_chars=chunk_chars,
        num_ctx=8_192,
        temperature=0.1,
    )


def document(markdown: str) -> MarkdownDocument:
    return MarkdownDocument(
        source_path=Path("/in/report.pdf"), file_hash="abc123", markdown=markdown
    )


# --- Preflight ---------------------------------------------------------------


def test_preflight_passes_when_model_is_installed() -> None:
    client = FakeClient([], listed={"models": [{"model": "test-model"}]})
    assert build_service(client).preflight() is None


def test_preflight_accepts_the_newer_object_response_shape() -> None:
    class Entry:
        model = "test-model"

    class Listed:
        def __init__(self) -> None:
            self.models = [Entry()]

    assert build_service(FakeClient([], listed=Listed())).preflight() is None


def test_preflight_treats_latest_tag_as_the_bare_name() -> None:
    client = FakeClient([], listed={"models": [{"model": "test-model:latest"}]})
    assert build_service(client).preflight() is None


def test_preflight_names_the_pull_command_for_a_missing_model() -> None:
    client = FakeClient([], listed={"models": [{"model": "other-model"}]})
    problem = build_service(client).preflight()
    assert problem is not None
    assert "ollama pull test-model" in problem


def test_preflight_reports_an_unreachable_server() -> None:
    client = FakeClient([], listed=ConnectionError("connection refused"))
    problem = build_service(client).preflight()
    assert problem is not None
    assert "Cannot reach Ollama" in problem


# --- Refining ----------------------------------------------------------------


def test_small_document_uses_a_single_pass() -> None:
    reply = json.dumps(
        {
            "title": "Quarterly Report",
            "summary": "A summary.",
            "author": "Ada",
            "tags": ["finance", "  ", "q3"],
            "markdown": "# Clean\n\nBody.",
        }
    )
    client = FakeClient([reply])
    result = build_service(client).refine(document("# Raw\n\nBody."))

    assert len(client.calls) == 1
    assert result.metadata.title == "Quarterly Report"
    assert result.metadata.tags == ["finance", "q3"]  # blank tag dropped
    assert result.markdown_with_front_matter.startswith("---\n")
    assert 'title: "Quarterly Report"' in result.markdown_with_front_matter
    assert result.markdown_with_front_matter.rstrip().endswith("Body.")


def test_large_document_cleans_in_chunks_then_derives_metadata() -> None:
    markdown = "\n\n".join(f"Block {i} {'x' * 60}" for i in range(10))
    cleans = [json.dumps({"markdown": f"cleaned-{i}"}) for i in range(5)]
    metadata = json.dumps({"title": "T", "summary": "S", "author": "A", "tags": ["one"]})
    client = FakeClient([*cleans, metadata])

    result = build_service(client, chunk_chars=200).refine(document(markdown))

    # One call per chunk, plus exactly one metadata pass at the end.
    assert len(client.calls) > 1
    assert "cleaned-0" in result.markdown_with_front_matter
    assert result.metadata.title == "T"


def test_metadata_falls_back_to_defaults_when_fields_are_missing() -> None:
    client = FakeClient([json.dumps({"markdown": "body"})])
    result = build_service(client).refine(document("raw"))
    assert result.metadata.title == "Untitled"
    assert result.metadata.author == "Unknown"
    assert result.metadata.tags == []


def test_unparseable_response_raises_llm_service_error() -> None:
    client = FakeClient(["not json at all"])
    with pytest.raises(LLMServiceError, match="Could not parse"):
        build_service(client).refine(document("raw"))


def test_transport_failure_raises_llm_service_error() -> None:
    client = FakeClient([RuntimeError("socket closed")])
    with pytest.raises(LLMServiceError, match="Ollama call failed"):
        build_service(client).refine(document("raw"))


def test_empty_response_raises_llm_service_error() -> None:
    client = FakeClient([""])
    with pytest.raises(LLMServiceError, match="empty message"):
        build_service(client).refine(document("raw"))


def test_num_ctx_and_temperature_reach_the_client() -> None:
    client = FakeClient([json.dumps({"markdown": "body"})])
    build_service(client).refine(document("raw"))
    options = client.calls[0]["options"]
    assert options == {"temperature": 0.1, "num_ctx": 8_192}
    assert client.calls[0]["format"] == "json"
