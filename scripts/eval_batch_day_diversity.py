"""Eval script (dev-only, NOT a pytest test): validates that the days
within one approved sub-group feel related without collapsing into
trivial variations of the same framing (see
src/agents/tu_arte_mi_arte/skills/galeria-por-lotes/SKILL.md — paso 4,
docs/dev_plan_phase_2.md 1.3, PRD §15.8).

Extends the criterion already used in eval_coherence.py (shared
palette/light/grain + archetype diversity within one set of panels) across
every day of a sub-group's *text* (scene descriptions), not generated
images — 1.3 doesn't generate any real image yet, that's 1.4+.

Hits the real API, costs money, and is non-deterministic — run manually
with `uv run python scripts/eval_batch_day_diversity.py` when validating
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

DAY_DIVERSITY_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    properties={
        "days_feel_thematically_related": types.Schema(
            type=types.Type.BOOLEAN,
            description=(
                "true si todos los días del sub-grupo se sienten parte "
                "del mismo ángulo/enfoque anunciado para ese sub-grupo, "
                "sin desviarse a otro tema"
            ),
        ),
        "avoids_trivial_repetition_across_days": types.Schema(
            type=types.Type.BOOLEAN,
            description=(
                "true si los días NO colapsan en variaciones triviales "
                "del mismo encuadre/composición (p. ej. mismo archetype "
                "con solo el sujeto cambiado, día tras día)"
            ),
        ),
        "panels_within_each_day_use_distinct_archetypes": types.Schema(
            type=types.Type.BOOLEAN,
            description=(
                "true si, dentro de cada día individual, los paneles "
                "descritos (43L/43R/50 o wide/50) usan tipos de plano "
                "claramente distintos entre sí"
            ),
        ),
        "notes": types.Schema(
            type=types.Type.STRING,
            description="Justificación breve, en español, de los puntajes",
        ),
    },
    required=[
        "days_feel_thematically_related",
        "avoids_trivial_repetition_across_days",
        "panels_within_each_day_use_distinct_archetypes",
        "notes",
    ],
)

DAY_DIVERSITY_JUDGE_PROMPT = (
    "Un asistente de arte generativo redactó, día por día, las escenas de "
    "un sub-grupo de una galería temática de '{theme}'. Evalúa el "
    "conjunto de días de este sub-grupo: ¿se sienten relacionados entre sí "
    "(mismo ángulo temático) sin colapsar en variaciones triviales del "
    "mismo encuadre día tras día? ¿Cada día, por separado, usa tipos de "
    "plano distintos entre sus propios paneles?\n\n{text}"
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


def judge_day_diversity(theme: str, text: str) -> dict:
    client = genai.Client()
    response = client.models.generate_content(
        model="gemini-flash-latest",
        contents=DAY_DIVERSITY_JUDGE_PROMPT.format(theme=theme, text=text),
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=DAY_DIVERSITY_SCHEMA,
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
        "prompts_text": _final_text(approval_events),
    }


def _all_bools_true(judgment: dict) -> bool:
    return (
        judgment["days_feel_thematically_related"]
        and judgment["avoids_trivial_repetition_across_days"]
        and judgment["panels_within_each_day_use_distinct_archetypes"]
    )


def run_day_diversity_checks() -> list[dict]:
    print("== diversidad y coherencia entre días de un mismo sub-grupo ==")
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
            judgment = judge_day_diversity(theme, run["prompts_text"])
            run["judgment"] = judgment
            run["passed"] = _all_bools_true(judgment)
            related = judgment["days_feel_thematically_related"]
            no_repeat = judgment["avoids_trivial_repetition_across_days"]
            distinct = judgment["panels_within_each_day_use_distinct_archetypes"]
            print(
                f"   thematically_related={related} "
                f"avoids_repetition={no_repeat} "
                f"distinct_archetypes={distinct} "
                f"pass={run['passed']}"
            )
        runs.append(run)
    return runs


def main() -> None:
    runs = run_day_diversity_checks()

    EVALS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = EVALS_DIR / "batch_day_diversity_eval.json"
    out_path.write_text(json.dumps(runs, indent=2, ensure_ascii=False))

    print("\n--- resumen ---")
    print(f"casos: {sum(r['passed'] for r in runs)}/{len(runs)}")
    print(f"Detalle guardado en {out_path}")


if __name__ == "__main__":
    main()
