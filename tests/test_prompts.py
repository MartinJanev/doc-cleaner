"""Unit tests for prompt rendering and response parsing."""

from __future__ import annotations

import pytest

from docpipe.services.prompts import (
    build_user_prompt,
    parse_llm_json,
    render_front_matter,
)


def test_build_user_prompt_truncates() -> None:
    long_md = "x" * 100
    prompt = build_user_prompt(long_md, max_chars=10)
    assert "x" * 10 in prompt
    assert "x" * 11 not in prompt


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
