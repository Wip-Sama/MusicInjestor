# Automated Music Ingestion Bot

Self-hosted music library ingestion engine powered by **Python 3.14**, **aiogram 3.x**, **SQLite (WAL mode)**, and the official **Local Telegram Bot API (`--local` mode)** supporting lossless audio and multi-disc archives up to **2 GB**.

## Architecture Highlights

The codebase follows a **Hexagonal Modular Monolith** layout under `src/` so the domain engine (`src/core/`) has zero dependency on Telegram (`aiogram`) or SQLite (`aiosqlite`) and can be extracted into a standalone microservice or paired with a Web UI at any time:

- [`src/core/`](file:///d:/Programming/musicDownloader/src/core/__init__.py): Pure domain models, `Protocol` ports, archive extraction (`7z`), acoustic fingerprinting (`fpcalc`), deduplication, metadata confidence scoring, LRCLIB client, and path organization.
- [`src/db/`](file:///d:/Programming/musicDownloader/src/db/__init__.py): Async SQLite (WAL mode) catalog and job state repository implementing `CatalogRepositoryPort`.
- [`src/bot/`](file:///d:/Programming/musicDownloader/src/bot/__init__.py): Telegram interface layer (`aiogram` 3.x), FSM states, inline verification keyboards, and `--local` Bot API container-to-host path resolution.
- [`src/config/`](file:///d:/Programming/musicDownloader/src/config/__init__.py): Typed environment and `.env` configuration.

For detailed architectural specifications and AI agent handoff status, see:
- [`music_ingestion_bot_specification.md`](file:///d:/Programming/musicDownloader/music_ingestion_bot_specification.md)
- [`AGENTS.md`](file:///d:/Programming/musicDownloader/AGENTS.md)

---

## Quick Start

### 1. Configuration (`.env` and/or `config.yaml`)
Copy `.env.example` to `.env` (or `config.example.yaml` to `config.yaml`) and fill in your Telegram API credentials (from [my.telegram.org](https://my.telegram.org) and `@BotFather`):
```bash
cp .env.example .env
cp config.example.yaml config.yaml
```
Settings are merged in order: `defaults < config.yaml < .env < environment variables`.

### 2. Running Tests Locally (`uv`)
All unit, architectural boundary, and SQLite WAL integration tests run locally via `uv`:
```bash
uv run pytest
```
Run linters and type checks:
```bash
uv run ruff check
uv run ty check
```

### 3. Hybrid Local Development with WSL Docker (`telegram-bot-api` in Docker + `uv` on Host)
Start the local Telegram Bot API server inside WSL Docker:
```bash
docker compose up -d telegram-bot-api
```
Then run the Python bot locally:
```bash
uv run python main.py
```
To run the live WSL Docker container smoke test:
```bash
RUN_WSL_DOCKER_TESTS=1 uv run pytest -m docker
```

### 4. Full Docker Compose Deployment (External Mounted Music Folder)
Set `HOST_MUSIC_DIR` in `.env` (e.g., `HOST_MUSIC_DIR=/mnt/nas/music` or `HOST_MUSIC_DIR=D:/Music`) so your external music folder is bind-mounted to `/mnt/music/library` inside the container (see [`docker-compose.example.yaml`](file:///d:/Programming/musicDownloader/docker-compose.example.yaml) and [`config.example.yaml`](file:///d:/Programming/musicDownloader/config.example.yaml)):
```bash
docker compose up -d --build
```
Run the test suite inside the container (with `fpcalc`, `7z`, and `ffmpeg` pre-installed):
```bash
docker compose --profile test run --rm test
```

