# syntax=docker/dockerfile:1

# ---------- Stage 1: builder ----------
FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Build deps that some scientific wheels may need at install time.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential && \
    rm -rf /var/lib/apt/lists/*

# Install into an isolated venv we can copy to the runtime stage.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# ---------- Stage 2: runtime ----------
FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

# Minimal runtime libs for document parsing.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 && \
    rm -rf /var/lib/apt/lists/*

# Non-root user.
RUN useradd --create-home --uid 1000 appuser
WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY docpipe/ ./docpipe/

# Defaults (override via compose / env).
ENV DOCPIPE_INPUT_DIR=/app/data/input \
    DOCPIPE_OUTPUT_DIR=/app/data/output \
    DOCPIPE_STATE_FILE=/app/data/.pipeline_state.json \
    DOCPIPE_OLLAMA_HOST=http://host.docker.internal:11434 \
    DOCPIPE_MODEL_TAG=mistral-nemo:12b \
    DOCPIPE_MAX_WORKERS=1 \
    DOCPIPE_WEB_HOST=0.0.0.0 \
    DOCPIPE_WEB_PORT=8000

RUN mkdir -p /app/data/input /app/data/output && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

CMD ["python", "-m", "docpipe.main"]
