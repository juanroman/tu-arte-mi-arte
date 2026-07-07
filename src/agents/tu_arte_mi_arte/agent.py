from google.adk.agents.llm_agent import Agent

from engine.art_direction import build_prompt, load_art_direction
from engine.generation import edit_image as edit_image_ai
from engine.generation import generate_image as generate_image_ai
from engine.preview import compose_preview as compose_preview_ai
from engine.split import load_split_config
from engine.split import split_wide_image as split_wide_image_ai


def generate_image(theme: str, aspect_ratio: str = "1:1") -> dict:
    """Genera una única imagen suelta con IA a partir de una descripción y la
    guarda en disco.

    Usa aspect_ratio '9:16' para vertical, '16:9' para horizontal, '1:1' para
    cuadrada. Aplica automáticamente la dirección de arte de la casa. Úsala
    solo cuando el usuario pida explícitamente una sola pieza suelta o un
    panel específico; para un tema nuevo sin esa aclaración, usa generate_set.
    """
    direction = load_art_direction()
    prompt = build_prompt(theme, direction)
    return generate_image_ai(prompt=prompt, aspect_ratio=aspect_ratio)


def refine_image(image_id: str, change: str) -> dict:
    """Refina una imagen ya generada aplicando una corrección puntual.

    Úsala cuando el usuario esté corrigiendo el último resultado (p. ej. "más
    otoñal", "quita el letrero") en vez de pedir un tema nuevo. `change` debe
    describir qué cambia, y opcionalmente qué se conserva. La imagen resultante
    mantiene la composición pero aplica el ajuste pedido.
    """
    return edit_image_ai(instruction=change, image_id=image_id)


def generate_set_diptico(scene_43l: str, scene_43r: str, scene_50: str) -> dict:
    """Genera de una sola vez las tres piezas de arte de la casa en modo
    díptico (PRD §7.3/§7.4): 43L y 43R como dos imágenes 9:16 independientes,
    y 50 como panorama 16:9.

    Cada `scene_*` debe ser una descripción de escena completa y autónoma
    (sujeto + acción + lugar + composición/encuadre + luz) para ese panel
    específico, ya elaborada por el agente a partir del tema/concepto
    acordado con el usuario. Las tres escenas deben usar tipos de plano
    (archetypes) claramente distintos entre sí — no repitas el mismo tipo de
    toma con el sujeto cambiado. No incluyas lenguaje de adyacencia/layout
    ("a la derecha de", "continúa la escena") ni flags de aspect ratio/cámara.

    Cada panel se genera de forma independiente (sin referenciarse entre
    sí) — la coherencia del conjunto viene solo del estilo de casa
    compartido (aplicado automáticamente) y de que las tres escenas nacen
    del mismo tema/turno.

    Esta es la tool por defecto cuando el usuario propone un tema nuevo de
    arte, ya que la v1 siempre produce el conjunto de la casa, no piezas
    sueltas. Devuelve un dict con el resultado de cada panel; si algún paso
    falla, detiene la cadena y devuelve lo generado hasta ahí más el error.
    """
    direction = load_art_direction()

    results: dict = {}

    results["43L"] = generate_image_ai(
        prompt=build_prompt(scene_43l, direction), aspect_ratio="9:16"
    )
    if "error" in results["43L"]:
        return results

    results["43R"] = generate_image_ai(
        prompt=build_prompt(scene_43r, direction), aspect_ratio="9:16"
    )
    if "error" in results["43R"]:
        return results

    results["50"] = generate_image_ai(
        prompt=build_prompt(scene_50, direction), aspect_ratio="16:9"
    )
    return results


def generate_set_split(scene_wide: str, scene_50: str) -> dict:
    """Genera de una sola vez las tres piezas de arte de la casa en modo
    split (PRD §7.3): una sola imagen ancha que se parte en dos para las
    43" (con compensación de marco), y 50 como panorama 16:9 independiente.

    Usa esta tool solo si el usuario pide explícitamente el modo split (p.
    ej. "en modo split", "una sola imagen partida"); si no lo aclara, usa
    generate_set_diptico.

    `scene_wide` describe la composición panorámica única para las dos 43"
    (sujeto + acción + lugar + composición/encuadre + luz) — no le pidas que
    mantenga el centro vacío; el recorte final se ajusta con la
    compensación de marco ya calibrada, no con instrucciones al modelo.
    `scene_50` describe una escena distinta para el panel 50, con un tipo de
    plano diferente al de `scene_wide` (ideal: más abierto/establishing, ya
    que 50 es el panorama). Ninguna escena debe incluir lenguaje de
    adyacencia/layout ni flags de aspect ratio/cámara.

    Devuelve un dict con 'wide', '43L', '43R' y '50'; si algún paso falla,
    detiene la cadena y devuelve lo generado hasta ahí más el error.
    """
    direction = load_art_direction()
    split_config = load_split_config()

    results: dict = {}

    results["wide"] = generate_image_ai(
        prompt=build_prompt(scene_wide, direction),
        aspect_ratio=split_config.wide_aspect_ratio,
    )
    if "error" in results["wide"]:
        return results

    split_result = split_wide_image_ai(
        results["wide"]["image_id"], split_config.gap_fraction
    )
    if "error" in split_result:
        results["error"] = split_result["error"]
        return results
    results["43L"] = split_result["left"]
    results["43R"] = split_result["right"]

    results["50"] = generate_image_ai(
        prompt=build_prompt(scene_50, direction), aspect_ratio="16:9"
    )
    return results


