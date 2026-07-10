# tu-arte-mi-arte

Direct, by conversation, the AI-generated art shown on three Samsung Frame TVs (two 43" verticals + one 50" horizontal) in a house — over Telegram, no manual exporting or uploading.

Ask for a theme, get a coherent three-piece set generated with [Nano Banana 2](https://ai.google.dev) via the Gemini API, preview it composited over a real photo of the room, refine it conversationally, then confirm to deploy it straight to the TVs' Art Mode.

Known non-blocking defects are tracked in [`KNOWN_ISSUES.md`](KNOWN_ISSUES.md).

## How it works

- **Engine** (`src/engine/`) — plain, framework-independent Python: image generation, art direction, split-mode compositing, room preview compositing, TV discovery/deploy. Fully testable in isolation.
- **Agent** (`src/agents/tu_arte_mi_arte/`) — a Google ADK agent that wraps the engine functions as tools and decides which to call and how to phrase each panel's scene description. It never touches image bytes directly — only `image_id` strings cross the agent/session boundary.
- **Bot** (`src/bot/`) — a thin Telegram interface that maps chat messages to ADK `Runner` calls against the same agent, reusing every tool as-is.

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
```

**3. (Optional) Configure your TVs**

```bash
cp config/tvs.toml.example config/tvs.toml
```

Fill in each TV's MAC address (from `GET http://<tv-ip>:8001/api/v2/`) — the MAC is the stable key; IPs are resolved automatically via mDNS since DHCP can reassign them. Also calibrate `config/room.toml` against a photo of your own room if you want the preview composited realistically.

## Usage

**Dev/demo interface (ADK Web chat UI)** — the primary way to exercise the agent during development:

```bash
uv run adk web src/agents
```

**Telegram bot** (the real interface once TVs/tokens are configured):

```bash
uv run python -m bot.telegram_bot
```

See [`docs/DEPLOY.md`](docs/DEPLOY.md) for running the bot as an always-on systemd service on a Raspberry Pi.

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
```

## License

MIT
