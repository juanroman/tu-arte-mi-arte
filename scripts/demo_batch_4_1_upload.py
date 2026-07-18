"""Demo manual de la iteración 4.1 (subida por lote a "Mis Fotos", NO es
un test de pytest): materializa un lote real de 15 días (45 paneles, ~70/30
independiente/split) y corre `engine.batch.run_upload_stage` contra las
TRES TVS FÍSICAS REALES de la casa -- el escenario de escala que motivó
toda esta iteración (dev_plan_phase_2.md §4.1: "podríamos llegar a un
escenario de 15 días, que son 45 imágenes... es la parte más delicada de
todo el plan").

Por qué imágenes sintéticas, no generación real de Gemini: el riesgo de
esta iteración es de TRANSPORTE (subir 45 imágenes a 3 TVs sin bloquearse
entre sí, sin re-subir lo ya subido, sin que una falla bloquee al resto),
no de CONTENIDO -- gastar en 45 llamadas reales a la API para un problema
que no depende del contenido de la imagen sería puro costo sin ninguna
señal adicional. En vez de eso, cada panel se genera con Pillow: un
rectángulo de color sólido distinto por día + el texto "Día N — panel"
quemado encima, en la resolución REAL de cada panel (43L/43R 2250x3712,
50 5504x3072 -- las mismas medidas confirmadas contra hardware real en la
demo de 2.3, PRD/dev_plan_phase_2.md §2.3) para que sean identificables a
simple vista en cada TV. Las filas `batch_item` se escriben directo en
`stage='finalized'` vía `batch_store.record_item_attempt`/
`record_split_day_outcome` (el mismo helper que usa el corredor real),
saltándose las etapas de draft/finalize -- 4.1 no las ejerce, ya las
prueban 2.2/2.3/2.5/2.6.

Falla forzada determinista (mismo mecanismo de asignación directa sobre
el nombre del módulo que ya usan demo_batch_2_6_end_to_end.py y
eval_partial_failure.py, no pytest.monkeypatch que no aplica fuera de un
test): el panel 43R del día 1 falla sus primeros 2 intentos de subida
(error transitorio simulado) y sube al tercero -- demuestra en una
corrida real "tolera una falla sin bloquear a los demás" (requisito duro
#2) + "reintenta hasta tv_deploy_max_attempts veces antes de
needs_attention" (requisito duro #3). El panel 43R del día 2 falla
SIEMPRE -- demuestra el camino a needs_attention tras agotar reintentos,
sin bloquear el resto del lote.

ADVERTENCIA (costo real, sin marcha atrás automática): esta demo SÍ
escribe contenido real a las tres TVs -- al terminar, "Mis Fotos" de cada
pantalla tendrá ~15 imágenes sintéticas nuevas (identificables por el
texto "DEMO 4.1" quemado en cada una) que quedarán ahí hasta que 4.2
configure la rotación nativa o alguien las borre a mano (p. ej. desde la
app SmartThings). La pantalla actualmente mostrada NO debe cambiar --
upload_image_to_category nunca selecciona ni muestra nada (esa es la
decisión central de esta iteración, ver docstring de
engine.tv_deploy.upload_image_to_category).

Requiere que las tres TVs (43L/43R/50) estén encendidas y en la red de
la casa (mismo requisito que cualquier demo previa de despliegue, p. ej.
scripts/spike_tv_write_path.py). Tarda unos minutos (45 uploads reales
por red, aunque las tres TVs corren en paralelo entre sí) -- correr
manualmente con `uv run python scripts/demo_batch_4_1_upload.py`.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from PIL import Image, ImageDraw  # noqa: E402

from engine import batch, batch_store  # noqa: E402
from engine.batch_store import ApprovedDay, PanelOutcome, WideOutcome  # noqa: E402
from engine.generation import IMAGES_DIR, _new_image_id  # noqa: E402

THEME = "Demo 4.1: subida por lote a escala real (15 días)"
DAY_COUNT = 15

# Resoluciones reales confirmadas contra hardware en la demo de 2.3
# (dev_plan_phase_2.md §2.3): panel 50 en 16:9 4K, 43L/43R en el recorte
# 4K real de la fuente ancha. "wide-fuente" no es un panel físico
# desplegable (nunca lo sube run_upload_stage, que solo itera batch_item)
# -- se genera igual, en la resolución 5:4 real de la fuente ancha
# (config/split.toml), solo para que batch_day.wide_image_id apunte a un
# archivo real en disco, consistente con lo que deja el corredor real.
_PANEL_SIZE_PX = {
    "43L": (2250, 3712),
    "43R": (2250, 3712),
    "50": (5504, 3072),
    "wide-fuente": (4608, 3712),
}

_DAY_COLORS = [
    (196, 90, 74),
    (74, 139, 196),
    (90, 168, 105),
    (214, 164, 61),
    (140, 94, 168),
    (196, 122, 74),
    (74, 168, 158),
    (168, 74, 122),
    (110, 140, 74),
    (74, 94, 168),
    (196, 74, 105),
    (105, 168, 74),
    (74, 122, 168),
    (168, 122, 74),
    (122, 74, 168),
]  # 15 colores distinguibles, uno por día

# Marcadores para la falla forzada determinista de la subida (día/panel).
_TRANSIENT_FAILURE_TARGET = (1, "43R")  # falla 2 veces, sube en el 3er intento
_EXHAUSTED_FAILURE_TARGET = (2, "43R")  # falla siempre -> needs_attention


def _synthetic_panel_image(day_index: int, panel: str) -> str:
    """Genera y guarda una imagen sintética distinguible por día/panel en
    la resolución real de ese panel -- ver docstring de módulo sobre por
    qué no se gasta en generación real de Gemini para esta demo.
    """
    width, height = _PANEL_SIZE_PX[panel]
    color = _DAY_COLORS[(day_index - 1) % len(_DAY_COLORS)]
    image = Image.new("RGB", (width, height), color=color)
    draw = ImageDraw.Draw(image)
    label = f"DEMO 4.1\nDía {day_index}\nPanel {panel}"
    draw.text((width // 10, height // 10), label, fill=(255, 255, 255))

    image_id = _new_image_id()
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    image.save(IMAGES_DIR / f"{image_id}.jpg", format="JPEG")
    return image_id


def _build_days() -> list[ApprovedDay]:
    """15 días ~70/30 independiente/split (mismo patrón que la demo de
    2.3), solo con prompts placeholder -- esta demo nunca genera contra
    Gemini, así que el texto del prompt es irrelevante.
    """
    days = []
    for day_index in range(1, DAY_COUNT + 1):
        sub_group = f"Sub-grupo {(day_index - 1) // 5 + 1}"
        # ~30% split (días 3/6/9/12/15 = 5 de 15) -- días 1/2 (los targets
        # de falla forzada más abajo) siempre caen en independiente.
        if day_index % 3 == 0:
            days.append(
                ApprovedDay(
                    day_index=day_index,
                    mode="split",
                    sub_group=sub_group,
                    prompts={
                        "wide": f"escena ancha día {day_index}",
                        "50": f"escena 50 día {day_index}",
                    },
                )
            )
        else:
            days.append(
                ApprovedDay(
                    day_index=day_index,
                    mode="independiente",
                    sub_group=sub_group,
                    prompts={
                        "43L": f"escena 43L día {day_index}",
                        "43R": f"escena 43R día {day_index}",
                        "50": f"escena 50 día {day_index}",
                    },
                )
            )
    return days


def _materialize_and_finalize_synthetically(theme: str, days: list[ApprovedDay]) -> str:
    """Materializa el lote y salta directo a stage='finalized' con
    imágenes sintéticas -- 4.1 no ejerce draft/finalize (ya probados en
    2.2/2.3/2.5/2.6), solo la subida.
    """
    batch_id = batch_store.materialize_batch(theme, days)

    for day in days:
        if day.mode == "split":
            wide_image_id = _synthetic_panel_image(day.day_index, "wide-fuente")
            image_43l = _synthetic_panel_image(day.day_index, "43L")
            image_43r = _synthetic_panel_image(day.day_index, "43R")
            batch_store.record_split_day_outcome(
                batch_id,
                day.day_index,
                panel_43l=PanelOutcome(
                    attempts=1, stage="finalized", image_id=image_43l
                ),
                panel_43r=PanelOutcome(
                    attempts=1, stage="finalized", image_id=image_43r
                ),
                wide=WideOutcome(wide_image_id=wide_image_id, wide_stage="finalized"),
            )
            image_50 = _synthetic_panel_image(day.day_index, "50")
            batch_store.record_item_attempt(
                batch_id,
                day.day_index,
                "50",
                attempts=1,
                stage="finalized",
                image_id=image_50,
                error=None,
            )
        else:
            for panel in ("43L", "43R", "50"):
                image_id = _synthetic_panel_image(day.day_index, panel)
                batch_store.record_item_attempt(
                    batch_id,
                    day.day_index,
                    panel,
                    attempts=1,
                    stage="finalized",
                    image_id=image_id,
                    error=None,
                )

    return batch_id


def _print_progress_by_tv(batch_id: str) -> None:
    items = batch_store.get_batch_items(batch_id)
    by_panel: dict[str, list] = {"43L": [], "43R": [], "50": []}
    for item in items:
        by_panel[item.panel].append(item)
    for panel, panel_items in by_panel.items():
        uploaded = sum(1 for i in panel_items if i.stage == "uploaded")
        needs_attention = sum(1 for i in panel_items if i.stage == "needs_attention")
        print(
            f"  TV {panel}: {uploaded}/{len(panel_items)} subidas, "
            f"{needs_attention} en needs_attention"
        )


def main() -> None:
    days = _build_days()
    batch_id = _materialize_and_finalize_synthetically(THEME, days)
    print(f"batch_id={batch_id} day_count={DAY_COUNT} (45 paneles)")

    items = batch_store.get_batch_items(batch_id)
    target_transient_image_id = next(
        item.image_id
        for item in items
        if (item.day_index, item.panel) == _TRANSIENT_FAILURE_TARGET
    )
    target_exhausted_image_id = next(
        item.image_id
        for item in items
        if (item.day_index, item.panel) == _EXHAUSTED_FAILURE_TARGET
    )

    real_upload_image_to_category = batch.upload_image_to_category
    transient_attempts = {"count": 0}

    def wrapper(tv_name: str, image_id: str) -> dict:
        if image_id == target_exhausted_image_id:
            return {"error": "falla de red simulada (DEMO, siempre falla)"}
        if image_id == target_transient_image_id:
            transient_attempts["count"] += 1
            if transient_attempts["count"] < 3:
                return {"error": "falla de red transitoria simulada (DEMO)"}
        return real_upload_image_to_category(tv_name, image_id)

    batch.upload_image_to_category = wrapper

    print(
        f"\nSubiendo 45 imágenes reales a las 3 TVs físicas "
        f"(día {_TRANSIENT_FAILURE_TARGET[0]}/panel {_TRANSIENT_FAILURE_TARGET[1]} "
        f"falla 2 veces antes de subir; día {_EXHAUSTED_FAILURE_TARGET[0]}/panel "
        f"{_EXHAUSTED_FAILURE_TARGET[1]} falla siempre)...\n"
    )
    start = time.monotonic()
    summary = batch.run_upload_stage(batch_id)
    elapsed = time.monotonic() - start
    batch.upload_image_to_category = real_upload_image_to_category

    print(f"\nrun_upload_stage terminó en {elapsed:.1f}s")
    print(
        f"uploaded={len(summary['uploaded'])} "
        f"needs_attention={summary['needs_attention']}"
    )

    print("\nProgreso final por TV:")
    _print_progress_by_tv(batch_id)

    target_item = next(
        item
        for item in batch_store.get_batch_items(batch_id)
        if (item.day_index, item.panel) == _TRANSIENT_FAILURE_TARGET
    )
    if target_item.stage != "uploaded" or target_item.attempts != 3:
        raise AssertionError(
            "la falla transitoria debía resolverse en exactamente 3 intentos, "
            f"quedó en stage={target_item.stage!r} attempts={target_item.attempts}"
        )
    print(
        f"\n[requisito duro #2/#3 confirmado] día {_TRANSIENT_FAILURE_TARGET[0]} "
        f"panel {_TRANSIENT_FAILURE_TARGET[1]}: stage=uploaded tras "
        f"{target_item.attempts} intentos, sin bloquear al resto del lote."
    )

    exhausted_item = next(
        item
        for item in batch_store.get_batch_items(batch_id)
        if (item.day_index, item.panel) == _EXHAUSTED_FAILURE_TARGET
    )
    max_attempts = batch_store.load_batch_config().tv_deploy_max_attempts
    if (
        exhausted_item.stage != "needs_attention"
        or exhausted_item.attempts != max_attempts
    ):
        raise AssertionError(
            "la falla agotada debía terminar en needs_attention tras "
            f"{max_attempts} intentos, quedó en stage={exhausted_item.stage!r} "
            f"attempts={exhausted_item.attempts}"
        )
    print(
        f"[requisito duro #3 confirmado] día {_EXHAUSTED_FAILURE_TARGET[0]} "
        f"panel {_EXHAUSTED_FAILURE_TARGET[1]}: needs_attention tras agotar "
        f"exactamente {max_attempts} intentos configurados."
    )

    print(
        "\nVerificación manual pendiente: confirma en la app SmartThings o el "
        "control remoto de cada TV que 'Mis Fotos' tiene ~15 imágenes nuevas "
        "(identificables por el texto 'DEMO 4.1' quemado en cada una), y que "
        "la imagen ACTUALMENTE MOSTRADA en cada pantalla no cambió -- esta "
        "iteración nunca selecciona ni muestra nada (eso es 4.2)."
    )

    full_summary = batch.summarize_batch(batch_id)
    print(f"\nsummarize_batch: {full_summary}")


if __name__ == "__main__":
    main()
