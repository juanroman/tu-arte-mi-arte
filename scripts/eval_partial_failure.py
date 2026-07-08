"""Eval script (dev-only, NOT a pytest test): validates the "RESULTADOS
MIXTOS" instruction added to root_agent in Etapa 2 iteración 2.5 (see
agent.py — MANEJO DE ERRORES / RESULTADOS MIXTOS).

generate_set_diptico/generate_set_split generate each panel independently
and stop the chain at the first error, returning a dict with whichever
panels already succeeded (with an image_id) plus the one that failed. This
script forces that exact mixed shape deterministically (43L succeeds, 43R
is rejected, 50 is never attempted) by monkeypatching generate_image_ai at
module level — no real image generation, so it's cheap to iterate on the
instruction text. Only the agent's own text-in/text-out turn hits the real
API.

Checks three things a mixed result must NOT do, since a fresh instruction
change is free-text judgment, not tool plumbing:
  - discard/never mention the image_id of the panel that DID succeed;
  - claim the whole set was rejected when only one panel was;
  - offer a pivot/retry that regenerates the whole set instead of just the
    failed panel.

Hits the real API, costs money, and is non-deterministic — run manually
with `uv run python scripts/eval_partial_failure.py` when validating
changes to the RESULTADOS MIXTOS instruction.
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

THEME = "faroles de papel picado colgando en un patio mexicano de noche"
SUCCESS_IMAGE_ID = "img_eval_43l_ok"

JUDGE_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    properties={
        "mentions_successful_image_id": types.Schema(
            type=types.Type.BOOLEAN,
            description=(
                f"true si el texto menciona explícitamente el image_id "
                f"'{SUCCESS_IMAGE_ID}' del panel que sí se generó"
            ),
        ),
        "claims_total_rejection": types.Schema(
            type=types.Type.BOOLEAN,
            description=(
                "true si el texto da a entender que TODO el conjunto fue "
                "rechazado (en vez de precisar que solo un panel falló)"
            ),
        ),
        "offers_whole_set_regeneration": types.Schema(
            type=types.Type.BOOLEAN,
            description=(
                "true si el texto propone regenerar o rehacer el conjunto "
                "completo, en vez de reparar solo el panel fallido"
            ),
        ),
        "notes": types.Schema(
            type=types.Type.STRING,
            description="Justificación breve, en español, de los puntajes",
        ),
    },
    required=[
        "mentions_successful_image_id",
        "claims_total_rejection",
        "offers_whole_set_regeneration",
        "notes",
    ],
)

JUDGE_PROMPT = (
    "Un asistente de arte generativo para las TVs de una casa intentó "
    "generar un conjunto de 3 piezas (43L, 43R, 50). El panel 43L se "
    f"generó con éxito (image_id='{SUCCESS_IMAGE_ID}'); el panel 43R fue "
    "rechazado por políticas de derechos/contenido; el panel 50 nunca se "
    "intentó porque la cadena se detuvo en el primer error. Evalúa la "
    "respuesta que el asistente le dio al usuario:\n\n{text}"
)


def _fake_generate_image_ai(prompt: str, aspect_ratio: str) -> dict:
    """Sequenced fake: 1st call succeeds (43L), 2nd call is a policy
    rejection (43R). 50 is never reached because generate_set_diptico
    stops the chain on the 43R error — this mirrors its real documented
    behavior, not a simplification for the eval.
    """
    _fake_generate_image_ai.calls += 1  # type: ignore[attr-defined]
    if _fake_generate_image_ai.calls == 1:  # type: ignore[attr-defined]
        return {"image_id": SUCCESS_IMAGE_ID, "path": "fake.jpg"}
    return {
        "error": "El modelo rechazó la solicitud (política o derechos).",
        "policy_rejection": True,
    }


_fake_generate_image_ai.calls = 0  # type: ignore[attr-defined]


def judge_mixed_result_reply(text: str) -> dict:
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


def _final_text(events: list) -> str:
    texts = []
    for event in events:
        if event.content and event.content.parts:
            for part in event.content.parts:
                if part.text:
                    texts.append(part.text)
    return "\n".join(texts)


async def run_case() -> dict:
    _fake_generate_image_ai.calls = 0  # type: ignore[attr-defined]
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
        new_message=types.Content(role="user", parts=[types.Part(text=THEME)]),
    ):
        events.append(event)

    return {"final_text": _final_text(events)}


def main() -> None:
    agent.generate_image_ai = _fake_generate_image_ai

    print(f"== tema: {THEME!r} (43L OK, 43R rechazado, 50 nunca intentado) ==")
    result = asyncio.run(run_case())
    text = result["final_text"]
    print(text)

    judgment = judge_mixed_result_reply(text)
    passed = (
        judgment["mentions_successful_image_id"]
        and not judgment["claims_total_rejection"]
        and not judgment["offers_whole_set_regeneration"]
    )

    EVALS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = EVALS_DIR / "partial_failure_eval.json"
    out_path.write_text(
        json.dumps(
            {"final_text": text, "judgment": judgment, "passed": passed},
            indent=2,
            ensure_ascii=False,
        )
    )

    print("\n--- resumen ---")
    print(f"mentions_successful_image_id: {judgment['mentions_successful_image_id']}")
    print(f"claims_total_rejection: {judgment['claims_total_rejection']}")
    print(f"offers_whole_set_regeneration: {judgment['offers_whole_set_regeneration']}")
    print(f"notas: {judgment['notes']}")
    print(f"PASSED: {passed}")
    print(f"Detalle guardado en {out_path}")


if __name__ == "__main__":
    main()
