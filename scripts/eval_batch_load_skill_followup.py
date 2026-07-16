"""Eval script (dev-only, NOT a pytest test): reproduces a real bug found
via a user-exported adk web conversation (weekend2.json).

Repro: a single, already-clear message ("quiero diseñar un lote nuevo
para el fin de semana") makes root_agent call list_skills -> load_skill
correctly, but the SAME turn then ends with an empty model text part
(finishReason=STOP, text=""). The user is left with no visible response
at all until they send a follow-up ("estas ahí?") a turn later. The
skill's own disambiguation logic (paso 2, resolución de referencias
relativas de tiempo) is never wrong when it actually runs — it just
doesn't run in the same turn as load_skill.

This is a deterministic check, not an LLM-judged one: after load_skill
fires, the turn's concatenated text must be non-empty. No judge model
needed to tell an empty string from a real one.

Hits the real API, costs money, and is non-deterministic (the empty-text
turn doesn't reproduce 100% of the time) — run manually with
`uv run python scripts/eval_batch_load_skill_followup.py` when validating
changes meant to fix this, and expect to run it a few times / bump
REPEAT_COUNT if a single run doesn't reproduce the failure.
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "agents"))

from dotenv import load_dotenv  # noqa: E402
from google.adk.runners import Runner  # noqa: E402
from google.adk.sessions import InMemorySessionService  # noqa: E402
from google.genai import types  # noqa: E402
from tu_arte_mi_arte.agent import root_agent  # noqa: E402

load_dotenv()

EVALS_DIR = Path(__file__).resolve().parent.parent / "data" / "evals"
LOAD_SKILL_TOOL_NAME = "load_skill"

REQUEST = "quiero diseñar un lote nuevo para el fin de semana"
REPEAT_COUNT = 15


def _called_load_skill(events: list) -> bool:
    for event in events:
        for call in event.get_function_calls():
            if call.name == LOAD_SKILL_TOOL_NAME:
                return True
    return False


def _final_text(events: list) -> str:
    texts = []
    for event in events:
        if event.content and event.content.parts:
            for part in event.content.parts:
                if part.text:
                    texts.append(part.text)
    return "\n".join(texts)


async def run_case() -> dict:
    user_id = "eval"
    session_service = InMemorySessionService()
    session = await session_service.create_session(
        app_name="tu_arte_mi_arte", user_id=user_id
    )
    runner = Runner(
        app_name="tu_arte_mi_arte", agent=root_agent, session_service=session_service
    )

    events = []
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session.id,
        new_message=types.Content(role="user", parts=[types.Part(text=REQUEST)]),
    ):
        events.append(event)

    text = _final_text(events)
    return {
        "called_load_skill": _called_load_skill(events),
        "final_text": text,
        "passed": bool(text.strip()),
    }


def main() -> None:
    print(f"== repro: {REQUEST!r} x{REPEAT_COUNT} corridas ==")
    runs = []
    for i in range(REPEAT_COUNT):
        run = asyncio.run(run_case())
        runs.append(run)
        status = "PASS" if run["passed"] else "FAIL (texto vacío)"
        print(
            f"-> corrida {i + 1}/{REPEAT_COUNT}: "
            f"called_load_skill={run['called_load_skill']} {status}"
        )

    EVALS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = EVALS_DIR / "batch_load_skill_followup_eval.json"
    out_path.write_text(json.dumps({"runs": runs}, indent=2, ensure_ascii=False))

    passed_count = sum(r["passed"] for r in runs)
    print("\n--- resumen ---")
    print(f"turnos con texto visible: {passed_count}/{REPEAT_COUNT}")
    print(f"Detalle guardado en {out_path}")


if __name__ == "__main__":
    main()
