"""Corredor del motor de galería por lotes (PRD §15.5, dev_plan
`dev_plan_phase_2.md` §2.2): procesa los `batch_item`/`batch_day` que un
lote ya materializado (`engine.batch_store.materialize_batch`) dejó en
`stage='pending'`, generando su imagen 1K y avanzando a `drafted` o
`needs_attention` -- sin bloquear el resto del lote y sin reimplementar
el manejo de errores de `engine.generation.generate_image` (política vs.
técnico), que se hereda tal cual leyendo la clave `policy_rejection`.

No dependency on google.adk: this module is testable in isolation and
reusable from any interface (adk web hoy, Telegram en Etapa 3).

Días split: la imagen ancha compartida por 43L/43R se genera y finaliza
una sola vez por día (nunca dos generaciones/finalizaciones
independientes para "el mismo" día, requisito duro #4 del dev_plan) --
cada intento de draft se registra en lockstep en ambas filas físicas
`batch_item` (mismo `attempts`/`stage`/`error`, sin `image_id` propio
todavía) más en `batch_day.wide_image_id`/`wide_stage`. `wide_stage`
gana un tercer valor en la etapa de finalización (§2.3): `"finalized"`
marca que la fuente ancha ya se re-generó en 4K, aunque el paso de
partición hacia 43L/43R (`split.split_wide_image`) siga pendiente o
haya fallado -- así una re-invocación que solo falló al partir nunca
vuelve a llamar `generation.generate_final_high_res` sobre la fuente,
solo reintenta el split.

Escritura atómica de días split (§2.5, requisito duro #5): cuando un
intento sobre un día split escribe más de una fila a la vez (los dos
`batch_item` de 43L/43R, y a veces también `batch_day`), lo hace en una
sola transacción vía `batch_store.record_split_day_outcome` -- nunca con
llamadas separadas a `record_item_attempt`/`record_wide_image`, que
dejarían una ventana real de inconsistencia si el proceso muere entre
escrituras (43R huérfano, o `wide_stage` sin avanzar mientras los
paneles ya sí lo hicieron, lo que dispararía una regeneración/reintento
espurio de la fuente ancha en la reinvocación, violando potencialmente
el requisito duro #1 sobre un `policy_rejection`).

Procesamiento secuencial, no paralelo, en generación/finalización: a
diferencia de `tv_deploy.deploy_set_to_panels` (TVs físicas
independientes, sin cuota compartida), la generación de imágenes
comparte una sola cuota de la API de Gemini -- procesar en paralelo
arriesgaría ráfagas de rate-limit sin ganancia real.

Etapa de subida (§4.1, dev_plan_phase_2.md): modelo de concurrencia
DISTINTO al de generación/finalización, porque la restricción real
también es distinta. Las tres TVs son dispositivos físicos
independientes sin cuota compartida (mismo principio que
`tv_deploy.deploy_set_to_panels`) -- así que `run_upload_stage` corre un
worker por TV en paralelo. Pero dentro de una misma TV, cada worker
drena su cola de items SECUENCIALMENTE: `SamsungTVArt`/el protocolo de
la TV asumen una sola conexión websocket a la vez por dispositivo (misma
asunción que sostiene todo `tv_deploy.py`), así que subir varias
imágenes a la MISMA TV en paralelo rompería esa asunción sin ganancia
real -- ni fully-secuencial entre TVs (tres veces más lento sin razón)
ni fully-paralelo sobre las hasta N*3 imágenes de un lote grande (arriesga
conexiones concurrentes contra el mismo socket físico). Tampoco hay
concepto de `policy_rejection` en la subida -- una TV no rechaza
contenido por política, solo falla de forma transitoria de red, así que
toda falla es reintentable hasta `tv_deploy_max_attempts`.
"""

import concurrent.futures
import logging
import math
from collections.abc import Callable
from pathlib import Path

from engine.art_direction import ArtDirection, build_prompt, load_art_direction
from engine.batch_store import (
    BatchDayRecord,
    BatchItemRecord,
    PanelOutcome,
    WideOutcome,
    get_batch,
    get_batch_days,
    get_batch_items,
    load_batch_config,
    record_item_attempt,
    record_split_day_outcome,
    record_wide_image,
)
from engine.generation import generate_final_high_res, generate_image
from engine.split import SplitConfig, load_split_config, split_wide_image
from engine.tv_deploy import (
    clear_photos_category,
    configure_batch_rotation,
    upload_image_to_category,
)

