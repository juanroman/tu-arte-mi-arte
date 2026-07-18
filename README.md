# tu-arte-mi-arte

[![CI](https://github.com/juanroman/tu-arte-mi-arte/actions/workflows/ci.yml/badge.svg)](https://github.com/juanroman/tu-arte-mi-arte/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Direct, by conversation, the AI-generated art shown on three Samsung Frame TVs (two 43" verticals + one 50" horizontal) in a house — over Telegram, no manual exporting or uploading.

Ask for a theme, get a coherent three-piece set generated with [Nano Banana 2](https://ai.google.dev) via the Gemini API, preview it composited over a real photo of the room, refine it conversationally, then confirm to deploy it straight to the TVs' Art Mode.

Ask instead for a themed gallery spanning several days ("something different every day this week"), and it proposes a sub-theme structure, drafts and previews day by day, estimates how long the full batch will take, then — once you confirm — generates, uploads, and configures native TV rotation for the whole batch in the background, without blocking the chat. It reports back on Telegram when done (or partially done — a 10-day batch that delivers 9 is a success, not a failure), and survives a process restart mid-batch without losing or duplicating work.

Known non-blocking defects are tracked in [`KNOWN_ISSUES.md`](KNOWN_ISSUES.md).

## How it works

- **Engine** (`src/engine/`) — plain, framework-independent Python: image generation, art direction, split-mode compositing, room preview compositing, TV discovery/deploy, and the batch gallery engine (`batch.py`/`batch_store.py`). Fully testable in isolation.
- **Agent** (`src/agents/tu_arte_mi_arte/`) — a Google ADK agent that wraps the engine functions as tools and decides which to call and how to phrase each panel's scene description. It never touches image bytes directly — only `image_id` strings cross the agent/session boundary.
- **Batch gallery skill** (`src/agents/tu_arte_mi_arte/skills/galeria-por-lotes/`) — an ADK Skill loaded onto the same `root_agent`, activated only when the user's request implies more than one day/occasion. It owns the multi-day conversational flow (grouping proposal → approval → per-day prompts → preview → time estimate → confirmation) and drives the batch engine; the single-piece flow and `root_agent`'s default behavior are unaffected when it's not active.
- **Bot** (`src/bot/`) — a thin Telegram interface that maps chat messages to ADK `Runner` calls against the same agent, reusing every tool as-is. Confirming a batch runs the engine (draft → 4K finalize → TV upload → rotation setup) in the background and reports proactively on completion; on restart, it detects and resumes any batch left mid-flight.

Coherence across the three pieces doesn't come from chaining one generated image into the next — the agent authors a distinct, specific scene description per panel under a shared theme, and each panel is generated independently. Shared style comes only from `config/art_direction.toml`.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- A Gemini API key with access to `gemini-3.1-flash-image`
- A Telegram bot token ([BotFather](https://t.me/BotFather)) — only needed for the Telegram interface, not for the `adk web` dev UI
- Samsung Frame TVs on the same LAN — only needed for automatic deploy to TVs, not for generation/preview

## Setup

**1. Install dependencies**

```bash
uv sync
```

**2. Configure secrets**

```bash
cp .env.example .env
```

```bash
GEMINI_API_KEY=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_USER_IDS=...          # comma-separated Telegram user IDs allowed to use the bot
SESSION_INACTIVITY_TIMEOUT_SECONDS=... # optional; how long an idle conversation stays alive
LOG_LEVEL=...                          # optional; DEBUG/INFO/WARNING/ERROR, defaults to INFO
```

**3. (Optional) Configure your TVs**

```bash
cp config/tvs.toml.example config/tvs.toml
```

Fill in each TV's MAC address (from `GET http://<tv-ip>:8001/api/v2/`) — the MAC is the stable key; IPs are resolved automatically via mDNS since DHCP can reassign them. Also calibrate `config/room.toml` against a photo of your own room if you want the preview composited realistically.

Other config files (all editable TOML, none require setup for a basic run): `config/art_direction.toml` (house style applied to every scene), `config/split.toml` (frame-gap compensation for split-mode panels), `config/house.toml` (house timezone, used to resolve relative time references like "this weekend"), `config/batch.toml` (retry ceilings, time-estimate constants, and native TV rotation settings for the batch gallery engine).

## Usage

**Dev/demo interface (ADK Web chat UI)** — the primary way to exercise the agent during development:

```bash
uv run adk web src/agents
```

**Telegram bot** (the real interface once TVs/tokens are configured):

```bash
uv run python -m bot.telegram_bot
```

See [`docs/DEPLOY.md`](docs/DEPLOY.md) for running the bot as an always-on systemd service on a Raspberry Pi, including how to raise `LOG_LEVEL` to `DEBUG` for troubleshooting via `journalctl`.

## Running tests

```bash
uv run pytest
uv run pytest tests/test_engine_generation.py -v          # single file
uv run pytest tests/test_engine_generation.py::test_name  # single test
```

Tests that need a live `GEMINI_API_KEY` are marked `requires_gemini_key` and skip automatically if it's not set.

## Code quality

```bash
uv run ruff check .       # linting
uv run black --check .    # formatting
uv run mypy src            # type checking
uv run pip-audit           # dependency vulnerability scan
```

All four must be clean before considering a change done.

## Manual eval scripts

These hit the real Gemini API (cost money, non-deterministic) and are not part of the `pytest` suite:

```bash
uv run python scripts/eval_coherence.py
uv run python scripts/eval_concept_stage.py
uv run python scripts/eval_framing.py
uv run python scripts/eval_partial_failure.py

# Batch gallery evalsets (scripts/eval_batch_*.py)
uv run python scripts/eval_batch_grouping.py
uv run python scripts/eval_batch_split_ratio.py
uv run python scripts/eval_batch_day_diversity.py
uv run python scripts/eval_batch_partial_report.py
uv run python scripts/eval_batch_load_skill_followup.py
```

`scripts/demo_batch_*.py` are one-off, narrated demo scripts used while building the batch gallery engine (some hit real TVs) — not evals, not part of regular workflows.

## License

MIT
