"""Unit tests for prompt rendering and response parsing."""

from __future__ import annotations

import pytest

from docpipe.services.prompts import (
    build_user_prompt,
    chunk_markdown,
    parse_llm_json,
    render_front_matter,
)


def test_build_user_prompt_truncates() -> None:
    long_md = "x" * 100
    prompt = build_user_prompt(long_md, max_chars=10)
    assert "x" * 10 in prompt
    assert "x" * 11 not in prompt


def test_chunk_markdown_short_returns_single_chunk() -> None:
    assert chunk_markdown("# Title\n\nshort body", max_chars=1000) == [
        "# Title\n\nshort body"
    ]


def test_chunk_markdown_empty_returns_empty_list() -> None:
    assert chunk_markdown("   \n\n  ", max_chars=100) == []


def test_chunk_markdown_packs_blocks_within_budget() -> None:
    blocks = [f"block-{i} " + "x" * 40 for i in range(6)]
    text = "\n\n".join(blocks)
    chunks = chunk_markdown(text, max_chars=100)
    assert len(chunks) > 1
    assert all(len(chunk) <= 100 for chunk in chunks)
    # No content is dropped: every block survives somewhere in the output.
    joined = "\n\n".join(chunks)
    for i in range(6):
        assert f"block-{i}" in joined


def test_chunk_markdown_hard_splits_oversized_block() -> None:
    text = "y" * 250
    chunks = chunk_markdown(text, max_chars=100)
    assert all(len(chunk) <= 100 for chunk in chunks)
    assert "".join(chunks) == text


def test_parse_plain_json() -> None:
    data = parse_llm_json('{"title": "T", "tags": ["a", "b"]}')
    assert data["title"] == "T"
    assert data["tags"] == ["a", "b"]


def test_parse_fenced_json() -> None:
    raw = 'Here you go:\n```json\n{"title": "T", "author": "Jane"}\n```\nThanks!'
    data = parse_llm_json(raw)
    assert data["author"] == "Jane"


def test_parse_json_with_surrounding_prose() -> None:
    raw = 'Sure! {"title": "Nested {brace}", "summary": "ok"} done'
    data = parse_llm_json(raw)
    assert data["title"] == "Nested {brace}"


def test_parse_invalid_raises() -> None:
    with pytest.raises(ValueError):
        parse_llm_json("no json here")


def test_render_front_matter_quotes_and_lists() -> None:
    fm = render_front_matter(
        {
            "title": 'Quarterly "Report"',
            "author": "Jane",
            "summary": "Line one\nline two",
            "tags": ["finance", "q1"],
        },
        source_file="report.pdf",
    )
    assert fm.startswith("---\n")
    assert fm.rstrip().endswith("---")
    assert 'title: "Quarterly \\"Report\\""' in fm
    assert "tags: [\"finance\", \"q1\"]" in fm
    assert 'source_file: "report.pdf"' in fm
    # newlines collapsed to keep YAML single-line scalars valid
    assert "Line one line two" in fm
