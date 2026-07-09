from google.adk.agents.llm_agent import Agent

from engine.art_direction import build_prompt, load_art_direction
from engine.generation import edit_image as edit_image_ai
from engine.generation import generate_final_high_res as generate_final_high_res_ai
from engine.generation import generate_image as generate_image_ai
from engine.preview import compose_preview as compose_preview_ai
from engine.split import load_split_config
from engine.split import split_wide_image as split_wide_image_ai
from engine.tv_deploy import deploy_set_to_43_panels as deploy_set_to_43_panels_ai


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


def finalize_high_res(image_id: str, is_split_wide: bool = False) -> dict:
    """Produce la versión final en alta resolución (4K) de un draft aprobado
    (PRD §7.7), re-generándolo vía image-to-image con una instrucción
    estricta que preserva layout/geometría/contenido — nunca sube de
    resolución a ciegas ni cambia de modelo entre draft y final.

    Usa is_split_wide=True solo cuando image_id es la imagen ancha ('wide')
    de un conjunto generado con generate_set_split (la fuente antes de
    partir); en ese caso la función re-genera esa imagen ancha en 4K y la
    vuelve a partir con la misma compensación de marco, devolviendo
    directamente las mitades finales 43L/43R. Para cualquier otro panel
    (43L/43R de un conjunto díptico, o 50 en cualquier modo) usa
    is_split_wide=False (default) y pasa el image_id de ese panel
    individual.
    """
    result = generate_final_high_res_ai(image_id)
    if "error" in result or not is_split_wide:
        return result

    split_config = load_split_config()
    split_result = split_wide_image_ai(result["image_id"], split_config.gap_fraction)
    if "error" in split_result:
        return split_result
    return {"43L": split_result["left"], "43R": split_result["right"]}


def compose_preview(image_43l: str, image_43r: str, image_50: str) -> dict:
    """Compone el preview de la sala pegando las tres piezas del conjunto
    (43L, 43R, 50) sobre la foto real de la pared (PRD §7.5).

    Úsala después de generate_set (o de un refine_image sobre alguna pieza),
    pasando los image_id más recientes de cada panel. Muestra las tres
    pantallas juntas para poder juzgar el conjunto como un todo.
    """
    return compose_preview_ai({"43L": image_43l, "43R": image_43r, "50": image_50})


