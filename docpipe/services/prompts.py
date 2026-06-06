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

# --- Chunked refining (large documents) ------------------------------------
# Long documents are cleaned one fragment at a time so the model never has to
# echo the whole body back in a single response. Metadata is derived in a
# separate pass over the assembled result.

CLEAN_SYSTEM_PROMPT = (
    "You are a meticulous document-cleaning assistant inside a local RAG "
    "ingestion pipeline. You receive ONE FRAGMENT of Markdown that was "
    "machine-extracted from a PDF or DOCX. Clean it WITHOUT inventing content:\n"
    "1. Fix broken headings, merged words, stray page numbers, and hyphenated "
    "line breaks.\n"
    "2. Preserve all tables as valid Markdown pipe tables.\n"
    "3. Do NOT add a title, summary, or any commentary — return only the cleaned "
    "fragment, and never drop content.\n\n"
    "Respond with a SINGLE JSON object and nothing else:\n"
    "{\n"
    '  "markdown": string (the cleaned Markdown fragment)\n'
    "}"
)

METADATA_SYSTEM_PROMPT = (
    "You derive top-level properties for a document in a local RAG pipeline. You "
    "receive cleaned Markdown (which may be truncated). Respond with a SINGLE "
    "JSON object and nothing else:\n"
    "{\n"
    '  "title": string,\n'
    '  "summary": string (2-4 sentences),\n'
    '  "author": string ("Unknown" if not present),\n'
    '  "tags": array of 3-8 short lowercase keyword strings\n'
    "}"
)

CLEAN_PROMPT_TEMPLATE = (
    "Clean the following Markdown fragment. Return only the JSON object "
    "described in the system prompt.\n\n"
    "---- BEGIN FRAGMENT ----\n"
    "{markdown}\n"
    "---- END FRAGMENT ----"
)

METADATA_PROMPT_TEMPLATE = (
    "Derive the document properties from the Markdown below. Return only the "
    "JSON object described in the system prompt.\n\n"
    "---- BEGIN MARKDOWN ----\n"
    "{markdown}\n"
    "---- END MARKDOWN ----"
)


def build_user_prompt(markdown: str, max_chars: int) -> str:
    """Render the single-pass clean+analyze prompt, truncating long Markdown."""
    body = markdown if len(markdown) <= max_chars else markdown[:max_chars]
    return USER_PROMPT_TEMPLATE.format(markdown=body)


def build_clean_prompt(fragment: str) -> str:
    """Render the cleaning prompt for one Markdown fragment."""
    return CLEAN_PROMPT_TEMPLATE.format(markdown=fragment)


def build_metadata_prompt(markdown: str, max_chars: int) -> str:
    """Render the metadata prompt over an overview of the cleaned body."""
    body = markdown if len(markdown) <= max_chars else markdown[:max_chars]
    return METADATA_PROMPT_TEMPLATE.format(markdown=body)


def chunk_markdown(markdown: str, max_chars: int) -> list[str]:
    """Split Markdown into chunks of at most ``max_chars``, on block boundaries.

    Blocks (separated by blank lines) are packed greedily so headings stay with
    their surrounding text where possible. A single block larger than the budget
    is hard-split as a last resort so no content is ever dropped.
    """
    text = markdown.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    current = ""
    for block in re.split(r"\n\s*\n", text):
        block = block.strip()
        if not block:
            continue
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        if len(block) <= max_chars:
            current = block
        else:
            chunks.extend(
                block[i : i + max_chars] for i in range(0, len(block), max_chars)
            )
    if current:
        chunks.append(current)
    return chunks


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