def compose_preview(image_43l: str, image_43r: str, image_50: str) -> dict:
    """Compone el preview de la sala pegando las tres piezas del conjunto
    (43L, 43R, 50) sobre la foto real de la pared (PRD §7.5).

    Úsala después de generate_set (o de un refine_image sobre alguna pieza),
    pasando los image_id más recientes de cada panel. Muestra las tres
    pantallas juntas para poder juzgar el conjunto como un todo.
    """
    return compose_preview_ai({"43L": image_43l, "43R": image_43r, "50": image_50})


root_agent = Agent(
    model="gemini-flash-latest",
    name="root_agent",
    description="Asistente de arte generativo para las Samsung Frame TVs de la casa.",
    instruction=(
        "Eres el asistente de arte generativo de la casa. La casa siempre "
        "se piensa como un conjunto de tres pantallas (43L, 43R, 50), no "
        "como piezas sueltas — para un tema nuevo usa generate_set_diptico "
        "o generate_set_split, nunca generate_image (esa es solo para "
        "cuando el usuario pida explícitamente una sola pieza o un panel "
        "específico). \n\n"
        "ETAPA 1 — CONCEPTO (sin llamar ninguna tool todavía). Cuando el "
        "usuario proponga un tema nuevo, evalúa si es amplio (admite varias "
        "direcciones visuales distintas, p. ej. 'Día de los Muertos', "
        "'otoño') o ya es un concepto específico (p. ej. 'bicicletas "
        "vintage en Santorini', 'faroles de papel picado en un patio'). Si "
        "es amplio, responde en el chat con 2-3 opciones de concepto "
        "concretas (una línea cada una, p. ej. 'podría ser retratos de "
        "calaveras, un jardín de cempasúchil, o una ofrenda — ofrenda da "
        "más variedad para el conjunto') y espera a que el usuario elija o "
        "proponga otra antes de generar nada. Si ya es específico, sáltate "
        "esta etapa y ve directo a elaborar.\n\n"
        "ETAPA 2 — ELABORACIÓN (una vez el concepto está definido, en este "
        "turno o uno anterior). Escribe tú mismo una descripción de escena "
        "distinta para cada panel (3 para diptico: 43L, 43R, 50; 2 para "
        "split: wide, 50), en prosa, siguiendo sujeto + acción + "
        "lugar/escena + composición/encuadre + luz. No uses lenguaje de "
        "adyacencia o layout ('a la derecha de', 'continúa la escena', "
        "'misma sesión que la anterior') ni menciones aspect ratio o flags "
        "de cámara — eso lo maneja la tool. Elige un tipo de plano "
        "(archetype) distinto para cada panel dentro del mismo conjunto, "
        "nunca repitas el mismo tipo de toma con el sujeto cambiado; usa "
        "como guía (no como lista cerrada): macro/detalle, plano general "
        "abierto/paisaje, figura humana en la escena, silueta, textura/"
        "abstracto en close-up, aéreo/elevado, reflejo/agua, líneas que "
        "guían la mirada, luz dorada/contraluz. Para el panel 50 inclínate "
        "por un plano general abierto, ya que es el panorama. Por defecto "
        "usa generate_set_diptico; usa generate_set_split solo si el "
        "usuario lo pide explícitamente (p. ej. 'en modo split', 'una sola "
        "imagen partida'). \n\n"
        "Si el usuario está corrigiendo o ajustando el resultado más reciente "
        "de la conversación (p. ej. 'más otoñal', 'quita eso'), en vez de "
        "generar una imagen nueva desde cero usa refine_image con el image_id "
        "de esa última pieza y una descripción de qué cambiar. "
        "Confirma siempre el/los image_id de lo que generaste o refinaste. "
        "Cuando el usuario pida ver el preview del conjunto (p. ej. "
        "'muéstrame el preview', 'cómo se ve en la sala'), usa "
        "compose_preview con los image_id más recientes de 43L, 43R y 50 de "
        "la conversación (si el usuario refinó una pieza, usa el image_id "
        "más nuevo de esa pieza)."
    ),
    tools=[
        generate_image,
        refine_image,
        generate_set_diptico,
        generate_set_split,
        compose_preview,
    ],
)