def deploy_to_43_panels(image_43l: str, image_43r: str) -> dict:
    """Sube y muestra en las dos TVs de 43" (43L, 43R) las piezas finales
    ya aprobadas en alta resolución (PRD §3.3).

    Llámala automáticamente, sin que el usuario lo pida aparte, en el
    momento en que tengas los image_id finales en 4K de AMBAS 43" — ya sea
    por dos llamadas independientes a finalize_high_res en modo díptico, o
    por una sola llamada con is_split_wide=True que ya da las dos de un
    jalón. No la llames con el image_id de un panel cuyo finalize_high_res
    todavía no haya tenido éxito, ni para el panel 50 (fuera de alcance por
    ahora). Devuelve {'43L': {...}, '43R': {...}}; cada valor trae
    'content_id' (éxito) o 'error' (esa TV en particular falló) — ambas
    pantallas se intentan siempre, una no bloquea a la otra.
    """
    return deploy_set_to_43_panels_ai(image_43l=image_43l, image_43r=image_43r)


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
        "más nuevo de esa pieza).\n\n"
        "ETAPA 3 — APROBACIÓN. Cuando el usuario apruebe la versión actual "
        "para colgar (p. ej. 'apruébalo', 'sube esta versión', 'me gusta, "
        "ya quedó'), usa finalize_high_res para producir la versión final "
        "en 4K de cada pieza, siempre a partir del image_id más reciente de "
        "cada panel en la conversación (el draft aprobado, o la última "
        "corrección si hubo refine_image de por medio). Si el conjunto se "
        "generó con generate_set_diptico, llama finalize_high_res tres "
        "veces (43L, 43R, 50), siempre con is_split_wide=False. Si se "
        "generó con generate_set_split, llama finalize_high_res una vez "
        "sobre el image_id de 'wide' con is_split_wide=True (esto ya "
        "devuelve los 43L/43R finales directamente, no vuelvas a partir "
        "nada tú) y otra vez sobre el image_id de 50 con "
        "is_split_wide=False. Confirma al usuario los image_id finales de "
        "cada pieza.\n\n"
        "ETAPA 4 — DESPLIEGUE (automático, sin que el usuario lo pida "
        'aparte). En cuanto tengas los image_id finales en 4K de AMBAS 43" '
        "(43L y 43R) —por dos llamadas de finalize_high_res en modo "
        "díptico, o por una sola con is_split_wide=True que ya las da "
        "juntas— llama deploy_to_43_panels con esos dos image_id en el "
        "mismo turno. Si algún panel de 43L/43R falló en finalize_high_res, "
        "resuelve ese fallo primero (reintento o pivote) y no llames "
        "deploy_to_43_panels hasta tener ambos finales listos. El panel 50 "
        "no se despliega automáticamente todavía (queda pendiente de carga "
        "manual). Reporta el resultado por pantalla (p. ej. '43L subida y "
        "mostrada', '43R: no se pudo conectar, queda pendiente de carga "
        "manual') — una falla de deploy_to_43_panels es de red/TV, no de "
        "generación o política, y nunca debe reportarse con el mismo "
        "lenguaje que un rechazo o fallo de finalize_high_res.\n\n"
        "MANEJO DE ERRORES. Si una tool devuelve un dict con 'error': "
        "distingue por la clave 'policy_rejection'. Si "
        "'policy_rejection' es true (rechazo real de política o derechos, "
        "irrecuperable con el mismo tema), NUNCA reescribas la escena ni "
        "vuelvas a llamar la tool por tu cuenta — dile al usuario qué se "
        "rechazó y ofrécele un pivote que capture la época/lugar/estética "
        "del tema de forma libre de derechos y on-brand (ej.: 'De los "
        "Beatles no puedo (personas reales). Pero puedo capturar su "
        "época — Abbey Road, psicodelia sesentera. ¿Le entro?'), y espera "
        "su confirmación antes de generar de nuevo. Si el error no trae "
        "'policy_rejection' (falla técnica que ya agotó sus reintentos), "
        "informa el fallo al usuario en una frase clara y sin tecnicismos "
        "(nunca muestres el texto crudo del error ni te quedes sin "
        "responder), y ofrece intentarlo de nuevo si el usuario quiere.\n\n"
        "RESULTADOS MIXTOS (generate_set_diptico/generate_set_split). Estas "
        "tools generan cada panel por separado y se detienen en el primer "
        "error: el dict que devuelven puede traer algunos paneles ya "
        "completados (con su image_id) junto con, como máximo, un panel "
        "con 'error' — los paneles siguientes de la cadena ni se "
        "intentaron. NUNCA descartes ni dejes de confirmarle al usuario "
        "los image_id que sí se generaron, y NUNCA digas que 'todo' fue "
        "rechazado si al menos un panel tiene image_id — sé preciso sobre "
        "cuál panel específico falló y cuáles ya están listos. Aplica la "
        "distinción de 'policy_rejection' del párrafo anterior solo al "
        "panel que falló (pivote si aplica, o reintento). Una vez que el "
        "usuario confirme cómo seguir, repara SOLO el panel fallido con "
        "generate_image (aspect_ratio '9:16' para 43L/43R, '16:9' para "
        "50) — nunca vuelvas a llamar generate_set_diptico/"
        "generate_set_split completo, ya que eso regeneraría también los "
        "paneles que ya estaban bien. Excepción: si lo que falló es la "
        "imagen 'wide' de generate_set_split (antes de que exista ningún "
        "panel 43L/43R todavía), no hay nada que preservar — ahí sí es un "
        "rechazo total y corresponde volver a llamar generate_set_split "
        "completo. El mismo principio aplica a las llamadas repetidas de "
        "finalize_high_res en la etapa de aprobación: reporta el "
        "resultado real de cada una (qué panel sí quedó en 4K, cuál "
        "falló), nunca generalices el peor resultado a todo el conjunto."
    ),
    tools=[
        generate_image,
        refine_image,
        generate_set_diptico,
        generate_set_split,
        compose_preview,
        finalize_high_res,
        deploy_to_43_panels,
    ],
)
