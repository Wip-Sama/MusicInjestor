# Stage 1: Build the React / Vite Telegram Mini App frontend
FROM node:22-bookworm-slim AS webapp-builder

WORKDIR /app/webapp

# Install frontend dependencies first for optimal caching
COPY webapp/package.json webapp/package-lock.json ./
RUN npm ci

# Copy webapp source and configuration files
COPY webapp/ ./

# Compile production bundle into /app/webapp/dist
RUN npm run build

# Stage 2: Production Python runtime with uv and audio dependencies
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
    PATH="/app/.venv/bin:${PATH}" \
    WEB_APP_DEV_MODE=false \
    WEB_APP_STATIC_DIR=/app/webapp/dist

# Install Python dependencies first for optimal layer caching
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-install-project

# Copy built static frontend assets from webapp-builder stage
COPY --from=webapp-builder /app/webapp/dist /app/webapp/dist

# Copy application source, tests, CI workflows, and base configuration templates
COPY src/ ./src/
COPY tests/ ./tests/
COPY .github/ ./.github/
COPY main.py README.md .env.example config.example.yaml docker-compose.yml docker-compose.example.yaml .actrc Dockerfile ./

# Ensure runtime storage and external music mount directories exist inside the container
RUN mkdir -p /app/storage/scratch /app/storage/library /var/lib/telegram-bot-api /mnt/music/library

CMD ["python", "main.py"]