_logger = logging.getLogger(__name__)

_ASPECT_RATIO_BY_PANEL = {"43L": "9:16", "43R": "9:16", "50": "16:9"}

# Cuántas llamadas al modelo implica un día según su modo (PRD §15.2
# objetivo 2): 3 paneles independientes, o 1 imagen ancha compartida + el
# panel 50 en modo split. Estructural (deriva de cómo funciona el
# corredor), no una constante de instalación -- por eso vive en código, no
# en config/batch.toml (dev_plan_phase_2.md §2.4).
_MODEL_CALLS_PER_DAY_BY_MODE = {"independiente": 3, "split": 2}


def _generate_with_retries(
    attempt: Callable[[], dict], max_attempts: int
) -> tuple[int, dict]:
    """Runs the item-level retry loop shared by draft generation and 4K
    finalization: calls `attempt` up to `max_attempts` times, stopping
    immediately on a `policy_rejection` (requisito duro #1), and returns
    the attempt count actually spent plus the last result dict.
    """
    attempts = 0
    while True:
        attempts += 1
        result = attempt()
        if "image_id" in result:
            return attempts, result
        if result.get("policy_rejection") or attempts >= max_attempts:
            return attempts, result


def _draft_item(
    batch_id: str,
    item: BatchItemRecord,
    direction: ArtDirection,
    max_attempts: int,
    path: Path | None,
) -> str:
    """Drafts a single independent panel (43L/43R/50, or the 50 of a split
    day). Returns 'drafted', 'needs_attention', or 'skipped'.
    """
    if item.stage != "pending":
        return "skipped"

    aspect_ratio = _ASPECT_RATIO_BY_PANEL[item.panel]
    prompt = build_prompt(item.prompt, direction)
    attempts, result = _generate_with_retries(
        lambda: generate_image(prompt, aspect_ratio), max_attempts
    )

    if "image_id" in result:
        record_item_attempt(
            batch_id,
            item.day_index,
            item.panel,
            attempts=attempts,
            stage="drafted",
            image_id=result["image_id"],
            error=None,
            path=path,
        )
        return "drafted"

    record_item_attempt(
        batch_id,
        item.day_index,
        item.panel,
        attempts=attempts,
        stage="needs_attention",
        image_id=None,
        error=result.get("error"),
        policy_rejection=bool(result.get("policy_rejection")),
        path=path,
    )
    return "needs_attention"


def _draft_split_day(
    batch_id: str,
    day: BatchDayRecord,
    item_43l: BatchItemRecord,
    direction: ArtDirection,
    split_config: SplitConfig,
    max_attempts: int,
    path: Path | None,
) -> str:
    """Drafts the shared wide image of a split day once, writing the same
    outcome in lockstep to the 43L/43R `batch_item` rows and to
    `batch_day.wide_image_id`/`wide_stage`. Returns 'drafted',
    'needs_attention', or 'skipped'.
    """
    if day.wide_stage != "pending":
        return "skipped"

    prompt = build_prompt(item_43l.prompt, direction)
    aspect_ratio = split_config.wide_aspect_ratio
    attempts, result = _generate_with_retries(
        lambda: generate_image(prompt, aspect_ratio), max_attempts
    )

    if "image_id" in result:
        record_split_day_outcome(
            batch_id,
            day.day_index,
            panel_43l=PanelOutcome(attempts=attempts, stage="drafted"),
            panel_43r=PanelOutcome(attempts=attempts, stage="drafted"),
            wide=WideOutcome(wide_image_id=result["image_id"], wide_stage="drafted"),
            path=path,
        )
        return "drafted"

    panel_error = result.get("error")
    panel_policy_rejection = bool(result.get("policy_rejection"))
    record_split_day_outcome(
        batch_id,
        day.day_index,
        panel_43l=PanelOutcome(
            attempts=attempts,
            stage="needs_attention",
            error=panel_error,
            policy_rejection=panel_policy_rejection,
        ),
        panel_43r=PanelOutcome(
            attempts=attempts,
            stage="needs_attention",
            error=panel_error,
            policy_rejection=panel_policy_rejection,
        ),
        wide=WideOutcome(wide_image_id=None, wide_stage="needs_attention"),
        path=path,
    )
    return "needs_attention"


