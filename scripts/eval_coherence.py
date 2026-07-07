"""Eval script (dev-only, NOT a pytest test): drives the real root_agent
(text turn in, tool call out) across a list of themes and asks Gemini
(vision) to judge set coherence per PRD §7.4.

Hits the real API, costs money, and is non-deterministic — run manually with
`uv run python scripts/eval_coherence.py` (optionally passing themes as CLI
args) when validating changes to how root_agent authors per-panel scenes in
generate_set_diptico/generate_set_split.
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

DEFAULT_THEMES = [
    "bicicletas vintage estacionadas tipo Santorini",
    "un puesto de flores de lavanda en un mercado de la Provenza al amanecer",
    "veleros anclados en una bahía de la costa amalfitana",
    "una cabaña de troncos con luces cálidas rodeada de pinos nevados",
    "las mesas de chapa de un café notable porteño con hojas de otoño en la vereda",
]

JUDGE_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    properties={
        "palette_lighting_coherence": types.Schema(
            type=types.Type.INTEGER,
            description="1-5: ¿comparten las tres piezas paleta, iluminación y grano?",
        ),
        "world_story_coherence": types.Schema(
            type=types.Type.INTEGER,
            description="1-5: ¿se sienten el mismo mundo/tema, no escenas inconexas?",
        ),
        "archetype_diversity": types.Schema(
            type=types.Type.INTEGER,
            description=(
                "1-5: ¿43L, 43R y 50 usan tipos de plano claramente distintos "
                "entre sí (p. ej. macro vs. plano abierto vs. figura), en vez "
                "de repetir la misma composición con el sujeto cambiado?"
            ),
        ),
        "overall_pass": types.Schema(
            type=types.Type.BOOLEAN,
            description="true si el conjunto se lee como una sala curada intencional",
        ),
        "notes": types.Schema(
            type=types.Type.STRING,
            description="Justificación breve, en español, de los puntajes",
        ),
    },
    required=[
        "palette_lighting_coherence",
        "world_story_coherence",
        "archetype_diversity",
        "overall_pass",
        "notes",
    ],
)

JUDGE_PROMPT = (
    "Eres un juez de dirección de arte. Te muestro tres imágenes generadas para "
    "las pantallas de una casa a partir del mismo tema: primero el panel 43L "
    "(vertical), luego 43R (vertical) y finalmente el panorama de la pantalla 50 "
    "(horizontal). Evalúa si se leen como UN CONJUNTO curado e intencional -no "
    "como tres imágenes sueltas e inconexas, ni como la misma toma repetida- "
    "según estos criterios del PRD de la casa: tratamiento visual compartido "
    "(paleta, luz, grano, tono), 43L/43R/50 usan tipos de plano/composición "
    "claramente distintos entre sí para no verse redundantes colgadas juntas, y "
    "las tres piezas pertenecen al mismo mundo/tema."
)


def _image_part(path: str) -> types.Part:
    return types.Part.from_bytes(data=Path(path).read_bytes(), mime_type="image/jpeg")


def judge_set(result: dict) -> dict:
    """Asks Gemini (vision) to score a generated set's coherence."""
    client = genai.Client()
    parts = [
        JUDGE_PROMPT,
        "Panel 43L:",
        _image_part(result["43L"]["path"]),
        "Panel 43R:",
        _image_part(result["43R"]["path"]),
        "Panorama 50:",
        _image_part(result["50"]["path"]),
    ]
    response = client.models.generate_content(
        model="gemini-flash-latest",
        contents=parts,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=JUDGE_SCHEMA,
        ),
    )
    if not response.text:
        raise RuntimeError("El juez no devolvió una respuesta de texto.")
    return json.loads(response.text)


async def _send(runner: Runner, user_id: str, session_id: str, text: str) -> list:
    events = []
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session_id,
        new_message=types.Content(role="user", parts=[types.Part(text=text)]),
    ):
        events.append(event)
    return events


async def run_agent_for_theme(theme: str, follow_up: str | None = None) -> dict:
    """Drives root_agent through a real conversational turn (and optional
    follow-up) and returns the result dict from whichever generate_set_*
    tool it called, or {} if it never called one (e.g. it only pitched
    concepts and no follow_up was given to resolve them).
    """
    user_id = "eval"
    session_service = InMemorySessionService()
    session = await session_service.create_session(
        app_name="tu_arte_mi_arte", user_id=user_id
    )
    runner = Runner(
        app_name="tu_arte_mi_arte",
        agent=root_agent,
        session_service=session_service,
    )

    events = await _send(runner, user_id, session.id, theme)
    if follow_up:
        events += await _send(runner, user_id, session.id, follow_up)

    result: dict = {}
    for event in events:
        for resp in event.get_function_responses():
            if resp.name in ("generate_set_diptico", "generate_set_split"):
                result = resp.response
    return result


def run_eval(themes: list[str]) -> list[dict]:
    runs = []
    for theme in themes:
        print(f"-> generando conjunto para: {theme!r}")
        result = asyncio.run(run_agent_for_theme(theme))
        if not result:
            print("   ERROR: el agente no llamó a generate_set_diptico/split")
            runs.append({"theme": theme, "result": result, "judgment": None})
            continue
        if any("error" in panel for panel in result.values()):
            print(f"   ERROR generando el conjunto: {result}")
            runs.append({"theme": theme, "result": result, "judgment": None})
            continue

        print("   evaluando coherencia...")
        judgment = judge_set(result)
        print(
            f"   paleta/luz={judgment['palette_lighting_coherence']} "
            f"mundo={judgment['world_story_coherence']} "
            f"archetype_diversity={judgment['archetype_diversity']} "
            f"pass={judgment['overall_pass']}"
        )
        runs.append({"theme": theme, "result": result, "judgment": judgment})
    return runs


def main() -> None:
    themes = sys.argv[1:] or DEFAULT_THEMES
    runs = run_eval(themes)

    EVALS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = EVALS_DIR / "coherence_eval.json"
    out_path.write_text(json.dumps(runs, indent=2, ensure_ascii=False))

    judged = [r for r in runs if r["judgment"]]
    passed = sum(1 for r in judged if r["judgment"]["overall_pass"])
    mean_archetype_diversity = (
        sum(r["judgment"]["archetype_diversity"] for r in judged) / len(judged)
        if judged
        else 0.0
    )
    print(
        f"\n{passed}/{len(judged)} conjuntos evaluados pasaron "
        f"(de {len(runs)} temas totales)."
    )
    print(f"archetype_diversity promedio: {mean_archetype_diversity:.2f}/5")
    print(f"Detalle guardado en {out_path}")


if __name__ == "__main__":
    main()
