"""Demo manual de 2.3 (NO es un test de pytest): materializa un lote real de
14 días (2 semanas) con mezcla ~70/30 independiente/split, y corre el
corredor completo (draft 1K -> finalización 4K) contra la API real de
Gemini, sin monkeypatch. Objetivo: confirmar que el corredor sostiene el
volumen real de una entrega de 2 semanas (42 imágenes finales), no solo el
caso feliz de 2-3 días ya probado en 2.2.

Hits the real API, costs money, tarda varios minutos (procesamiento
secuencial, ver docstring de src/engine/batch.py) — correr manualmente con
`uv run python scripts/demo_batch_2_3_finalize.py`.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from engine import batch, batch_store  # noqa: E402
from engine.batch_store import ApprovedDay  # noqa: E402

THEME = "Alrededor del mundo en 14 días"

# 10 días independiente + 4 días split (~70/30), repartidos en 4 sub-grupos.
DAYS = [
    ApprovedDay(
        day_index=1,
        mode="independiente",
        sub_group="Bosques nórdicos",
        prompts={
            "43L": "un sendero de pinos cubierto de niebla al amanecer",
            "43R": "musgo y liquen sobre una roca húmeda, macro",
            "50": "un lago escandinavo en calma reflejando montañas lejanas",
        },
    ),
    ApprovedDay(
        day_index=2,
        mode="split",
        sub_group="Bosques nórdicos",
        prompts={
            "wide": "una cabaña de madera aislada entre abetos nevados, panorámica",
            "50": "aurora boreal verde sobre un valle nórdico",
        },
    ),
    ApprovedDay(
        day_index=3,
        mode="independiente",
        sub_group="Bosques nórdicos",
        prompts={
            "43L": "una cascada estrecha cayendo entre rocas oscuras",
            "43R": "corteza de abedul blanco en primer plano",
            "50": "un fiordo noruego visto desde lo alto de un acantilado",
        },
    ),
    ApprovedDay(
        day_index=4,
        mode="independiente",
        sub_group="Desiertos cálidos",
        prompts={
            "43L": "dunas de arena con sombras alargadas al atardecer",
            "43R": "una planta de cactus solitaria en primer plano",
            "50": "una caravana de camellos cruzando el desierto al horizonte",
        },
    ),
    ApprovedDay(
        day_index=5,
        mode="split",
        sub_group="Desiertos cálidos",
        prompts={
            "wide": "un oasis rodeado de palmeras entre dunas doradas, panorámica",
            "50": "estrellas y vía láctea sobre un desierto nocturno",
        },
    ),
    ApprovedDay(
        day_index=6,
        mode="independiente",
        sub_group="Desiertos cálidos",
        prompts={
            "43L": "textura de arena ondulada por el viento, macro",
            "43R": "un pueblo de adobe entre montañas rocosas",
            "50": "un cañón rojo iluminado por el sol de mediodía",
        },
    ),
    ApprovedDay(
        day_index=7,
        mode="independiente",
        sub_group="Desiertos cálidos",
        prompts={
            "43L": "ruinas antiguas de piedra parcialmente cubiertas de arena",
            "43R": "una lagartija sobre una roca caliente, primer plano",
            "50": "un atardecer naranja intenso sobre dunas infinitas",
        },
    ),
    ApprovedDay(
        day_index=8,
        mode="independiente",
        sub_group="Costas tropicales",
        prompts={
            "43L": "olas rompiendo sobre arena blanca vistas de cerca",
            "43R": "una hoja de palmera con gotas de agua, macro",
            "50": "una bahía turquesa con arrecifes de coral visibles",
        },
    ),
    ApprovedDay(
        day_index=9,
        mode="split",
        sub_group="Costas tropicales",
        prompts={
            "wide": "una playa curva bordeada de palmeras al atardecer, panorámica",
            "50": "un barco de pescadores anclado en aguas cristalinas",
        },
    ),
    ApprovedDay(
        day_index=10,
        mode="independiente",
        sub_group="Costas tropicales",
        prompts={
            "43L": "un faro blanco sobre un acantilado costero",
            "43R": "conchas marinas dispersas sobre arena mojada",
            "50": "delfines saltando frente a una costa rocosa",
        },
    ),
    ApprovedDay(
        day_index=11,
        mode="independiente",
        sub_group="Montañas altas",
        prompts={
            "43L": "un pico nevado sobresaliendo entre nubes bajas",
            "43R": "una flor alpina creciendo entre rocas grises",
            "50": "un valle glaciar visto desde un mirador elevado",
        },
    ),
    ApprovedDay(
        day_index=12,
        mode="independiente",
        sub_group="Montañas altas",
        prompts={
            "43L": "un sendero de montaña serpenteante entre riscos",
            "43R": "cristales de hielo sobre una roca, macro",
            "50": "una cordillera al amanecer con luz dorada",
        },
    ),
    ApprovedDay(
        day_index=13,
        mode="split",
        sub_group="Montañas altas",
        prompts={
            "wide": "una cumbre nevada bajo un cielo estrellado, panorámica",
            "50": "un refugio de montaña iluminado en la noche",
        },
    ),
    ApprovedDay(
        day_index=14,
        mode="independiente",
        sub_group="Montañas altas",
        prompts={
            "43L": "una cabra montesa sobre un risco escarpado",
            "43R": "textura de roca volcánica oscura, primer plano",
            "50": "una vista panorámica de picos nevados al atardecer",
        },
    ),
]


def main() -> None:
    batch_id = batch_store.materialize_batch(THEME, DAYS)
    print(f"batch_id={batch_id} day_count={len(DAYS)}")

    started = time.monotonic()
    draft_summary = batch.run_draft_stage(batch_id)
    print(f"draft_summary={draft_summary}")
    print(f"draft elapsed={time.monotonic() - started:.1f}s")

    started = time.monotonic()
    finalize_summary = batch.run_finalize_stage(batch_id)
    print(f"finalize_summary={finalize_summary}")
    print(f"finalize elapsed={time.monotonic() - started:.1f}s")

    items = batch_store.get_batch_items(batch_id)
    finalized = [item for item in items if item.stage == "finalized"]
    needs_attention = [item for item in items if item.stage == "needs_attention"]
    print(
        f"total items={len(items)} finalized={len(finalized)} "
        f"needs_attention={len(needs_attention)}"
    )
    for item in needs_attention:
        print(
            f"  needs_attention: day={item.day_index} panel={item.panel} "
            f"error={item.error!r}"
        )


if __name__ == "__main__":
    main()
