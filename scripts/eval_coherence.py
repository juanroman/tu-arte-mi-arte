"""Eval script (dev-only, NOT a pytest test): runs generate_set across a list
of themes and asks Gemini (vision) to judge set coherence per PRD §7.4.

Hits the real API, costs money, and is non-deterministic — run manually with
`uv run python scripts/eval_coherence.py` (optionally passing themes as CLI
args) when validating changes to the image-to-image chaining in generate_set.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "agents"))

from dotenv import load_dotenv  # noqa: E402
from google import genai  # noqa: E402
from google.genai import types  # noqa: E402
from tu_arte_mi_arte.agent import generate_set  # noqa: E402

load_dotenv()

EVALS_DIR = Path(__file__).resolve().parent.parent / "data" / "evals"

DEFAULT_THEMES = [
    "bicicletas vintage estacionadas tipo Santorini",
    "un mercado de flores en la Provenza al amanecer",
    "veleros anclados en una bahía de la costa amalfitana",
    "una cabaña de montaña rodeada de pinos nevados",
    "un café de esquina en Buenos Aires en otoño",
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
        "diptych_seam_coherence": types.Schema(
            type=types.Type.INTEGER,
            description=(
                "1-5: ¿43L y 43R embonan como un díptico (mismo horizonte/luz)?"
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
        "diptych_seam_coherence",
        "overall_pass",
        "notes",
    ],
)

JUDGE_PROMPT = (
    "Eres un juez de dirección de arte. Te muestro tres imágenes generadas para "
    "las pantallas de una casa a partir del mismo tema: primero el panel 43L "
    "(vertical), luego 43R (vertical, mitad derecha del díptico) y finalmente el "
    "panorama de la pantalla 50 (horizontal). Evalúa si se leen como UN CONJUNTO "
    "curado e intencional -no como tres imágenes sueltas e inconexas- según estos "
    "criterios del PRD de la casa: tratamiento visual compartido (paleta, luz, "
    "grano, tono), el díptico 43L/43R embona como par (mismo horizonte/luz), y "
    "el panorama de la 50 pertenece al mismo mundo/tema que el par."
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


def run_eval(themes: list[str]) -> list[dict]:
    runs = []
    for theme in themes:
        print(f"-> generando conjunto para: {theme!r}")
        result = generate_set(theme)
        if any("error" in panel for panel in result.values()):
            print(f"   ERROR generando el conjunto: {result}")
            runs.append({"theme": theme, "result": result, "judgment": None})
            continue

        print("   evaluando coherencia...")
        judgment = judge_set(result)
        print(
            f"   paleta/luz={judgment['palette_lighting_coherence']} "
            f"mundo={judgment['world_story_coherence']} "
            f"diptico={judgment['diptych_seam_coherence']} "
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
    print(
        f"\n{passed}/{len(judged)} conjuntos evaluados pasaron "
        f"(de {len(runs)} temas totales)."
    )
    print(f"Detalle guardado en {out_path}")


if __name__ == "__main__":
    main()