def run_draft_stage(batch_id: str, path: Path | None = None) -> dict:
    """Processes every `batch_item`/`batch_day` of `batch_id` still in
    `stage='pending'`/`wide_stage='pending'`, generating 1K drafts and
    advancing to `drafted` or `needs_attention`. Never stops on a failed
    item -- each per-item/per-day worker above is total (never raises) and
    always records its own outcome before moving to the next one. Safe to
    re-invoke: items already past `pending` are skipped (§2.5 groundwork).
    """
    direction = load_art_direction()
    split_config = load_split_config()
    max_attempts = load_batch_config().generation_max_attempts

    days = {day.day_index: day for day in get_batch_days(batch_id, path=path)}
    items_by_day: dict[int, dict[str, BatchItemRecord]] = {}
    for item in get_batch_items(batch_id, path=path):
        items_by_day.setdefault(item.day_index, {})[item.panel] = item

    summary: dict[str, list[str]] = {
        "drafted": [],
        "needs_attention": [],
        "skipped": [],
    }

    for day_index, day in sorted(days.items()):
        panels = items_by_day.get(day_index, {})
        if day.mode == "split":
            outcome = _draft_split_day(
                batch_id,
                day,
                panels["43L"],
                direction,
                split_config,
                max_attempts,
                path,
            )
            summary[outcome].append(f"{day_index}:wide")
            fifty_outcome = _draft_item(
                batch_id, panels["50"], direction, max_attempts, path
            )
            summary[fifty_outcome].append(f"{day_index}:50")
        else:
            for panel in ("43L", "43R", "50"):
                outcome = _draft_item(
                    batch_id, panels[panel], direction, max_attempts, path
                )
                summary[outcome].append(f"{day_index}:{panel}")

    _logger.info(
        "run_draft_stage: batch_id=%s drafted=%d needs_attention=%d skipped=%d",
        batch_id,
        len(summary["drafted"]),
        len(summary["needs_attention"]),
        len(summary["skipped"]),
    )
    return summary


def _finalize_item(
    batch_id: str,
    item: BatchItemRecord,
    max_attempts: int,
    path: Path | None,
) -> str:
    """Finalizes a single independent panel (43L/43R/50 of an independiente
    day, or the 50 of a split day) from its already-drafted `image_id` to a
    4K version. Returns 'finalized', 'needs_attention', or 'skipped'.
    """
    if item.stage != "drafted":
        return "skipped"

    image_id = item.image_id
    if image_id is None:
        raise ValueError(f"batch_item en stage='drafted' sin image_id: {item!r}")
    attempts, result = _generate_with_retries(
        lambda: generate_final_high_res(image_id), max_attempts
    )

    if "image_id" in result:
        record_item_attempt(
            batch_id,
            item.day_index,
            item.panel,
            attempts=attempts,
            stage="finalized",
            image_id=result["image_id"],
            error=None,
            path=path,
        )
        return "finalized"

    record_item_attempt(
        batch_id,
        item.day_index,
        item.panel,
        attempts=attempts,
        stage="needs_attention",
        image_id=item.image_id,
        error=result.get("error"),
        policy_rejection=bool(result.get("policy_rejection")),
        path=path,
    )
    return "needs_attention"


