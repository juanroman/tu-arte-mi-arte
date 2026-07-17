"""Eval script (dev-only, NOT a pytest test): validates the "Consulta de
estado del lote" instruction added to the galeria-por-lotes skill in
dev_plan_phase_2.md 2.6 (see SKILL.md — sección "Consulta de estado del
lote (bajo pedido)"), analogous to eval_partial_failure.py but at batch
scale (PRD §15.8, eval_batch_partial_report.py).

check_batch_status (agent.py) wraps engine.batch.summarize_batch as-is —
this script forces a mixed batch result deterministically (3 days
finalized, one day with a policy-rejected panel, one day with a
technically-exhausted panel) by monkeypatching agent.summarize_batch_ai
at module level, no real generation and no real SQLite batch needed.
Only the agent's own text-in/text-out turns hit the real API.

Checks three things a batch-scale status report must NOT do, since a
fresh instruction is free-text judgment, not tool plumbing:
  - fail to mention the days/panels that DID finalize successfully;
  - claim the whole batch failed when only 2 of 5 days have a problem;
  - collapse the policy-rejection panel and the technically-exhausted
    panel into one generic "something failed" statement.

Hits the real API, costs money, and is non-deterministic — run manually
with `uv run python scripts/eval_batch_partial_report.py` when validating
changes to the "Consulta de estado del lote" instruction in SKILL.md.
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
from tu_arte_mi_arte import agent  # noqa: E402
from tu_arte_mi_arte.agent import root_agent  # noqa: E402

load_dotenv()

EVALS_DIR = Path(__file__).resolve().parent.parent / "data" / "evals"

FAKE_BATCH_ID = "batch_eval_partial_report"

# 5 días: 3 finalizados por completo, uno con un panel rechazado por
# política, uno con un panel que agotó sus reintentos técnicos.
FAKE_SUMMARY = {
    "batch_id": FAKE_BATCH_ID,
    "theme": "faroles de papel picado por un valle otoñal",
    "day_count": 5,
    "stage_counts": {"finalized": 13, "needs_attention": 2},
    "needs_attention_policy_rejection": [
        {
            "day_index": 2,
            "panel": "43R",
            "error": "El modelo rechazó la solicitud (política o derechos).",
        }
    ],
    "needs_attention_technical": [
        {
            "day_index": 4,
            "panel": "50",
            "error": "Fallo transitorio de red tras agotar los reintentos.",
            "attempts": 2,
        }
    ],
    "days": [
        {
            "day_index": 1,
            "mode": "independiente",
            "sub_group": "Calles y faroles",
            "panels": {
                "43L": {"stage": "finalized", "image_id": "img_d1_43l", "error": None},
                "43R": {"stage": "finalized", "image_id": "img_d1_43r", "error": None},
                "50": {"stage": "finalized", "image_id": "img_d1_50", "error": None},
            },
        },
        {
            "day_index": 2,
            "mode": "independiente",
            "sub_group": "Calles y faroles",
            "panels": {
                "43L": {"stage": "finalized", "image_id": "img_d2_43l", "error": None},
                "43R": {
                    "stage": "needs_attention",
                    "image_id": None,
                    "error": "El modelo rechazó la solicitud (política o derechos).",
                },
                "50": {"stage": "finalized", "image_id": "img_d2_50", "error": None},
            },
        },
        {
            "day_index": 3,
            "mode": "split",
            "sub_group": "Plazas y mercados",
            "panels": {
                "43L": {"stage": "finalized", "image_id": "img_d3_43l", "error": None},
                "43R": {"stage": "finalized", "image_id": "img_d3_43r", "error": None},
                "50": {"stage": "finalized", "image_id": "img_d3_50", "error": None},
            },
        },
        {
            "day_index": 4,
            "mode": "independiente",
            "sub_group": "Plazas y mercados",
            "panels": {
                "43L": {"stage": "finalized", "image_id": "img_d4_43l", "error": None},
                "43R": {"stage": "finalized", "image_id": "img_d4_43r", "error": None},
                "50": {
                    "stage": "needs_attention",
                    "image_id": None,
                    "error": "Fallo transitorio de red tras agotar los reintentos.",
                },
            },
        },
        {
            "day_index": 5,
            "mode": "independiente",
            "sub_group": "Noche de altares",
            "panels": {
                "43L": {"stage": "finalized", "image_id": "img_d5_43l", "error": None},
                "43R": {"stage": "finalized", "image_id": "img_d5_43r", "error": None},
                "50": {"stage": "finalized", "image_id": "img_d5_50", "error": None},
            },
        },
    ],
}

# El primer turno menciona "varios días"/lote para disparar load_skill de
# forma confiable (mismo vocabulario ya validado en 1.1-1.5), el segundo
# pregunta explícitamente por el estado -- dos turnos porque
# check_batch_status vive gated tras la activación de la skill.
SETUP_MESSAGE = (
    "quiero saber cómo va el lote de varios días que dejé corriendo, "
    f"batch_id={FAKE_BATCH_ID}"
)
STATUS_QUESTION = f"¿cómo va el lote {FAKE_BATCH_ID}? ¿qué se logró y qué no?"

LOAD_SKILL_TOOL_NAME = "load_skill"
CHECK_STATUS_TOOL_NAME = "check_batch_status"

JUDGE_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    properties={
        "mentions_all_successful_days": types.Schema(
            type=types.Type.BOOLEAN,
            description=(
                "true si el texto menciona con precisión que los días "
                "1, 3 y 5 se lograron por completo (13 paneles "
                "finalizados en total)"
            ),
        ),
        "claims_total_batch_failure": types.Schema(
            type=types.Type.BOOLEAN,
            description=(
                "true si el texto da a entender que el lote completo "
                "falló o tuvo problemas graves, en vez de precisar que "
                "solo 2 de 5 días tienen un panel pendiente"
            ),
        ),
        "distinguishes_policy_vs_technical_failure": types.Schema(
            type=types.Type.BOOLEAN,
            description=(
                "true si el texto trata el panel rechazado por política "
                "(día 2, 43R) y el panel con falla técnica agotada "
                "(día 4, 50) como cosas distintas -- nunca los mezcla en "
                "una sola frase genérica de 'algo falló'"
            ),
        ),
        "notes": types.Schema(
            type=types.Type.STRING,
            description="Justificación breve, en español, de los puntajes",
        ),
    },
    required=[
        "mentions_all_successful_days",
        "claims_total_batch_failure",
        "distinguishes_policy_vs_technical_failure",
        "notes",
    ],
)

JUDGE_PROMPT = (
    "Un asistente de arte generativo para las TVs de una casa recibió la "
    "pregunta de un usuario sobre el estado de un lote de 5 días. El "
    "estado real es: días 1, 3 y 5 completamente finalizados (13 "
    "paneles); día 2 tiene el panel 43R rechazado por política de "
    "contenido/derechos; día 4 tiene el panel 50 con una falla técnica "
    "que agotó sus reintentos (2 intentos). Evalúa la respuesta que el "
    "asistente le dio al usuario:\n\n{text}"
)


def judge_status_report(text: str) -> dict:
    client = genai.Client()
    response = client.models.generate_content(
        model="gemini-flash-latest",
        contents=JUDGE_PROMPT.format(text=text),
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=JUDGE_SCHEMA,
        ),
    )
    if not response.text:
        raise RuntimeError("El juez no devolvió una respuesta de texto.")
    return json.loads(response.text)


def _called_tool(events: list, tool_name: str) -> bool:
    for event in events:
        for call in event.get_function_calls():
            if call.name == tool_name:
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


async def _send(runner: Runner, user_id: str, session_id: str, text: str) -> list:
    events = []
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session_id,
        new_message=types.Content(role="user", parts=[types.Part(text=text)]),
    ):
        events.append(event)
    return events


async def run_case() -> dict:
    user_id = "eval"
    session_service = InMemorySessionService()
    session = await session_service.create_session(
        app_name="tu_arte_mi_arte", user_id=user_id
    )
    runner = Runner(
        app_name="tu_arte_mi_arte", agent=root_agent, session_service=session_service
    )

    setup_events = await _send(runner, user_id, session.id, SETUP_MESSAGE)
    status_events = await _send(runner, user_id, session.id, STATUS_QUESTION)

    return {
        "called_load_skill": _called_tool(
            setup_events + status_events, LOAD_SKILL_TOOL_NAME
        ),
        "called_check_batch_status": _called_tool(
            setup_events + status_events, CHECK_STATUS_TOOL_NAME
        ),
        "final_text": _final_text(status_events),
    }


def main() -> None:
    agent.summarize_batch_ai = lambda batch_id: FAKE_SUMMARY

    print(f"== consulta de estado: batch_id={FAKE_BATCH_ID!r} ==")
    print(f"-> {SETUP_MESSAGE!r}")
    print(f"-> {STATUS_QUESTION!r}")
    result = asyncio.run(run_case())

    if not result["called_load_skill"]:
        print(
            "AVISO: load_skill no se llamó en ningún turno (riesgo ya "
            "documentado de variancia de activación, ver 1.1-1.5)."
        )
    if not result["called_check_batch_status"]:
        print(
            "FALLA: check_batch_status nunca se llamó -- no hay texto "
            "de reporte real que evaluar."
        )
        judgment = None
        passed = False
    else:
        text = result["final_text"]
        print(f"\n{text}\n")
        judgment = judge_status_report(text)
        passed = (
            judgment["mentions_all_successful_days"]
            and not judgment["claims_total_batch_failure"]
            and judgment["distinguishes_policy_vs_technical_failure"]
        )

    EVALS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = EVALS_DIR / "batch_partial_report_eval.json"
    out_path.write_text(
        json.dumps(
            {"result": result, "judgment": judgment, "passed": passed},
            indent=2,
            ensure_ascii=False,
        )
    )

    print("\n--- resumen ---")
    print(f"called_load_skill: {result['called_load_skill']}")
    print(f"called_check_batch_status: {result['called_check_batch_status']}")
    if judgment is not None:
        print(
            f"mentions_all_successful_days: "
            f"{judgment['mentions_all_successful_days']}"
        )
        print(f"claims_total_batch_failure: {judgment['claims_total_batch_failure']}")
        print(
            f"distinguishes_policy_vs_technical_failure: "
            f"{judgment['distinguishes_policy_vs_technical_failure']}"
        )
        print(f"notas: {judgment['notes']}")
    print(f"PASSED: {passed}")
    print(f"Detalle guardado en {out_path}")


if __name__ == "__main__":
    main()
