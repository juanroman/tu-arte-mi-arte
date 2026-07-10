# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A system that lets you direct, by conversation, the AI-generated art shown on three Samsung Frame TVs (two 43" verticals + one 50" horizontal) in a house. Full spec lives in `docs/prd.md`; the iteration-by-iteration build plan (what's done, what's next, and why) lives in `docs/dev_plan.md` — **read `docs/dev_plan.md` before starting new work** to see current status and avoid re-doing decisions already made. Both are local-only (gitignored, not part of the public repo) — they exist on disk here but won't be present in a fresh clone. `KNOWN_ISSUES.md` tracks real defects found in manual testing that aren't blocking but need a product decision later.

**This is a personal project, not FINDEP enterprise work.** If your global/user-level CLAUDE.md carries FINDEP enterprise rules (mandatory planning workflow, `feature/WI-[ID]` branches, Work Item commit references, PR-only delivery, Azure DevOps integration, etc.), none of that applies here — this repo's conventions override those for any work done in this directory. Commit directly to `main`, no feature-branch/PR workflow, no Work Item IDs in commit messages, no Azure DevOps.

## Commands

```bash
# Run the dev/demo interface (ADK Web chat UI) — this is the primary way to exercise the agent
uv run adk web src/agents

# Run the Telegram bot (Etapa 2+)
uv run python -m bot.telegram_bot

# Tests
uv run pytest
uv run pytest tests/test_engine_generation.py -v   # single file
uv run pytest tests/test_engine_generation.py::test_name  # single test

# Static analysis — all four must be clean before considering work done
uv run ruff check .
uv run black --check .
uv run mypy src
uv run pip-audit

# Manual eval scripts (hit the real Gemini API, cost money, non-deterministic — not part of pytest)
uv run python scripts/eval_coherence.py
uv run python scripts/eval_concept_stage.py

# Dependency management
uv add <package>
uv sync
```

Tests that need a live `GEMINI_API_KEY` are marked with `requires_gemini_key` and skip automatically if it's not set.

## Architecture

**Layer separation is the load-bearing design decision.** `src/engine/` contains plain, ADK-independent Python functions (image generation, art direction, split-mode compositing, preview compositing) — testable in isolation, with no framework coupling. `src/agents/tu_arte_mi_arte/agent.py` wraps those functions as ADK **tools** on `root_agent`; the agent's job is only to decide *which* tool to call and *how to phrase* the scene description for each panel, never to do the image work itself. `src/bot/` (added in Etapa 2) is a thin interface layer — it will map Telegram messages to ADK `Runner` calls against the same `root_agent`, reusing every tool as-is.

When adding new generation/composition logic, put it in `src/engine/` as a plain function first, then expose it as a tool in `agent.py` if the agent needs to call it conversationally.

**Byte boundary (PRD §7.11):** image bytes never enter the ADK session/conversation history — only `image_id` strings and metadata do. Every engine function that produces an image returns `{"image_id": ..., "path": ...}` (or `{"error": ...}` on failure) and persists bytes to `data/images/` under that ID. The agent reasons about IDs, never pixels.

**Per-panel authorship, not image-to-image chaining.** Coherence across the three pieces (43L/43R/50) does *not* come from conditioning one generated image on another (`docs/dev_plan.md` §1.6 documents why that approach was abandoned — Nano Banana 2 either produced collages or near-clones when asked to "continue" a reference). Instead, `root_agent` itself authors a distinct, specific scene description per panel (using a menu of composition archetypes to force variety), and each panel is generated **independently**. Shared style comes only from `config/art_direction.toml` applied uniformly via `build_prompt()`. Don't reintroduce reference-image chaining between panels without re-reading that section.

**Error handling ladder (PRD §7.9, `src/engine/generation.py::_call_model`/`_save_response_image`):** transient failures (429/5xx) retry silently via the SDK's native `HttpRetryOptions`; real policy/rights rejections are detected from `finish_reason`/`block_reason` and marked with `policy_rejection: True` in the returned error dict. `root_agent`'s instructions branch on that flag: policy rejections get a pivot offer (never a silent rewrite-and-retry), generic errors get a plain-language failure message with a retry offer. Any new failure path should preserve this distinction rather than collapsing all errors into one shape.

**Config-as-data, not code.** House art direction (`config/art_direction.toml`), the split-mode frame-gap compensation (`config/split.toml`), and the room-photo panel calibration (`config/room.toml`) are all editable TOML loaded by dataclass-returning `load_*` functions in the corresponding `src/engine/*.py` module. Prefer adding a config field over hardcoding a constant when the value is installation-specific (measured once, physical, or house-preference).

**Two-pass resolution (PRD §7.7):** everything generates at 1K by default (cheap iteration). `generate_final_high_res`/`finalize_high_res` re-run the approved draft through image-to-image at a fixed 4K with a strict "preserve layout/geometry" instruction — never a blind upscale, never a different model. Split-mode finalization re-splits the wide 4K source with the same gap compensation rather than upscaling an already-cropped half.

**Test import pattern:** since `pytest` doesn't pick up the editable install's site-packages path the same way `uv run adk web`/`uv run python -m ...` do, every test file manually prepends `src/` (and `src/agents/` where needed) to `sys.path` before importing — see the top of any `tests/test_*.py` file for the exact pattern to copy.
