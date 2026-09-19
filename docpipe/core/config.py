"""Runtime configuration.

All knobs are environment-driven so the same image runs identically on a host
or inside Docker. The ``Settings`` instance is built once in ``main`` and
injected downward; nothing else should read ``os.environ`` directly.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Active Ollama model (pull first: ``ollama pull <tag>``).
# Other choices suitable for doc cleanup + JSON metadata:
#   "llama3"           - baseline, fast, weaker JSON
#   "llama3.1:8b"      - solid upgrade, similar RAM
#   "qwen2.5:7b"       - strong JSON + instructions, lighter RAM
#   "qwen2.5:14b"      - best quality if you have 16 GB+ RAM
#   "mistral-nemo:12b" - strong mid-size model (current choice)
DOCPIPE_MODEL = "qwen2.5:14b"


class Settings(BaseSettings):
    """Strongly-typed, validated pipeline settings."""

    model_config = SettingsConfigDict(
        env_prefix="DOCPIPE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Filesystem ---------------------------------------------------------
    input_dir: Path = Field(
        default=Path("data/input"),
        description="Directory watched for incoming documents.",
    )
    output_dir: Path = Field(
        default=Path("data/output"),
        description="Root directory for generated artifacts.",
    )
    state_file: Path = Field(
        default=Path(".pipeline_state.json"),
        description="JSON ledger tracking per-document processing state.",
    )

    # --- Ollama / LLM -------------------------------------------------------
    ollama_host: str = Field(
        default="http://localhost:11434",
        description="Base URL of the local Ollama server.",
    )
    model_tag: str = Field(
        default=DOCPIPE_MODEL,
        description="Ollama model used for the refining / metadata pass.",
    )
    llm_max_chars: int = Field(
        default=12_000,
        ge=1_000,
        description="Characters of Markdown used for the metadata pass. Small "
        "documents are still refined in a single pass within this budget.",
    )
    llm_chunk_chars: int = Field(
        default=8_000,
        ge=1_000,
        description="Documents larger than this are cleaned in chunks of roughly "
        "this size, lifting the single-pass output ceiling for long files.",
    )
    llm_num_ctx: int = Field(
        default=8_192,
        ge=512,
        description="Token context window requested from Ollama. Must comfortably "
        "fit one chunk plus its cleaned output; raise it for bigger chunks.",
    )
    llm_timeout_s: int = Field(
        default=300,
        ge=1,
        description="Per-request timeout for the Ollama call, in seconds.",
    )
    llm_temperature: float = Field(default=0.1, ge=0.0, le=2.0)

    # --- Concurrency / watcher ---------------------------------------------
    max_workers: int = Field(
        default=1,
        ge=1,
        description="Worker threads consuming the job queue. Keep low when "
        "Docling and Ollama share the same machine.",
    )
    debounce_seconds: float = Field(
        default=2.0,
        ge=0.0,
        description="Quiet period a file must be stable for before processing.",
    )
    max_attempts: int = Field(
        default=3,
        ge=1,
        description="How many times a FAILED document is retried on restart.",
    )
    supported_suffixes: tuple[str, ...] = Field(
        default=(".pdf", ".docx"),
        description="File extensions the pipeline will process.",
    )

    # --- Web interface ------------------------------------------------------
    web_enabled: bool = Field(
        default=True,
        description="Serve the web UI alongside the watcher. Disable for a "
        "headless watcher-only daemon (e.g. some Docker setups).",
    )
    web_host: str = Field(
        default="127.0.0.1",
        description="Interface the web server binds to. Use 0.0.0.0 to expose "
        "it outside the host (e.g. inside Docker).",
    )
    web_port: int = Field(
        default=8000,
        ge=1,
        le=65_535,
        description="Port the web server listens on.",
    )
    web_max_upload_mb: int = Field(
        default=200,
        ge=1,
        description="Maximum size, in megabytes, accepted for a single upload.",
    )

    # --- Logging ------------------------------------------------------------
    log_level: str = Field(default="INFO")
    log_json: bool = Field(
        default=True,
        description="Emit JSON logs (True) or human-readable console logs.",
    )

    @field_validator("supported_suffixes", mode="before")
    @classmethod
    def _normalize_suffixes(cls, value: object) -> object:
        """Accept comma-separated env strings and normalize to lowercase dots."""
        if isinstance(value, str):
            parts = [p.strip() for p in value.split(",") if p.strip()]
        elif isinstance(value, (list, tuple)):
            parts = [str(p).strip() for p in value if str(p).strip()]
        else:
            return value
        return tuple(p.lower() if p.startswith(".") else f".{p.lower()}" for p in parts)

    @model_validator(mode="after")
    def _chunks_must_fit_the_context_window(self) -> Settings:
        """Refuse a chunk size the context window cannot hold.

        The cleaning call sends one chunk and asks the model to echo a cleaned
        version back, so the window has to hold both. At roughly four characters
        per token that is ``chunk_chars / 2`` tokens. Getting this wrong does not
        raise anywhere downstream — Ollama simply truncates the response, which
        corrupts the output Markdown silently. Failing at startup is far kinder.
        """
        needed = self.llm_chunk_chars // 2
        if needed > self.llm_num_ctx:
            raise ValueError(
                f"DOCPIPE_LLM_CHUNK_CHARS={self.llm_chunk_chars} needs about "
                f"{needed} context tokens (one chunk in, one cleaned chunk out) "
                f"but DOCPIPE_LLM_NUM_CTX is {self.llm_num_ctx}. Raise NUM_CTX to "
                f"at least {needed}, or lower CHUNK_CHARS to "
                f"{self.llm_num_ctx * 2}."
            )
        return self

    @property
    def markdown_dir(self) -> Path:
        """Destination for refined Markdown documents."""
        return self.output_dir / "markdown"

    @property
    def metadata_dir(self) -> Path:
        """Destination for structured metadata sidecar files."""
        return self.output_dir / "metadata"
