"""Eval script (dev-only, NOT a pytest test): checks whether Nano Banana 2
honors specific framing/shot-type instructions more reliably when phrased as
verbose descriptive prose (Google's own prompting-guide pattern: "shot from
a [angle] with a [lens]") vs. as a short jargon keyword ("primer plano
macro"), which is how `agent.py`'s archetype menu currently phrases them.

Default mode directly exercises `engine.generation.generate_image` (bypasses
the agent) so the comparison isolates prompt phrasing from the LLM's own
scene-writing variance — see KNOWN_ISSUES.md #2 for the original repro (a
requested macro shot came back as a wide ambient shot).

`--agent` mode instead drives `root_agent` itself (reusing
`eval_coherence.run_agent_for_theme`) for a theme likely to pick the
macro/detalle archetype, to check what root_agent actually authors for that
archetype before/after changing its instruction wording in agent.py.

Hits the real API, costs money, and is non-deterministic — run manually with
`uv run python scripts/eval_framing.py` (or `--agent`) when deciding whether
to update the archetype menu wording in agent.py.
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "agents"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv  # noqa: E402
from eval_coherence import run_agent_for_theme  # noqa: E402
from google import genai  # noqa: E402
from google.genai import types  # noqa: E402

from engine.art_direction import build_prompt, load_art_direction  # noqa: E402
from engine.generation import generate_image  # noqa: E402

load_dotenv()

EVALS_DIR = Path(__file__).resolve().parent.parent / "data" / "evals"

# Each case pairs the same subject under two phrasings of the same requested
# framing (macro/extreme close-up): the short jargon keyword style currently
# used in agent.py's archetype menu, vs. the verbose descriptive pattern from
# Google's Gemini image-generation prompting guide ("shot from a [camera
# angle] with a [lens type]").
CASES = [
    {
        "name": "calcetin_navideno",
        "keyword": (
            "Un calcetín navideño de fieltro rojo con ribete blanco cuelga de "
            "la repisa de una chimenea de piedra. Primer plano macro."
        ),
        "verbose": (
            "Un calcetín navideño de fieltro rojo con ribete blanco cuelga de "
            "la repisa de una chimenea de piedra. Tomada desde una perspectiva "
            "macro extrema con un lente macro de enfoque cercano, llenando "
            "todo el cuadro con la textura del fieltro y el ribete de peluche "
            "blanco, fondo completamente desenfocado."
        ),
    },
    {
        "name": "taza_de_cafe",
        "keyword": (
            "Una taza de café con espuma sobre una mesa de madera junto a una "
            "ventana. Primer plano macro."
        ),
        "verbose": (
            "Una taza de café con espuma sobre una mesa de madera junto a una "
            "ventana. Tomada desde una perspectiva macro extrema con un lente "
            "macro de enfoque cercano, llenando todo el cuadro con la textura "
            "de la espuma y el grano de la madera, fondo completamente "
            "desenfocado."
        ),
    },
    {
        "name": "gota_de_rocio",
        "keyword": (
            "Gotas de rocío sobre el pétalo de una rosa en un jardín al "
            "amanecer. Primer plano macro."
        ),
        "verbose": (
            "Gotas de rocío sobre el pétalo de una rosa en un jardín al "
            "amanecer. Tomada desde una perspectiva macro extrema con un "
            "lente macro de enfoque cercano, llenando todo el cuadro con la "
            "textura del pétalo y el detalle de las gotas, fondo completamente "
            "desenfocado."
        ),
    },
]

JUDGE_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    properties={
        "is_tight_macro": types.Schema(
            type=types.Type.BOOLEAN,
            description=(
                "true si la imagen es un acercamiento macro genuino que llena "
                "el cuadro con el sujeto/textura, sin contexto ambiental "
                "amplio (piso, pared, ventana completa, habitación)"
            ),
        ),
        "framing_match": types.Schema(
            type=types.Type.INTEGER,
            description=(
                "1-5: qué tan bien coincide el encuadre real con un "
                "'primer plano macro' pedido explícitamente (5 = macro "
                "perfecto, 1 = toma ambiental/plano general, sin relación "
                "con lo pedido)"
            ),
        ),
        "notes": types.Schema(
            type=types.Type.STRING,
            description="Justificación breve, en español, del puntaje",
        ),
    },
    required=["is_tight_macro", "framing_match", "notes"],
)

JUDGE_PROMPT = (
    "Se le pidió a un modelo de generación de imágenes un 'primer plano "
    "macro' de: {subject}. Evalúa la imagen resultante: ¿es un acercamiento "
    "macro genuino que llena el cuadro con el sujeto/su textura, o salió "
    "como una toma más ambiental/abierta que muestra el contexto completo "
    "de la escena (habitación, piso, ventana, paisaje)?"
)

# Tema específico (salta la etapa de pitch de conceptos) elegido para que
# root_agent probablemente elija el archetype macro/detalle en al menos un
# panel, para poder comparar la escena que el agente redacta antes/después
# de reforzar esa entrada del menú en agent.py.
AGENT_THEMES = [
    "adornos navideños de fieltro colgados de una chimenea de piedra",
]


def _image_part(path: str) -> types.Part:
    return types.Part.from_bytes(data=Path(path).read_bytes(), mime_type="image/jpeg")


def judge_framing(subject: str, image_path: str) -> dict:
    client = genai.Client()
    response = client.models.generate_content(
        model="gemini-flash-latest",
        contents=[JUDGE_PROMPT.format(subject=subject), _image_part(image_path)],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=JUDGE_SCHEMA,
        ),
    )
    if not response.text:
        raise RuntimeError("El juez no devolvió una respuesta de texto.")
    return json.loads(response.text)


def run_variant(name: str, variant: str, scene: str) -> dict:
    direction = load_art_direction()
    prompt = build_prompt(scene, direction)
    result = generate_image(prompt=prompt, aspect_ratio="9:16")
    if "error" in result:
        print(f"   ERROR generando {name}/{variant}: {result}")
        return {"variant": variant, "scene": scene, "result": result, "judgment": None}

    judgment = judge_framing(subject=name, image_path=result["path"])
    print(
        f"   {variant}: is_tight_macro={judgment['is_tight_macro']} "
        f"framing_match={judgment['framing_match']}/5"
    )
    return {"variant": variant, "scene": scene, "result": result, "judgment": judgment}


def run_agent_framing_case(theme: str) -> dict:
    """Drives root_agent (not a direct generate_image call) for a theme
    likely to pick a macro/detalle archetype for at least one panel, then
    judges every returned panel's image against how tight/macro it reads.
    Used to compare root_agent's real authored scenes before/after the
    archetype wording change in agent.py.
    """
    result = asyncio.run(run_agent_for_theme(theme))
    if not result:
        print(f"   ERROR: el agente no llamó a generate_set_* para {theme!r}")
        return {"theme": theme, "result": result, "judgments": None}
    if any("error" in panel for panel in result.values() if isinstance(panel, dict)):
        print(f"   ERROR generando el conjunto para {theme!r}: {result}")
        return {"theme": theme, "result": result, "judgments": None}

    judgments = {}
    for panel, panel_result in result.items():
        if not isinstance(panel_result, dict) or "path" not in panel_result:
            continue
        judgment = judge_framing(subject=theme, image_path=panel_result["path"])
        judgments[panel] = judgment
        print(
            f"   {panel}: is_tight_macro={judgment['is_tight_macro']} "
            f"framing_match={judgment['framing_match']}/5"
        )
    return {"theme": theme, "result": result, "judgments": judgments}


def run_agent_mode() -> None:
    runs = [run_agent_framing_case(theme) for theme in AGENT_THEMES]

    EVALS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = EVALS_DIR / "framing_eval_agent.json"
    out_path.write_text(json.dumps(runs, indent=2, ensure_ascii=False))
    print(f"Detalle guardado en {out_path}")


def main() -> None:
    if "--agent" in sys.argv:
        run_agent_mode()
        return

    runs = []
    for case in CASES:
        print(f"-> {case['name']!r}")
        keyword_run = run_variant(case["name"], "keyword", case["keyword"])
        verbose_run = run_variant(case["name"], "verbose", case["verbose"])
        runs.append(
            {"name": case["name"], "keyword": keyword_run, "verbose": verbose_run}
        )

    EVALS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = EVALS_DIR / "framing_eval.json"
    out_path.write_text(json.dumps(runs, indent=2, ensure_ascii=False))

    def _score(run: dict) -> int:
        j = run["judgment"]
        return j["framing_match"] if j else 0

    keyword_scores = [_score(r["keyword"]) for r in runs]
    verbose_scores = [_score(r["verbose"]) for r in runs]

    print("\n--- resumen ---")
    print(
        f"keyword ('primer plano macro'): "
        f"promedio framing_match={sum(keyword_scores) / len(keyword_scores):.2f}/5 "
        f"({keyword_scores})"
    )
    print(
        f"verbose (estilo guía de Google): "
        f"promedio framing_match={sum(verbose_scores) / len(verbose_scores):.2f}/5 "
        f"({verbose_scores})"
    )
    print(f"Detalle guardado en {out_path}")


if __name__ == "__main__":
    main()