def _finalize_split_day(
    batch_id: str,
    day: BatchDayRecord,
    item_43l: BatchItemRecord,
    split_config: SplitConfig,
    max_attempts: int,
    path: Path | None,
) -> str:
    """Finalizes the shared wide image of a split day once (unless it was
    already finalized in a prior invocation, requisito duro #4), then splits
    it into the 43L/43R `batch_item` rows. A failed split is retried on
    re-invocation without ever re-finalizing the wide source. Returns
    'finalized', 'needs_attention', or 'skipped'.
    """
    if day.wide_stage == "finalized" and item_43l.stage == "finalized":
        return "skipped"
    if day.wide_stage not in ("drafted", "finalized"):
        return "skipped"

    wide_image_id = day.wide_image_id
    if day.wide_stage != "finalized":
        if wide_image_id is None:
            raise ValueError(
                f"batch_day en wide_stage='drafted' sin wide_image_id: {day!r}"
            )
        drafted_wide_image_id = wide_image_id
        attempts, result = _generate_with_retries(
            lambda: generate_final_high_res(drafted_wide_image_id), max_attempts
        )

        if "image_id" not in result:
            finalize_error = result.get("error")
            finalize_policy_rejection = bool(result.get("policy_rejection"))
            record_split_day_outcome(
                batch_id,
                day.day_index,
                panel_43l=PanelOutcome(
                    attempts=attempts,
                    stage="needs_attention",
                    error=finalize_error,
                    policy_rejection=finalize_policy_rejection,
                ),
                panel_43r=PanelOutcome(
                    attempts=attempts,
                    stage="needs_attention",
                    error=finalize_error,
                    policy_rejection=finalize_policy_rejection,
                ),
                wide=WideOutcome(
                    wide_image_id=day.wide_image_id, wide_stage="needs_attention"
                ),
                path=path,
            )
            return "needs_attention"

        wide_image_id = result["image_id"]
        record_wide_image(
            batch_id,
            day.day_index,
            wide_image_id=wide_image_id,
            wide_stage="finalized",
            path=path,
        )
    else:
        attempts = item_43l.attempts

    if wide_image_id is None:
        raise ValueError(
            f"batch_day en wide_stage='finalized' sin wide_image_id: {day!r}"
        )
    split_result = split_wide_image(wide_image_id, split_config.gap_fraction)

    if "error" in split_result:
        record_split_day_outcome(
            batch_id,
            day.day_index,
            panel_43l=PanelOutcome(
                attempts=attempts,
                stage="needs_attention",
                error=split_result["error"],
            ),
            panel_43r=PanelOutcome(
                attempts=attempts,
                stage="needs_attention",
                error=split_result["error"],
            ),
            path=path,
        )
        return "needs_attention"

    record_split_day_outcome(
        batch_id,
        day.day_index,
        panel_43l=PanelOutcome(
            attempts=attempts,
            stage="finalized",
            image_id=split_result["left"]["image_id"],
        ),
        panel_43r=PanelOutcome(
            attempts=attempts,
            stage="finalized",
            image_id=split_result["right"]["image_id"],
        ),
        path=path,
    )
    return "finalized"


def run_finalize_stage(batch_id: str, path: Path | None = None) -> dict:
    """Processes every `batch_item`/`batch_day` of `batch_id` still in
    `stage='drafted'`, re-generating each in 4K via
    `generation.generate_final_high_res` and advancing to `finalized` or
    `needs_attention`. For split days, the shared wide image is finalized
    once and then split into 43L/43R (requisito duro #4) -- never re-stops
    the rest of the batch on a failed item, and is safe to re-invoke: items
    already finalized are skipped, and a day whose wide source already
    finalized but whose split failed only retries the split.
    """
    split_config = load_split_config()
    max_attempts = load_batch_config().generation_max_attempts

    days = {day.day_index: day for day in get_batch_days(batch_id, path=path)}
    items_by_day: dict[int, dict[str, BatchItemRecord]] = {}
    for item in get_batch_items(batch_id, path=path):
        items_by_day.setdefault(item.day_index, {})[item.panel] = item

    summary: dict[str, list[str]] = {
        "finalized": [],
        "needs_attention": [],
        "skipped": [],
    }

    for day_index, day in sorted(days.items()):
        panels = items_by_day.get(day_index, {})
        if day.mode == "split":
            outcome = _finalize_split_day(
                batch_id, day, panels["43L"], split_config, max_attempts, path
            )
            summary[outcome].append(f"{day_index}:wide")
            fifty_outcome = _finalize_item(batch_id, panels["50"], max_attempts, path)
            summary[fifty_outcome].append(f"{day_index}:50")
        else:
            for panel in ("43L", "43R", "50"):
                outcome = _finalize_item(batch_id, panels[panel], max_attempts, path)
                summary[outcome].append(f"{day_index}:{panel}")

    _logger.info(
        "run_finalize_stage: batch_id=%s finalized=%d needs_attention=%d skipped=%d",
        batch_id,
        len(summary["finalized"]),
        len(summary["needs_attention"]),
        len(summary["skipped"]),
    )
    return summary


def _upload_with_retries(
    attempt: Callable[[], dict], max_attempts: int
) -> tuple[int, dict]:
    """Retry loop de la etapa de subida a TV (§4.1) -- variante mínima de
    `_generate_with_retries`, no reutilizable tal cual: detecta éxito por
    `'content_id' in result` (el shape de `tv_deploy.upload_image_to_category`),
    no por `'image_id'`, y no existe concepto de `policy_rejection` en
    subida -- toda falla es reintentable hasta `max_attempts`, a
    diferencia del corte inmediato que sí aplica a generación.
    """
    attempts = 0
    while True:
        attempts += 1
        result = attempt()
        if "content_id" in result or attempts >= max_attempts:
            return attempts, result


