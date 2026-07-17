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

Procesamiento secuencial, no paralelo: a diferencia de
`tv_deploy.deploy_set_to_panels` (TVs físicas independientes, sin cuota
compartida), la generación de imágenes comparte una sola cuota de la
API de Gemini -- procesar en paralelo arriesgaría ráfagas de rate-limit
sin ganancia real.
"""

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
