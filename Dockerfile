FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

# Install system dependencies required by the ingestion pipeline:
# - libchromaprint-tools: provides `fpcalc` for acoustic fingerprinting
# - 7zip: provides `7z` for archive unpacking (.7z, .zip, .rar, .tar)
# - ffmpeg: audio decoding backend for chromaprint/fpcalc
RUN apt-get update && apt-get install -y --no-install-recommends \
    libchromaprint-tools \
    7zip \
    ffmpeg \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Enable bytecode compilation and copy mode for uv in container
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PATH="/app/.venv/bin:${PATH}"

# Install Python dependencies first for optimal layer caching
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-install-project

# Copy application source, tests, CI workflows, and base configuration templates
COPY src/ ./src/
COPY tests/ ./tests/
COPY .github/ ./.github/
COPY main.py README.md .env.example config.example.yaml docker-compose.yml docker-compose.example.yaml ./

# Ensure runtime storage and external music mount directories exist inside the container
RUN mkdir -p /app/storage/scratch /app/storage/library /var/lib/telegram-bot-api /mnt/music/library

CMD ["python", "main.py"]