def _upload_item(
    batch_id: str,
    item: BatchItemRecord,
    max_attempts: int,
    path: Path | None,
) -> str:
    """Sube un único panel físico (43L/43R/50, de un día independiente o
    split -- indistinguible aquí, ver nota de módulo) ya en
    stage='finalized' a la TV cuyo nombre coincide con `item.panel`.
    Devuelve 'uploaded', 'needs_attention', o 'skipped' (todavía no
    finalizado, o ya subido -- requisito duro #9).
    """
    if item.stage != "finalized":
        return "skipped"

    image_id = item.image_id
    if image_id is None:
        raise ValueError(f"batch_item en stage='finalized' sin image_id: {item!r}")

    attempts, result = _upload_with_retries(
        lambda: upload_image_to_category(item.panel, image_id), max_attempts
    )

    if "content_id" in result:
        record_item_attempt(
            batch_id,
            item.day_index,
            item.panel,
            attempts=attempts,
            stage="uploaded",
            image_id=image_id,
            error=None,
            path=path,
        )
        return "uploaded"

    record_item_attempt(
        batch_id,
        item.day_index,
        item.panel,
        attempts=attempts,
        stage="needs_attention",
        image_id=image_id,  # la imagen finalizada sigue válida en disco
        error=result.get("error"),
        path=path,
    )
    return "needs_attention"


def run_upload_stage(batch_id: str, path: Path | None = None) -> dict:
    """Sube cada `batch_item` de `batch_id` en `stage='finalized'` a la TV
    correspondiente a su panel (dev_plan_phase_2.md §4.1), avanzando a
    `uploaded` o `needs_attention`. Seguro de re-invocar: items ya
    `uploaded` se saltan (requisito duro #9); items que aún no llegaron a
    `finalized` también se saltan (todavía no hay nada que subir).

    Concurrencia (ver nota de módulo): un worker por TV física (3 en
    total), cada uno drenando su propia cola de items de esa TV en orden
    secuencial de `day_index` -- nunca fully-secuencial entre TVs (tres
    dispositivos físicos independientes sin cuota compartida) ni
    fully-paralelo dentro de una misma TV (rompería la asunción de una
    sola conexión websocket por TV).

    Reintento con `tv_deploy_max_attempts` (config/batch.toml, §15.5) vía
    `_upload_with_retries` -- sin concepto de `policy_rejection`.

    Días split: sin manejo especial -- para cuando un item llega a
    `stage='finalized'`, un día split ya dejó dos filas físicas 43L/43R
    independientes (el split ya ocurrió en 2.3); este corredor itera
    `batch_item` sin mirar `batch_day.mode` en absoluto.

    Vaciado previo por TV ("clean slate per batch", dev_plan_phase_2.md
    §4.2): antes de subir el primer item de este lote a una TV, si
    NINGÚN item de `batch_id` para ese panel está ya en `stage='uploaded'`,
    se asume que nada de este lote se subió todavía a esa TV y se vacía
    'Mis Fotos' primero (`tv_deploy.clear_photos_category`) -- así la
    rotación nativa nunca mezcla el lote vigente con uno anterior. Si al
    menos un item ya está `uploaded` (reinvocación tras un crash a medio
    subir), se salta el vaciado -- ya ocurrió en una corrida anterior, y
    repetirlo borraría imágenes de este mismo lote que ya subieron con
    éxito. Un vaciado fallido (TV inalcanzable) se loguea como warning y
    la subida real continúa igual -- es limpieza best-effort, nunca debe
    bloquear la subida.
    """
    max_attempts = load_batch_config().tv_deploy_max_attempts

    items_by_panel: dict[str, list[BatchItemRecord]] = {"43L": [], "43R": [], "50": []}
    for item in get_batch_items(batch_id, path=path):
        items_by_panel[item.panel].append(item)

    summary: dict[str, list[str]] = {
        "uploaded": [],
        "needs_attention": [],
        "skipped": [],
    }

    def _drain_panel(panel: str) -> list[tuple[str, str]]:
        items = items_by_panel[panel]
        if not any(item.stage == "uploaded" for item in items):
            clear_result = clear_photos_category(panel)
            if "error" in clear_result:
                _logger.warning(
                    "No se pudo vaciar 'Mis Fotos' en %s antes de subir el "
                    "lote %s: %s",
                    panel,
                    batch_id,
                    clear_result["error"],
                )
        return [
            (
                _upload_item(batch_id, item, max_attempts, path),
                f"{item.day_index}:{panel}",
            )
            for item in items
        ]

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=len(items_by_panel)
    ) as executor:
        futures = [executor.submit(_drain_panel, panel) for panel in items_by_panel]
        for future in futures:
            for outcome, label in future.result():
                summary[outcome].append(label)

    _logger.info(
        "run_upload_stage: batch_id=%s uploaded=%d needs_attention=%d skipped=%d",
        batch_id,
        len(summary["uploaded"]),
        len(summary["needs_attention"]),
        len(summary["skipped"]),
    )
    return summary


