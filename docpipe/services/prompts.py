"""Prompt templates and response helpers for the LLM refining stage.

Kept separate from ``LLMService`` so prompts can be unit-tested and iterated on
without touching transport logic.
"""

from __future__ import annotations

import json
import re
from typing import Any

SYSTEM_PROMPT = (
    "You are a meticulous document-cleaning assistant inside a local RAG "
    "ingestion pipeline. You receive Markdown that was machine-extracted from a "
    "PDF or DOCX. Your job is to:\n"
    "1. Fix obvious structural defects (broken headings, merged words, stray "
    "page numbers, hyphenated line breaks) WITHOUT inventing content.\n"
    "2. Preserve all tables as valid Markdown pipe tables.\n"
    "3. Derive top-level document properties.\n\n"
    "You MUST respond with a SINGLE JSON object and nothing else. Do not wrap "
    "it in prose. The JSON schema is:\n"
    "{\n"
    '  "title": string,\n'
    '  "summary": string (2-4 sentences),\n'
    '  "author": string ("Unknown" if not present),\n'
    '  "tags": array of 3-8 short lowercase keyword strings,\n'
    '  "markdown": string (the cleaned Markdown body, no front matter)\n'
    "}"
)

USER_PROMPT_TEMPLATE = (
    "Clean and analyze the following extracted Markdown. Return only the JSON "
    "object described in the system prompt.\n\n"
    "---- BEGIN MARKDOWN ----\n"
    "{markdown}\n"
    "---- END MARKDOWN ----"
)


def build_user_prompt(markdown: str, max_chars: int) -> str:
    """Render the user prompt, truncating overly long Markdown."""
    body = markdown if len(markdown) <= max_chars else markdown[:max_chars]
    return USER_PROMPT_TEMPLATE.format(markdown=body)


def parse_llm_json(content: str) -> dict[str, Any]:
    """Extract the JSON object from a model response.

    Tolerates models that wrap JSON in ```json fences or add stray prose by
    locating the outermost balanced ``{...}`` block.

    Raises:
        ValueError: If no JSON object can be parsed.
    """
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
    candidate = fenced.group(1) if fenced else _extract_braced(content)
    if candidate is None:
        raise ValueError("No JSON object found in LLM response")
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM response was not valid JSON: {exc}") from exc


def _extract_braced(text: str) -> str | None:
    """Return the first balanced brace-delimited substring, if any."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for idx in range(start, len(text)):
        char = text[idx]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : idx + 1]
    return None


def render_front_matter(metadata: dict[str, Any], source_file: str) -> str:
    """Render a YAML front-matter block from metadata fields.

    Strings are quoted to stay valid YAML; tags render as an inline list.
    """
    tags = metadata.get("tags") or []
    tag_list = ", ".join(_yaml_str(str(t)) for t in tags)
    lines = [
        "---",
        f"title: {_yaml_str(str(metadata.get('title', 'Untitled')))}",
        f"author: {_yaml_str(str(metadata.get('author', 'Unknown')))}",
        f"summary: {_yaml_str(str(metadata.get('summary', '')))}",
        f"tags: [{tag_list}]",
        f"source_file: {_yaml_str(source_file)}",
        "---",
        "",
    ]
    return "\n".join(lines)


def _yaml_str(value: str) -> str:
    """Quote a scalar for safe single-line YAML embedding."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    return f'"{escaped}"'
