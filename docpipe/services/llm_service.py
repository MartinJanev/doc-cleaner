"""Ollama orchestration for the refining / metadata stage.

The ``ollama.Client`` is injected (DI) so the host URL is configured once in the
composition root and the service stays trivially testable. The service turns a
raw ``MarkdownDocument`` into a ``RestructuredDocument`` with YAML front matter.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from docpipe.core.exceptions import LLMServiceError
from docpipe.core.logging import get_logger
from docpipe.models.documents import (
    DocumentMetadata,
    MarkdownDocument,
    RestructuredDocument,
)
from docpipe.services.prompts import (
    CLEAN_SYSTEM_PROMPT,
    METADATA_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    build_clean_prompt,
    build_metadata_prompt,
    build_user_prompt,
    chunk_markdown,
    parse_llm_json,
    render_front_matter,
)

if TYPE_CHECKING:
    from ollama import Client

logger = get_logger(__name__)


class LLMService:
    """Sends Markdown to a local Ollama model and injects metadata."""

    def __init__(
        self,
        client: "Client",
        model: str,
        max_chars: int,
        chunk_chars: int,
        num_ctx: int,
        temperature: float = 0.1,
    ) -> None:
        self._client = client
        self._model = model
        self._max_chars = max_chars
        self._chunk_chars = chunk_chars
        self._num_ctx = num_ctx
        self._temperature = temperature

    def refine(self, document: MarkdownDocument) -> RestructuredDocument:
        """Clean Markdown and prepend YAML front matter via the local LLM.

        Small documents are cleaned and analyzed in a single pass. Documents
        larger than ``chunk_chars`` are cleaned fragment by fragment (so the
        model never has to emit the whole body at once), then analyzed in a
        separate metadata pass.

        Raises:
            LLMServiceError: On transport failure or unparseable output.
        """
        markdown = document.markdown
        logger.info(
            "llm.refine.start", path=str(document.source_path), chars=len(markdown)
        )

        if len(markdown) <= self._chunk_chars:
            parsed = self._chat_json(
                SYSTEM_PROMPT, build_user_prompt(markdown, self._max_chars), document
            )
            cleaned_body = str(parsed.get("markdown") or markdown).strip()
            metadata_source = parsed
        else:
            cleaned_body = self._clean_in_chunks(markdown, document)
            metadata_source = self._chat_json(
                METADATA_SYSTEM_PROMPT,
                build_metadata_prompt(cleaned_body, self._max_chars),
                document,
            )

        metadata = self._to_metadata(metadata_source)
        front_matter = render_front_matter(
            metadata_source, source_file=document.source_path.name
        )

        logger.info(
            "llm.refine.done",
            path=str(document.source_path),
            title=metadata.title,
            tags=metadata.tags,
            out_chars=len(cleaned_body),
        )
        return RestructuredDocument(
            source_path=document.source_path,
            file_hash=document.file_hash,
            metadata=metadata,
            markdown_with_front_matter=f"{front_matter}{cleaned_body}\n",
        )

    def _clean_in_chunks(
        self, markdown: str, document: MarkdownDocument
    ) -> str:
        """Clean a large document one fragment at a time and reassemble it."""
        chunks = chunk_markdown(markdown, self._chunk_chars)
        logger.info(
            "llm.refine.chunked", path=str(document.source_path), chunks=len(chunks)
        )
        cleaned_parts: list[str] = []
        for index, chunk in enumerate(chunks, start=1):
            parsed = self._chat_json(
                CLEAN_SYSTEM_PROMPT, build_clean_prompt(chunk), document
            )
            part = str(parsed.get("markdown") or chunk).strip()
            if part:
                cleaned_parts.append(part)
            logger.info(
                "llm.refine.chunk",
                path=str(document.source_path),
                index=index,
                total=len(chunks),
            )
        return "\n\n".join(cleaned_parts)

    def _chat_json(
        self, system_prompt: str, user_prompt: str, document: MarkdownDocument
    ) -> dict[str, Any]:
        """Run one JSON chat round-trip against Ollama and parse the result."""
        try:
            response = self._client.chat(
                model=self._model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                format="json",
                options={
                    "temperature": self._temperature,
                    "num_ctx": self._num_ctx,
                },
            )
        except Exception as exc:  # ollama raises ResponseError / ConnectionError
            raise LLMServiceError(
                f"Ollama call failed for {document.source_path.name}: {exc}"
            ) from exc

        content = self._extract_content(response)
        try:
            return parse_llm_json(content)
        except ValueError as exc:
            raise LLMServiceError(
                f"Could not parse LLM response for "
                f"{document.source_path.name}: {exc}"
            ) from exc

    @staticmethod
    def _extract_content(response: Any) -> str:
        """Pull the assistant message text from an Ollama chat response."""
        try:
            message = response["message"] if isinstance(response, dict) else response.message
            content = message["content"] if isinstance(message, dict) else message.content
        except (KeyError, AttributeError, TypeError) as exc:
            raise LLMServiceError(f"Unexpected Ollama response shape: {exc}") from exc
        if not content:
            raise LLMServiceError("Ollama returned an empty message")
        return content

    @staticmethod
    def _to_metadata(parsed: dict[str, Any]) -> DocumentMetadata:
        raw_tags = parsed.get("tags") or []
        tags = [str(t).strip() for t in raw_tags if str(t).strip()]
        return DocumentMetadata(
            title=str(parsed.get("title") or "Untitled").strip(),
            summary=str(parsed.get("summary") or "").strip(),
            author=str(parsed.get("author") or "Unknown").strip(),
            tags=tags,
        )
