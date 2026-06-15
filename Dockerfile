FROM python:3.11-slim

# ---------------------------------------------------------------------------
# System dependencies
# ---------------------------------------------------------------------------
# The `anki` PyPI package ships manylinux wheels with the Rust backend
# statically linked. In practice no compiler is needed. We add only:
#   - curl: used by the HEALTHCHECK instruction below
#   - ca-certificates: required for TLS when syncing to AnkiWeb
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# Python dependencies
# ---------------------------------------------------------------------------
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---------------------------------------------------------------------------
# Application source
# ---------------------------------------------------------------------------
COPY src/ ./src/
COPY templates/ ./templates/
COPY static/ ./static/

# ---------------------------------------------------------------------------
# Runtime user note
# ---------------------------------------------------------------------------
# We do NOT bake a fixed UID into the image because in production the server
# must read/write a collection owned by UID 1005 / GID 136 (the Tilts anki
# user). Set the runtime UID via `docker run --user 1005:136` or the compose
# `user:` key. The image itself runs as root during build only.

# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------
EXPOSE 8765

# ---------------------------------------------------------------------------
# Healthcheck
# ---------------------------------------------------------------------------
# Hit the dedicated /health endpoint (GET, returns {"status":"ok"} with 200).
# This is simpler and more robust than parsing the AnkiConnect JSON envelope.
HEALTHCHECK --interval=10s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fs http://localhost:8765/health || exit 1

# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
# SINGLE WORKER — DO NOT change to a multi-worker WSGI server.
# The Anki Collection is an SQLite database with a single write lock.
# Running more than one process against the same .anki2 file will corrupt it.
CMD ["python", "-m", "src.server"]
