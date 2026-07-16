"""Eval script (dev-only, NOT a pytest test): validates the per-day
independiente/split mode decision of the batch-gallery skill (see
src/agents/tu_arte_mi_arte/skills/galeria-por-lotes/SKILL.md — paso 4,
docs/dev_plan_phase_2.md 1.3, PRD §15.2 objetivo 2 / §15.8).

Checks a behavior that can't be verified by pytest (it's free-text LLM
judgment, not tool plumbing): once a sub-group's scenes are drafted, does
each day's independiente/split choice make sense given the scene it
describes (e.g. split for a continuous horizon/landscape, independiente
for a portrait or detail)? Per §15.8 this never validates an exact ratio
for a single run — the ~70/30 split is an aggregate orientation across a
whole batch, not a per-batch quota — only that each individual decision is
reasonable.

Hits the real API, costs money, and is non-deterministic — run manually
with `uv run python scripts/eval_batch_split_ratio.py` when validating
changes to the per-day prompt instructions in SKILL.md.
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "agents"))

from dotenv import load_dotenv  # noqa: E402
from google import genai  # noqa: E402
from google.adk.runners import Runner  # noqa: E402
from google.adk.sessions import InMemorySessionService  # noqa: E402
from google.genai import types  # noqa: E402
from tu_arte_mi_arte.agent import root_agent  # noqa: E402

load_dotenv()

EVALS_DIR = Path(__file__).resolve().parent.parent / "data" / "evals"
LOAD_SKILL_TOOL_NAME = "load_skill"
APPROVAL_MESSAGE = "sí, así está bien"

REFERENCE_CASES = [
    {"theme": "Primavera", "day_count": 7},
    {"theme": "Día de los Muertos", "day_count": 6},
    {"theme": "playas de la Riviera Maya", "day_count": 8},
]

SPLIT_RATIO_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    properties={
        "day_decisions": types.Schema(
            type=types.Type.ARRAY,
            description=(
                "una entrada por cada día mencionado en el texto, en el "
                "orden en que aparecen"
            ),
            items=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "day_label": types.Schema(
                        type=types.Type.STRING,
                        description=(
                            "identificador del día tal como aparece en el texto"
                        ),
                    ),
                    "mode": types.Schema(
                        type=types.Type.STRING,
                        description=(
                            "'independiente' o 'split', según lo que el "
                            "texto indique"
                        ),
                    ),
                    "decision_is_reasonable": types.Schema(
                        type=types.Type.BOOLEAN,
                        description=(
                            "true si el modo elegido tiene sentido dado el "
                            "contenido descrito de la escena (p. ej. split "
                            "para un horizonte/paisaje continuo, "
                            "independiente para un retrato o detalle)"
                        ),
                    ),
                },
                required=["day_label", "mode", "decision_is_reasonable"],
            ),
        ),
        "notes": types.Schema(
            type=types.Type.STRING,
            description="Justificación breve, en español, de los puntajes",
        ),
    },
    required=["day_decisions", "notes"],
)

SPLIT_RATIO_JUDGE_PROMPT = (
    "Un asistente de arte generativo redactó las escenas de un sub-grupo "
    "de días para una galería temática de '{theme}'. Por cada día decidió "
    "si generar en modo independiente (3 escenas: 43L, 43R, 50, cada panel "
    "una composición autónoma) o modo split (2 escenas: wide, 50, donde "
    "las dos verticales comparten una sola composición continua). Evalúa, "
    "por cada día que aparezca en el texto, si la elección de modo tiene "
    "sentido dado el contenido descrito de la escena de ese día — nunca "
    "evalúes si la proporción global es 'correcta', solo si cada decisión "
    "individual es razonable:\n\n{text}"
)


async def _send(runner: Runner, user_id: str, session_id: str, text: str) -> list:
    events = []
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session_id,
        new_message=types.Content(role="user", parts=[types.Part(text=text)]),
    ):
        events.append(event)
    return events


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


def judge_split_ratio(theme: str, text: str) -> dict:
    client = genai.Client()
    response = client.models.generate_content(
        model="gemini-flash-latest",
        contents=SPLIT_RATIO_JUDGE_PROMPT.format(theme=theme, text=text),
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=SPLIT_RATIO_SCHEMA,
        ),
    )
    if not response.text:
        raise RuntimeError("El juez no devolvió una respuesta de texto.")
    return json.loads(response.text)


async def run_case(theme: str, day_count: int) -> dict:
    user_id = "eval"
    session_service = InMemorySessionService()
    session = await session_service.create_session(
        app_name="tu_arte_mi_arte", user_id=user_id
    )
    runner = Runner(
        app_name="tu_arte_mi_arte", agent=root_agent, session_service=session_service
    )

    request = f"quiero una galería de {theme} para {day_count} días"
    grouping_events = await _send(runner, user_id, session.id, request)
    approval_events = await _send(runner, user_id, session.id, APPROVAL_MESSAGE)

    return {
        "theme": theme,
        "day_count": day_count,
        "request": request,
        "called_load_skill": _called_load_skill(grouping_events),
        "grouping_text": _final_text(grouping_events),
        "prompts_text": _final_text(approval_events),
    }


def run_split_ratio_checks() -> list[dict]:
    print("== decisión día-a-día de modo independiente/split ==")
    runs = []
    for case in REFERENCE_CASES:
        theme, day_count = case["theme"], case["day_count"]
        print(f"-> {theme!r} ({day_count} días)")
        run = asyncio.run(run_case(theme, day_count))
        if not run["called_load_skill"]:
            run["passed"] = False
            run["judgment"] = None
            print("   FALLA: no llamó load_skill para un pedido de galería/lote")
        elif not run["prompts_text"]:
            run["passed"] = False
            run["judgment"] = None
            print("   FALLA: la respuesta de aprobación no redactó escenas")
        else:
            judgment = judge_split_ratio(theme, run["prompts_text"])
            run["judgment"] = judgment
            decisions = judgment["day_decisions"]
            run["passed"] = bool(decisions) and all(
                d["decision_is_reasonable"] for d in decisions
            )
            modes = [d["mode"] for d in decisions]
            print(
                f"   días_evaluados={len(decisions)} modos={modes} "
                f"pass={run['passed']}"
            )
        runs.append(run)
    return runs


def main() -> None:
    runs = run_split_ratio_checks()

    EVALS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = EVALS_DIR / "batch_split_ratio_eval.json"
    out_path.write_text(json.dumps(runs, indent=2, ensure_ascii=False))

    print("\n--- resumen ---")
    print(f"casos: {sum(r['passed'] for r in runs)}/{len(runs)}")
    print(f"Detalle guardado en {out_path}")


if __name__ == "__main__":
    main()