def _configure_rotation_with_retries(panel: str, max_attempts: int) -> dict:
    attempts = 0
    config = load_batch_config()
    while True:
        attempts += 1
        result = configure_batch_rotation(
            panel, config.rotation_duration_minutes, config.rotation_shuffle
        )
        if "result" in result or attempts >= max_attempts:
            return result


def run_rotation_stage(batch_id: str, path: Path | None = None) -> dict:
    """Configura la rotación nativa de 'Mis Fotos' en las tres TVs, una
    sola vez, al terminar de subir un lote (dev_plan_phase_2.md §4.2, PRD
    §15.2 objetivo 7). Duración/orden fijos de `config/batch.toml`
    (`rotation_duration_minutes`/`rotation_shuffle`) -- alcance reducido
    decidido con el usuario: sin variación por calendario, el usuario
    ajusta a mano si quiere otra cadencia antes del próximo lote.

    `batch_id` no se usa para leer `batch_item` -- la rotación se
    configura sobre la categoría completa de cada TV, no por item -- solo
    para el log; `path` se acepta (sin uso) para que la firma sea
    simétrica con `run_draft_stage`/`run_finalize_stage`/`run_upload_stage`
    y encaje en `_run_batch_engine_in_background` sin un caso especial.

    Concurrencia: un worker por TV física (3 en total, mismo principio
    que `deploy_set_to_panels`/`run_upload_stage` -- dispositivos físicos
    independientes, se intentan siempre las tres). Reintento con
    `tv_deploy_max_attempts` (config/batch.toml) vía
    `_configure_rotation_with_retries`. Idempotente por naturaleza:
    volver a llamarla (p. ej. en una reconciliación de reinicio) solo
    reaplica la misma configuración. Nunca lanza; la falla de una TV
    nunca impide que las otras dos se configuren.

    Devuelve {'43L': {...}, '43R': {...}, '50': {...}}, cada valor el
    resultado final de `configure_batch_rotation` para esa TV.
    """
    del path  # ver docstring: la rotación es por categoría, no por item
    max_attempts = load_batch_config().tv_deploy_max_attempts
    panels = ("43L", "43R", "50")

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(panels)) as executor:
        futures = {
            panel: executor.submit(
                _configure_rotation_with_retries, panel, max_attempts
            )
            for panel in panels
        }
        results = {panel: future.result() for panel, future in futures.items()}

    _logger.info(
        "run_rotation_stage: batch_id=%s resultados=%s",
        batch_id,
        {panel: ("ok" if "result" in r else "error") for panel, r in results.items()},
    )
    return results


