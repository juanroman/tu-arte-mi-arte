"""Eval script (dev-only, NOT a pytest test): validates the grouping-proposal
step of the batch-gallery skill (see
src/agents/tu_arte_mi_arte/skills/galeria-por-lotes/SKILL.md — paso 2,
docs/dev_plan_phase_2.md 1.2).

Checks behaviors that can't be verified by pytest (they're free-text LLM
judgment, not tool plumbing):
  - a broad theme + day count triggers load_skill and produces a grouping
    proposal covering all days, in 2-4 sub-groups, without conceptual
    overlap, and staying within the requested theme;
  - a correction turn ("move X to the front") adjusts the proposal without
    the conversation restarting from scratch.

Hits the real API, costs money, and is non-deterministic — run manually with
`uv run python scripts/eval_batch_grouping.py` when validating changes to the
grouping-proposal instructions in SKILL.md.
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

REFERENCE_CASES = [
    {"theme": "Primavera", "day_count": 7},
    {"theme": "Día de los Muertos", "day_count": 6},
    {"theme": "playas de la Riviera Maya", "day_count": 8},
]
CORRECTION_FOLLOW_UP = "mueve el sub-grupo de hora dorada al principio"

GROUPING_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    properties={
        "covers_all_days_no_gaps_or_overlaps": types.Schema(
            type=types.Type.BOOLEAN,
            description=(
                "true si los sub-grupos propuestos, sumados, cubren "
                "exactamente el número de días pedido, sin traslapes ni "
                "huecos entre ellos"
            ),
        ),
        "sub_group_count": types.Schema(
            type=types.Type.INTEGER,
            description="cuántos sub-grupos distintos se propusieron",
        ),
        "sub_groups_have_distinct_angles": types.Schema(
            type=types.Type.BOOLEAN,
            description=(
                "true si cada sub-grupo tiene un ángulo/enfoque visual "
                "distinto, sin traslape conceptual con los demás"
            ),
        ),
        "stays_within_requested_theme": types.Schema(
            type=types.Type.BOOLEAN,
            description=(
                "true si todos los sub-grupos se mantienen dentro del tema "
                "general pedido, sin desviarse a temas no relacionados"
            ),
        ),
    },
    required=[
        "covers_all_days_no_gaps_or_overlaps",
        "sub_group_count",
        "sub_groups_have_distinct_angles",
        "stays_within_requested_theme",
    ],
)

GROUPING_JUDGE_PROMPT = (
    "Un asistente de arte generativo recibió el pedido de una galería "
    "temática de {day_count} días bajo el tema '{theme}'. En vez de "
    "generar imágenes de inmediato, respondió con una propuesta de "
    "estructura de sub-grupos. Evalúa la propuesta:\n\n{text}"
)

CORRECTION_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    properties={
        "applied_the_requested_change": types.Schema(
            type=types.Type.BOOLEAN,
            description=(
                "true si la respuesta refleja el ajuste pedido por el "
                "usuario aplicado sobre la propuesta anterior"
            ),
        ),
        "presented_full_updated_structure": types.Schema(
            type=types.Type.BOOLEAN,
            description=(
                "true si la respuesta vuelve a presentar la estructura "
                "completa actualizada (no solo confirma el cambio en "
                "abstracto)"
            ),
        ),
    },
    required=["applied_the_requested_change", "presented_full_updated_structure"],
)

CORRECTION_JUDGE_PROMPT = (
    "Un asistente de arte generativo había propuesto esta estructura de "
    "sub-grupos:\n\n{original}\n\nEl usuario pidió este ajuste: "
    "'{follow_up}'. El asistente respondió:\n\n{text}\n\nEvalúa la "
    "respuesta."
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


def judge_grouping(theme: str, day_count: int, text: str) -> dict:
    client = genai.Client()
    response = client.models.generate_content(
        model="gemini-flash-latest",
        contents=GROUPING_JUDGE_PROMPT.format(
            theme=theme, day_count=day_count, text=text
        ),
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=GROUPING_SCHEMA,
        ),
    )
    if not response.text:
        raise RuntimeError("El juez no devolvió una respuesta de texto.")
    return json.loads(response.text)


def judge_correction(original: str, follow_up: str, text: str) -> dict:
    client = genai.Client()
    response = client.models.generate_content(
        model="gemini-flash-latest",
        contents=CORRECTION_JUDGE_PROMPT.format(
            original=original, follow_up=follow_up, text=text
        ),
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=CORRECTION_SCHEMA,
        ),
    )
    if not response.text:
        raise RuntimeError("El juez no devolvió una respuesta de texto.")
    return json.loads(response.text)


async def run_case(theme: str, day_count: int, with_correction: bool) -> dict:
    user_id = "eval"
    session_service = InMemorySessionService()
    session = await session_service.create_session(
        app_name="tu_arte_mi_arte", user_id=user_id
    )
    runner = Runner(
        app_name="tu_arte_mi_arte", agent=root_agent, session_service=session_service
    )

    request = f"quiero una galería de {theme} para {day_count} días"
    first_turn_events = await _send(runner, user_id, session.id, request)
    result = {
        "theme": theme,
        "day_count": day_count,
        "request": request,
        "called_load_skill": _called_load_skill(first_turn_events),
        "proposal_text": _final_text(first_turn_events),
    }

    if with_correction:
        correction_events = await _send(
            runner, user_id, session.id, CORRECTION_FOLLOW_UP
        )
        result["follow_up"] = CORRECTION_FOLLOW_UP
        result["correction_text"] = _final_text(correction_events)

    return result


def run_grouping_checks() -> list[dict]:
    print("== propuesta de agrupación: cobertura, ángulos distintos, tema ==")
    runs = []
    for case in REFERENCE_CASES:
        theme, day_count = case["theme"], case["day_count"]
        print(f"-> {theme!r} ({day_count} días)")
        run = asyncio.run(run_case(theme, day_count, with_correction=False))
        if not run["called_load_skill"]:
            run["passed"] = False
            run["judgment"] = None
            print("   FALLA: no llamó load_skill para un pedido de galería/lote")
        else:
            judgment = judge_grouping(theme, day_count, run["proposal_text"])
            run["judgment"] = judgment
            run["passed"] = _all_bools_true(judgment)
            print(
                f"   sub_group_count={judgment['sub_group_count']} "
                f"covers_all_days={judgment['covers_all_days_no_gaps_or_overlaps']} "
                f"distinct_angles={judgment['sub_groups_have_distinct_angles']} "
                f"on_theme={judgment['stays_within_requested_theme']} "
                f"pass={run['passed']}"
            )
        runs.append(run)
    return runs


def _all_bools_true(judgment: dict) -> bool:
    return (
        judgment["covers_all_days_no_gaps_or_overlaps"]
        and judgment["sub_groups_have_distinct_angles"]
        and judgment["stays_within_requested_theme"]
    )


def run_correction_check() -> dict:
    print("\n== turno de corrección: ajusta sin reiniciar la conversación ==")
    case = REFERENCE_CASES[0]
    theme, day_count = case["theme"], case["day_count"]
    print(f"-> {theme!r} ({day_count} días) + {CORRECTION_FOLLOW_UP!r}")
    run = asyncio.run(run_case(theme, day_count, with_correction=True))
    judgment = judge_correction(
        run["proposal_text"], CORRECTION_FOLLOW_UP, run["correction_text"]
    )
    run["judgment"] = judgment
    run["passed"] = (
        judgment["applied_the_requested_change"]
        and judgment["presented_full_updated_structure"]
    )
    print(
        f"   applied_change={judgment['applied_the_requested_change']} "
        f"presented_full_structure={judgment['presented_full_updated_structure']} "
        f"pass={run['passed']}"
    )
    return run


def main() -> None:
    grouping_runs = run_grouping_checks()
    correction_run = run_correction_check()

    EVALS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = EVALS_DIR / "batch_grouping_eval.json"
    out_path.write_text(
        json.dumps(
            {
                "grouping_proposals": grouping_runs,
                "correction_turn": correction_run,
            },
            indent=2,
            ensure_ascii=False,
        )
    )

    print("\n--- resumen ---")
    print(
        f"propuestas de agrupación: "
        f"{sum(r['passed'] for r in grouping_runs)}/{len(grouping_runs)}"
    )
    print(f"turno de corrección: {'PASS' if correction_run['passed'] else 'FAIL'}")
    print(f"Detalle guardado en {out_path}")


if __name__ == "__main__":
    main()
