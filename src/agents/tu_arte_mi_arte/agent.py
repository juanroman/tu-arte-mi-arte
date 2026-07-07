from google.adk.agents.llm_agent import Agent

from engine.art_direction import build_prompt, load_art_direction
from engine.generation import edit_image as edit_image_ai
from engine.generation import generate_image as generate_image_ai
from engine.generation import generate_image_with_references as generate_with_refs_ai
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


def generate_set(theme: str, mode: str = "diptico") -> dict:
    """Genera de una sola vez las tres piezas de arte de la casa (paneles
    43L, 43R y 50), todas para el mismo tema y coherentes entre sí como un
    conjunto (PRD §7.4).

    Esta es la tool por defecto cuando el usuario propone un tema nuevo de
    arte (p. ej. "bicicletas vintage en Santorini"), ya que la v1 siempre
    produce el conjunto de la casa, no piezas sueltas.

    `mode` controla el formato de las dos 43" (PRD §7.3): 'diptico'
    (default) genera 43L y 43R como dos imágenes 9:16 independientes pero
    condicionadas entre sí; 'split' genera una sola imagen ancha y la parte
    en dos con compensación de marco. Usa 'split' solo si el usuario lo pide
    explícitamente (p. ej. "en modo split", "una sola imagen partida"); si no
    lo aclara, usa 'diptico'.
    """
    if mode == "split":
        return _generate_split_set(theme)
    if mode != "diptico":
        return {"error": f"Modo desconocido: {mode!r}"}
    return _generate_diptico_set(theme)


def _generate_diptico_set(theme: str) -> dict:
    """Modo díptico (default): 43L se genera primero y 43R se genera
    condicionada a 43L como "otra foto de la misma sesión, ángulo distinto"
    (§7.7: [referencias] + [instrucción de relación] + [nuevo escenario]).
    Importante: el prompt evita cualquier lenguaje de adyacencia/layout
    ("a la derecha de", "continúa hacia", "díptico") — con Nano Banana 2 esa
    redacción empuja al modelo a devolver un collage/grid de sub-imágenes en
    vez de una sola composición, incluso pidiéndole explícitamente que no lo
    haga. Pedirle una foto más de la misma sesión (sin relación espacial
    explícita) es lo que produce una sola imagen limpia de forma consistente;
    la relación de layout entre 43L/43R es curaduría nuestra, no del modelo.
    La 50 se genera igual, condicionada al par, para compartir mundo y
    paleta. 43L y 43R salen en 9:16 y 50 en 16:9. Devuelve un dict con el
    resultado de cada panel; si algún paso falla, detiene la cadena y
    devuelve lo generado hasta ahí más el error.
    """
    direction = load_art_direction()
    base_prompt = build_prompt(theme, direction)

    results: dict = {}

    results["43L"] = generate_image_ai(prompt=base_prompt, aspect_ratio="9:16")
    if "error" in results["43L"]:
        return results

    results["43R"] = generate_with_refs_ai(
        prompt=(
            f"{base_prompt} Una nueva fotografía de la misma sesión que la "
            "imagen de referencia: mismo lugar, mismos sujetos, misma luz "
            "dorada, misma paleta, mismo grano de película y estilo "
            "fotográfico — pero un ángulo de cámara y composición distintos "
            "dentro de la escena, como si se hubiera tomado momentos después "
            "durante la misma sesión."
        ),
        aspect_ratio="9:16",
        reference_image_ids=[results["43L"]["image_id"]],
    )
    if "error" in results["43R"]:
        return results

    results["50"] = generate_with_refs_ai(
        prompt=(
            f"{base_prompt} Una toma general abierta de la misma sesión que "
            "las imágenes de referencia: mismo lugar, mismos sujetos, misma "
            "luz dorada, misma paleta, mismo grano de película y estilo "
            "fotográfico — un encuadre más abierto y un ángulo de cámara "
            "distinto al de las referencias, como si se hubiera tomado "
            "momentos después durante la misma sesión."
        ),
        aspect_ratio="16:9",
        reference_image_ids=[results["43L"]["image_id"], results["43R"]["image_id"]],
    )
    return results


def _generate_split_set(theme: str) -> dict:
    """Modo split (PRD §7.3): una sola imagen ancha pensada para partirse
    entre las dos 43", con la regla anti-centrado inyectada en el prompt
    (el centro se recorta como franja de compensación de marco). La 50 se
    genera condicionada a la imagen ancha para compartir mundo y paleta.
    Devuelve un dict con 'wide', '43L', '43R' y '50'; si algún paso falla,
    detiene la cadena y devuelve lo generado hasta ahí más el error.
    """
    direction = load_art_direction()
    base_prompt = build_prompt(theme, direction)
    split_config = load_split_config()

    results: dict = {}

    results["wide"] = generate_image_ai(
        prompt=(
            f"{base_prompt} Genera una sola composición panorámica pensada "
            "para partirse verticalmente por la mitad entre dos pantallas: "
            "mantén el área central de la composición vacía de sujetos "
            "importantes y reparte el peso visual a los tercios laterales, "
            "ya que el centro se recortará."
        ),
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

    results["50"] = generate_with_refs_ai(
        prompt=(
            f"{base_prompt} Una toma general abierta de la misma sesión que "
            "la imagen de referencia: mismo lugar, mismos sujetos, misma luz "
            "dorada, misma paleta, mismo grano de película y estilo "
            "fotográfico — un encuadre más abierto y un ángulo de cámara "
            "distinto al de la referencia, como si se hubiera tomado "
            "momentos después durante la misma sesión."
        ),
        aspect_ratio="16:9",
        reference_image_ids=[results["wide"]["image_id"]],
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
        "Eres el asistente de arte generativo de la casa. "
        "Cuando el usuario proponga un tema nuevo (p. ej. 'bicicletas "
        "vintage en Santorini'), usa generate_set: la casa siempre se "
        "piensa como un conjunto de tres pantallas (43L, 43R, 50), no como "
        "piezas sueltas. Por defecto usa mode='diptico' (43L y 43R como dos "
        "imágenes 9:16 condicionadas entre sí); usa mode='split' solo si el "
        "usuario lo pide explícitamente (p. ej. 'en modo split', 'una sola "
        "imagen partida'), que genera una sola imagen ancha partida en dos "
        "con compensación de marco. Usa generate_image en su lugar solo si "
        "el usuario pide explícitamente una sola pieza o un panel "
        "específico. "
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
    tools=[generate_image, refine_image, generate_set, compose_preview],
)
