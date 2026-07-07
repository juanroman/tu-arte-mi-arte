from google.adk.agents.llm_agent import Agent

from engine.art_direction import build_prompt, load_art_direction
from engine.generation import edit_image as edit_image_ai
from engine.generation import generate_image as generate_image_ai
from engine.generation import generate_image_with_references as generate_with_refs_ai


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


def generate_set(theme: str) -> dict:
    """Genera de una sola vez las tres piezas de arte de la casa (paneles
    43L, 43R y 50), todas para el mismo tema y coherentes entre sí como un
    conjunto (PRD §7.4).

    Esta es la tool por defecto cuando el usuario propone un tema nuevo de
    arte (p. ej. "bicicletas vintage en Santorini"), ya que la v1 siempre
    produce el conjunto de la casa, no piezas sueltas. Para lograr la
    coherencia del set (§7.7: [referencias] + [instrucción de relación] +
    [nuevo escenario]), 43L se genera primero y 43R se genera condicionada a
    43L ("continúa esta escena, mismo horizonte/luz/paleta"); la 50 se genera
    condicionada al par para compartir mundo y paleta. 43L y 43R salen en
    9:16 y 50 en 16:9. Devuelve un dict con el resultado de cada panel; si
    algún paso falla, detiene la cadena y devuelve lo generado hasta ahí más
    el error.
    """
    direction = load_art_direction()
    base_prompt = build_prompt(theme, direction)

    results: dict = {}

    results["43L"] = generate_image_ai(prompt=base_prompt, aspect_ratio="9:16")
    if "error" in results["43L"]:
        return results

    results["43R"] = generate_with_refs_ai(
        prompt=(
            f"{base_prompt} Continúa la escena de la imagen de referencia hacia "
            "la derecha, como la mitad complementaria de un díptico: mismo "
            "horizonte, misma luz y la misma paleta."
        ),
        aspect_ratio="9:16",
        reference_image_ids=[results["43L"]["image_id"]],
    )
    if "error" in results["43R"]:
        return results

    results["50"] = generate_with_refs_ai(
        prompt=(
            f"{base_prompt} Genera un panorama horizontal que pertenezca al "
            "mismo mundo y tema que las imágenes de referencia (mismo "
            "horizonte, luz y paleta), no una escena distinta."
        ),
        aspect_ratio="16:9",
        reference_image_ids=[results["43L"]["image_id"], results["43R"]["image_id"]],
    )
    return results


root_agent = Agent(
    model="gemini-flash-latest",
    name="root_agent",
    description="Asistente de arte generativo para las Samsung Frame TVs de la casa.",
    instruction=(
        "Eres el asistente de arte generativo de la casa. "
        "Cuando el usuario proponga un tema nuevo (p. ej. 'bicicletas "
        "vintage en Santorini'), usa generate_set: la casa siempre se "
        "piensa como un conjunto de tres pantallas (43L, 43R, 50), no como "
        "piezas sueltas. Usa generate_image en su lugar solo si el usuario "
        "pide explícitamente una sola pieza o un panel específico. "
        "Si el usuario está corrigiendo o ajustando el resultado más reciente "
        "de la conversación (p. ej. 'más otoñal', 'quita eso'), en vez de "
        "generar una imagen nueva desde cero usa refine_image con el image_id "
        "de esa última pieza y una descripción de qué cambiar. "
        "Confirma siempre el/los image_id de lo que generaste o refinaste."
    ),
    tools=[generate_image, refine_image, generate_set],
)