def estimate_batch_duration(day_modes: list[str]) -> dict:
    """Estimado determinístico de duración (PRD §15.2 objetivo 4, §15.3
    paso 7, dev_plan_phase_2.md §2.4) -- no es juicio de LLM, es aritmética
    sobre el conteo de llamadas al modelo que implica la mezcla real de
    modos de un lote, más el despliegue a TV (PRD §15.2 objetivo 4 pide
    explícitamente "generación final 4K + despliegue", no solo
    generación). Corre ANTES de materializar el lote (recibe los modos ya
    decididos en el paso 4/5 de la skill, no un `batch_id`) para no
    introducir un segundo checkpoint de aprobación entre "prompts
    aprobados" y "confirmar el lote" -- desviación deliberada de una
    lectura literal de este documento, documentada en el cierre de 2.4.

    El término de despliegue (`deploy_seconds_per_day`) es un PLACEHOLDER
    sin medición real: el corredor de subida por lote no existe todavía
    (Etapa 4/iteración 4.1), así que no hay datos reales de cuánto tarda
    subir una imagen 4K por red a una Frame TV. Escala por día, no por
    panel, asumiendo que las tres TVs de un día se despliegan en paralelo
    entre sí (mismo patrón que `engine.tv_deploy.deploy_set_to_panels`) --
    revisar con datos reales en 4.1.

    Aplica `eta_safety_margin` de `config/batch.toml` sobre el costo base
    (draft + finalización + despliegue) y redondea SIEMPRE hacia arriba
    (`math.ceil`, nunca `round`) -- decisión explícita del usuario: nunca
    subestimar, porque un estimado corto hace pensar que el lote se
    congeló cuando en realidad sigue corriendo. `finalize_seconds_per_call`
    ya viene calibrado por encima del peor caso observado hasta ahora
    (batch_86bd3e0f, dev_plan_phase_2.md §2.4), no solo por el promedio --
    el margen es una segunda capa de seguridad, no la única.

    `day_modes` ya viene validado por la tool de agent.py (mismo patrón
    que `materialize_batch_gallery`/`batch_store.materialize_batch`): una
    lista no vacía de `'independiente'`/`'split'`, un valor por día.
    """
    config = load_batch_config()
    independent_days = day_modes.count("independiente")
    split_days = day_modes.count("split")
    total_model_calls = (
        independent_days * _MODEL_CALLS_PER_DAY_BY_MODE["independiente"]
        + split_days * _MODEL_CALLS_PER_DAY_BY_MODE["split"]
    )
    generation_seconds = total_model_calls * (
        config.draft_seconds_per_call + config.finalize_seconds_per_call
    )
    deploy_seconds = len(day_modes) * config.deploy_seconds_per_day
    estimated_seconds = (generation_seconds + deploy_seconds) * config.eta_safety_margin
    return {
        "day_count": len(day_modes),
        "independent_days": independent_days,
        "split_days": split_days,
        "total_model_calls": total_model_calls,
        "estimated_seconds": estimated_seconds,
        "estimated_minutes": math.ceil(estimated_seconds / 60),
    }


def summarize_batch(batch_id: str, path: Path | None = None) -> dict:
    """Resumen de "lo que se logró" de un lote (PRD §15.3 paso 9,
    dev_plan_phase_2.md §2.4), reutilizable por el reporte proactivo de
    Telegram (Etapa 3): cuenta `batch_item` por `stage` final y separa
    `needs_attention` por `policy_rejection` vs. falla técnica agotada --
    nunca infiere esa distinción del texto de `error`, lee la columna
    persistida tal cual (§2.4, `batch_store.record_item_attempt`).
    """
    batch_record = get_batch(batch_id, path=path)
    if batch_record is None:
        return {"error": f"No existe un lote con batch_id={batch_id!r}."}

    days = {day.day_index: day for day in get_batch_days(batch_id, path=path)}
    items = get_batch_items(batch_id, path=path)

    stage_counts: dict[str, int] = {}
    needs_attention_policy_rejection: list[dict] = []
    needs_attention_technical: list[dict] = []
    for item in items:
        stage_counts[item.stage] = stage_counts.get(item.stage, 0) + 1
        if item.stage != "needs_attention":
            continue
        entry = {"day_index": item.day_index, "panel": item.panel, "error": item.error}
        if item.policy_rejection:
            needs_attention_policy_rejection.append(entry)
        else:
            needs_attention_technical.append({**entry, "attempts": item.attempts})

    items_by_day: dict[int, dict[str, BatchItemRecord]] = {}
    for item in items:
        items_by_day.setdefault(item.day_index, {})[item.panel] = item

    day_summaries = []
    for day_index, day in sorted(days.items()):
        panels = {
            panel: {
                "stage": item.stage,
                "image_id": item.image_id,
                "error": item.error,
            }
            for panel, item in items_by_day.get(day_index, {}).items()
        }
        day_summaries.append(
            {
                "day_index": day_index,
                "mode": day.mode,
                "sub_group": day.sub_group,
                "panels": panels,
            }
        )

    return {
        "batch_id": batch_record.batch_id,
        "theme": batch_record.theme,
        "day_count": batch_record.day_count,
        "stage_counts": stage_counts,
        "needs_attention_policy_rejection": needs_attention_policy_rejection,
        "needs_attention_technical": needs_attention_technical,
        "days": day_summaries,
    }
