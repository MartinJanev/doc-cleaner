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
    SYSTEM_PROMPT,
    build_user_prompt,
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
        temperature: float = 0.1,
    ) -> None:
        self._client = client
        self._model = model
        self._max_chars = max_chars
        self._temperature = temperature

    def refine(self, document: MarkdownDocument) -> RestructuredDocument:
        """Clean Markdown and prepend YAML front matter via the local LLM.

        Raises:
            LLMServiceError: On transport failure or unparseable output.
        """
        logger.info("llm.refine.start", path=str(document.source_path))
        user_prompt = build_user_prompt(document.markdown, self._max_chars)

        try:
            response = self._client.chat(
                model=self._model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                format="json",
                options={"temperature": self._temperature},
            )
        except Exception as exc:  # ollama raises ResponseError / ConnectionError
            raise LLMServiceError(
                f"Ollama call failed for {document.source_path.name}: {exc}"
            ) from exc

        content = self._extract_content(response)
        try:
            parsed = parse_llm_json(content)
        except ValueError as exc:
            raise LLMServiceError(
                f"Could not parse LLM response for "
                f"{document.source_path.name}: {exc}"
            ) from exc

        metadata = self._to_metadata(parsed)
        cleaned_body = str(parsed.get("markdown") or document.markdown).strip()
        front_matter = render_front_matter(
            parsed, source_file=document.source_path.name
        )

        logger.info(
            "llm.refine.done",
            path=str(document.source_path),
            title=metadata.title,
            tags=metadata.tags,
        )
        return RestructuredDocument(
            source_path=document.source_path,
            file_hash=document.file_hash,
            metadata=metadata,
            markdown_with_front_matter=f"{front_matter}{cleaned_body}\n",
        )

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
