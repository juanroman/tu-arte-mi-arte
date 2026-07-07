from google.adk.agents.llm_agent import Agent

from engine.art_direction import build_prompt, load_art_direction
from engine.generation import edit_image as edit_image_ai
from engine.generation import generate_image as generate_image_ai

# PRD §6: 43L y 43R son verticales 9:16, la 50 es horizontal 16:9.
PANEL_ASPECT_RATIOS = {"43L": "9:16", "43R": "9:16", "50": "16:9"}


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
    43L, 43R y 50), todas para el mismo tema.

    Esta es la tool por defecto cuando el usuario propone un tema nuevo de
    arte (p. ej. "bicicletas vintage en Santorini"), ya que la v1 siempre
    produce el conjunto de la casa, no piezas sueltas. Cada pieza se genera
    de forma independiente, sin referenciarse entre sí todavía (eso llega en
    una iteración posterior). 43L y 43R salen en 9:16 y 50 en 16:9. Devuelve
    un dict con el resultado de cada panel.
    """
    direction = load_art_direction()
    prompt = build_prompt(theme, direction)
    return {
        panel: generate_image_ai(prompt=prompt, aspect_ratio=aspect_ratio)
        for panel, aspect_ratio in PANEL_ASPECT_RATIOS.items()
    }


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
