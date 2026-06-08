"""Domain-specific exception hierarchy.

Every failure mode in the pipeline maps to one of these types so that the
orchestration layer can apply discrete error boundaries per document without
leaking implementation details from the services.
"""

from __future__ import annotations


class PipelineError(Exception):
    """Base class for all pipeline errors."""


class ConfigurationError(PipelineError):
    """Raised when the runtime configuration is invalid or incomplete."""


class ExtractionError(PipelineError):
    """Raised when Docling fails to convert a document into Markdown."""


class LLMServiceError(PipelineError):
    """Raised when the Ollama call fails or returns an unparseable response."""


class StorageError(PipelineError):
    """Raised when reading inputs or writing outputs / the state ledger fails."""


class UploadTooLargeError(StorageError):
    """Raised when an uploaded file exceeds the configured size limit."""
