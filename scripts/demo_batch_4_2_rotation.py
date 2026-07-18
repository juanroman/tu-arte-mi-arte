"""Demo manual de la iteración 4.2 (rotación nativa de la TV, NO es un
test de pytest): materializa un lote pequeño (4 días, ~70/30 independiente/
split) y corre `engine.batch.run_upload_stage` seguido de
`engine.batch.run_rotation_stage` contra las TRES TVS FÍSICAS REALES de la
casa (dev_plan_phase_2.md §4.2).

Por qué imágenes sintéticas, no generación real de Gemini: mismo criterio
que demo_batch_4_1_upload.py -- el riesgo de esta iteración es de
CONFIGURACIÓN de la TV (vaciar 'Mis Fotos' antes de subir, dejarla rotando
con la duración/orden correctos), no de contenido. Cada panel se genera
con Pillow: un rectángulo de color sólido + el texto "DEMO 4.2" quemado
encima, en la resolución real de cada panel (mismas medidas que 4.1).

Punto central de esta demo: al momento de escribir esto, las tres TVs
todavía tienen las ~15 imágenes "DEMO 4.1" subidas por la demo de la
iteración anterior -- un "lote viejo" real, no simulado, perfecto para
probar el vaciado ("clean slate per batch", §4.2) en condiciones reales.
Si esta demo corre limpiamente, "Mis Fotos" de cada TV debe terminar SOLO
con las imágenes "DEMO 4.2" (4 días = hasta 12 paneles, según cuántos días
caigan en split), sin ningún rastro de "DEMO 4.1".

A diferencia de 4.1, esta demo SÍ deja las TVs mostrando/rotando contenido
nuevo -- run_rotation_stage llama set_slideshow_status (o su fallback
legacy set_auto_rotation_status) con rotation_duration_minutes/
rotation_shuffle de config/batch.toml (1440 min = 1 día por imagen, sin
shuffle). No se puede observar la rotación avanzar en una sola sesión
(1440 minutos), pero sí se puede confirmar que la TV la aceptó, leyendo
get_slideshow_status/get_auto_rotation_status después de configurarla.

Requiere que las tres TVs (43L/43R/50) estén encendidas y en la red de la
casa. Correr manualmente con:
    uv run python scripts/demo_batch_4_2_rotation.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from PIL import Image, ImageDraw  # noqa: E402
from samsungtvws import exceptions  # noqa: E402
from samsungtvws.art import SamsungTVArt  # noqa: E402

from engine import batch, batch_store, tv_deploy  # noqa: E402
from engine.batch_store import ApprovedDay, PanelOutcome, WideOutcome  # noqa: E402
from engine.generation import IMAGES_DIR, _new_image_id  # noqa: E402
from engine.tv_discovery import resolve_tv_host  # noqa: E402

THEME = "Demo 4.2: rotación nativa a escala real (4 días)"
DAY_COUNT = 4

_PANEL_SIZE_PX = {
    "43L": (2250, 3712),
    "43R": (2250, 3712),
    "50": (5504, 3072),
    "wide-fuente": (4608, 3712),
}

_DAY_COLORS = [
    (61, 133, 198),
    (106, 168, 79),
    (204, 65, 37),
    (142, 105, 199),
]  # 4 colores distinguibles, uno por día


def _synthetic_panel_image(day_index: int, panel: str) -> str:
    width, height = _PANEL_SIZE_PX[panel]
    color = _DAY_COLORS[(day_index - 1) % len(_DAY_COLORS)]
    image = Image.new("RGB", (width, height), color=color)
    draw = ImageDraw.Draw(image)
    label = f"DEMO 4.2\nDía {day_index}\nPanel {panel}"
    draw.text((width // 10, height // 10), label, fill=(255, 255, 255))

    image_id = _new_image_id()
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    image.save(IMAGES_DIR / f"{image_id}.jpg", format="JPEG")
    return image_id


def _build_days() -> list[ApprovedDay]:
    days = []
    for day_index in range(1, DAY_COUNT + 1):
        if day_index == 3:
            days.append(
                ApprovedDay(
                    day_index=day_index,
                    mode="split",
                    sub_group="Sub-grupo 1",
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
                    sub_group="Sub-grupo 1",
                    prompts={
                        "43L": f"escena 43L día {day_index}",
                        "43R": f"escena 43R día {day_index}",
                        "50": f"escena 50 día {day_index}",
                    },
                )
            )
    return days


def _materialize_and_finalize_synthetically(theme: str, days: list[ApprovedDay]) -> str:
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


def _read_slideshow_status(tv_name: str) -> object:
    """Lee el estado de rotación real de la TV tras configurarla -- mismo
    patrón de fallback (API nueva, luego legacy) que
    tv_deploy.configure_batch_rotation, ya que get_slideshow_status()
    también puede no estar soportado en TVs con protocolo más viejo.
    """
    host = resolve_tv_host(tv_name)
    token_file = tv_deploy.DATA_DIR / f"tv_{tv_name.lower()}_token.json"
    tv = SamsungTVArt(host=host, token_file=str(token_file), timeout=15)
    try:
        tv.open()
        try:
            return tv.get_slideshow_status()
        except exceptions.ResponseError:
            return tv.get_auto_rotation_status()
    finally:
        tv.close()


def _list_my_photos_content_ids(tv_name: str) -> list[str]:
    host = resolve_tv_host(tv_name)
    token_file = tv_deploy.DATA_DIR / f"tv_{tv_name.lower()}_token.json"
    tv = SamsungTVArt(host=host, token_file=str(token_file), timeout=15)
    try:
        tv.open()
        return [
            item["content_id"]
            for item in tv.available(category=tv_deploy._MY_PHOTOS_CATEGORY)
        ]
    finally:
        tv.close()


def main() -> None:
    print(
        "Antes de subir, contenido actual de 'Mis Fotos' por TV (debería "
        "incluir las ~15 imágenes 'DEMO 4.1' de la demo anterior, si no se "
        "han borrado a mano):"
    )
    for panel in ("43L", "43R", "50"):
        try:
            content_ids = _list_my_photos_content_ids(panel)
            print(f"  TV {panel}: {len(content_ids)} imágenes en 'Mis Fotos'")
        except Exception as error:  # solo diagnóstico, no bloquea la demo
            print(f"  TV {panel}: no se pudo leer ({error})")

    days = _build_days()
    batch_id = _materialize_and_finalize_synthetically(THEME, days)
    print(f"\nbatch_id={batch_id} day_count={DAY_COUNT}")

    print("\nSubiendo el lote nuevo (debe vaciar 'Mis Fotos' antes)...")
    upload_summary = batch.run_upload_stage(batch_id)
    print(
        f"uploaded={len(upload_summary['uploaded'])} "
        f"needs_attention={upload_summary['needs_attention']}"
    )

    print("\nContenido de 'Mis Fotos' tras la subida (solo debe quedar DEMO 4.2):")
    for panel in ("43L", "43R", "50"):
        content_ids = _list_my_photos_content_ids(panel)
        print(f"  TV {panel}: {len(content_ids)} imágenes en 'Mis Fotos'")

    print("\nConfigurando rotación nativa en las tres TVs...")
    rotation_result = batch.run_rotation_stage(batch_id)
    for panel, result in rotation_result.items():
        print(f"  TV {panel}: {result}")

    print("\nEstado de rotación reportado por cada TV tras configurarla:")
    for panel in ("43L", "43R", "50"):
        try:
            status = _read_slideshow_status(panel)
            print(f"  TV {panel}: {status}")
        except Exception as error:  # solo diagnóstico, no bloquea la demo
            print(f"  TV {panel}: no se pudo leer ({error})")

    full_summary = batch.summarize_batch(batch_id)
    print(f"\nsummarize_batch: {full_summary}")

    print(
        "\nVerificación manual pendiente: confirma en la app SmartThings o "
        "el control remoto de cada TV que 'Mis Fotos' YA NO tiene imágenes "
        "'DEMO 4.1' (solo 'DEMO 4.2'), y que la pantalla mostró un cambio "
        "de imagen tras esta corrida (run_rotation_stage sí deja la TV "
        "rotando/mostrando el lote nuevo, a diferencia de la subida sola "
        "de 4.1)."
    )


if __name__ == "__main__":
    main()
