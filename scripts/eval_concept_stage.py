"""Eval script (dev-only, NOT a pytest test): validates the concept-selection
conversational stage of root_agent's instruction (see agent.py — ETAPA 1).

Checks three behaviors that can't be verified by pytest (they're free-text
LLM judgment, not tool plumbing):
  - broad themes trigger a text-only concept pitch (multiple options, no
    tool call) instead of jumping straight to generation;
  - narrow/specific themes skip the pitch and call generate_set_* right away;
  - a follow-up turn picking a concept resolves into a generate_set_* call.

Hits the real API, costs money, and is non-deterministic — run manually with
`uv run python scripts/eval_concept_stage.py` when validating changes to the
concept-stage instruction in root_agent.
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
GENERATE_SET_TOOL_NAMES = ("generate_set_diptico", "generate_set_split")

BROAD_THEMES = ["Día de los Muertos", "otoño", "una fiesta de cumpleaños"]
NARROW_THEMES = [
    "bicicletas vintage estacionadas tipo Santorini",
    "faroles de papel picado colgando en un patio",
]
RESOLUTION_CASES = [
    {"theme": "Día de los Muertos", "follow_up": "vamos con la ofrenda"},
]

CONCEPT_PITCH_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    properties={
        "proposed_multiple_concepts": types.Schema(
            type=types.Type.BOOLEAN,
            description=(
                "true si el texto ofrece 2 o más direcciones de concepto " "distintas"
            ),
        ),
        "concept_count": types.Schema(
            type=types.Type.INTEGER,
            description="cuántas opciones de concepto distintas se ofrecieron",
        ),
    },
    required=["proposed_multiple_concepts", "concept_count"],
)

CONCEPT_PITCH_JUDGE_PROMPT = (
    "Un asistente de arte generativo recibió un tema amplio del usuario. En "
    "vez de generar imágenes de inmediato, respondió con este texto. "
    "Evalúa si el texto ofrece varias (2 o más) direcciones de concepto "
    "concretas y distintas entre sí para elegir, en vez de una sola idea o "
    "una pregunta genérica:\n\n{text}"
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


def _called_generate_set(events: list) -> bool:
    for event in events:
        for call in event.get_function_calls():
            if call.name in GENERATE_SET_TOOL_NAMES:
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


def judge_concept_pitch(text: str) -> dict:
    client = genai.Client()
    response = client.models.generate_content(
        model="gemini-flash-latest",
        contents=CONCEPT_PITCH_JUDGE_PROMPT.format(text=text),
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=CONCEPT_PITCH_SCHEMA,
        ),
    )
    if not response.text:
        raise RuntimeError("El juez no devolvió una respuesta de texto.")
    return json.loads(response.text)


async def run_case(theme: str, follow_up: str | None = None) -> dict:
    user_id = "eval"
    session_service = InMemorySessionService()
    session = await session_service.create_session(
        app_name="tu_arte_mi_arte", user_id=user_id
    )
    runner = Runner(
        app_name="tu_arte_mi_arte", agent=root_agent, session_service=session_service
    )

    first_turn_events = await _send(runner, user_id, session.id, theme)
    result = {
        "theme": theme,
        "first_turn_called_generate_set": _called_generate_set(first_turn_events),
        "first_turn_text": _final_text(first_turn_events),
    }

    if follow_up:
        follow_up_events = await _send(runner, user_id, session.id, follow_up)
        result["follow_up"] = follow_up
        result["follow_up_called_generate_set"] = _called_generate_set(follow_up_events)

    return result


def run_broad_theme_checks() -> list[dict]:
    print("== temas amplios: se espera pitch de conceptos, sin tool call ==")
    runs = []
    for theme in BROAD_THEMES:
        print(f"-> {theme!r}")
        run = asyncio.run(run_case(theme))
        if run["first_turn_called_generate_set"]:
            run["passed"] = False
            run["judgment"] = None
            print("   FALLA: llamó a generate_set_* sin pedir el concepto primero")
        else:
            judgment = judge_concept_pitch(run["first_turn_text"])
            run["judgment"] = judgment
            run["passed"] = judgment["proposed_multiple_concepts"]
            proposed = judgment["proposed_multiple_concepts"]
            print(
                f"   proposed_multiple_concepts={proposed} "
                f"concept_count={judgment['concept_count']} pass={run['passed']}"
            )
        runs.append(run)
    return runs


def run_narrow_theme_checks() -> list[dict]:
    print("\n== temas específicos: se espera tool call directo, sin pitch ==")
    runs = []
    for theme in NARROW_THEMES:
        print(f"-> {theme!r}")
        run = asyncio.run(run_case(theme))
        run["passed"] = run["first_turn_called_generate_set"]
        print(f"   called_generate_set={run['first_turn_called_generate_set']}")
        runs.append(run)
    return runs


def run_resolution_checks() -> list[dict]:
    print("\n== turno de resolución: elegir un concepto dispara el tool call ==")
    runs = []
    for case in RESOLUTION_CASES:
        print(f"-> {case['theme']!r} + {case['follow_up']!r}")
        run = asyncio.run(run_case(case["theme"], follow_up=case["follow_up"]))
        run["passed"] = run.get("follow_up_called_generate_set", False)
        print(f"   follow_up_called_generate_set={run['passed']}")
        runs.append(run)
    return runs


def main() -> None:
    broad_runs = run_broad_theme_checks()
    narrow_runs = run_narrow_theme_checks()
    resolution_runs = run_resolution_checks()

    EVALS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = EVALS_DIR / "concept_stage_eval.json"
    out_path.write_text(
        json.dumps(
            {
                "broad_themes": broad_runs,
                "narrow_themes": narrow_runs,
                "resolution_turns": resolution_runs,
            },
            indent=2,
            ensure_ascii=False,
        )
    )

    print("\n--- resumen ---")
    print(f"temas amplios: {sum(r['passed'] for r in broad_runs)}/{len(broad_runs)}")
    print(
        f"temas específicos: {sum(r['passed'] for r in narrow_runs)}/{len(narrow_runs)}"
    )
    print(
        f"turnos de resolución: "
        f"{sum(r['passed'] for r in resolution_runs)}/{len(resolution_runs)}"
    )
    print(f"Detalle guardado en {out_path}")


if __name__ == "__main__":
    main()
