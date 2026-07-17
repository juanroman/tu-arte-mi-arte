"""Demo manual de 2.4 (NO es un test de pytest): materializa un lote real de
3 días (9 imágenes, los 3 días en modo independiente para que el conteo de
paneles sea exacto y fácil de verificar), calcula el ETA con
`estimate_batch_duration` ANTES de correr nada, y luego corre el corredor
real (draft 1K -> finalización 4K) contra la API real de Gemini, sin
monkeypatch, para comparar el estimado inicial contra la duración real.

Hits the real API, costs money, tarda varios minutos (procesamiento
secuencial, ver docstring de src/engine/batch.py) — correr manualmente con
`uv run python scripts/demo_batch_2_4_eta_vs_actual.py`.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from engine import batch, batch_store  # noqa: E402
from engine.batch_store import ApprovedDay  # noqa: E402

THEME = "Jardines japoneses"

DAYS = [
    ApprovedDay(
        day_index=1,
        mode="independiente",
        sub_group="Estanques y reflejos",
        prompts={
            "43L": "un puente de madera roja cruzando un estanque con koi",
            "43R": "hojas de loto y gotas de agua, macro",
            "50": "un jardín zen con un estanque en calma reflejando arces",
        },
    ),
    ApprovedDay(
        day_index=2,
        mode="independiente",
        sub_group="Estanques y reflejos",
        prompts={
            "43L": "piedras cubiertas de musgo junto a un arroyo estrecho",
            "43R": "una linterna de piedra tradicional entre bambú",
            "50": "un sendero de piedra serpenteando entre arces japoneses",
        },
    ),
    ApprovedDay(
        day_index=3,
        mode="independiente",
        sub_group="Bambú y madera",
        prompts={
            "43L": "un bosque de bambú denso visto desde abajo",
            "43R": "textura de corteza de bambú, primer plano",
            "50": "un pabellón de madera tradicional entre bambú al atardecer",
        },
    ),
]


def main() -> None:
    day_modes = [day.mode for day in DAYS]
    eta = batch.estimate_batch_duration(day_modes)
    print(f"ETA inicial (antes de materializar): {eta}")
    print(
        f"  -> estimado: {eta['estimated_minutes']} minutos "
        f"({eta['estimated_seconds']:.1f}s)"
    )

    batch_id = batch_store.materialize_batch(THEME, DAYS)
    print(f"\nbatch_id={batch_id} day_count={len(DAYS)}")

    started = time.monotonic()
    draft_summary = batch.run_draft_stage(batch_id)
    draft_elapsed = time.monotonic() - started
    print(f"draft_summary={draft_summary}")
    print(f"draft elapsed={draft_elapsed:.1f}s")

    started = time.monotonic()
    finalize_summary = batch.run_finalize_stage(batch_id)
    finalize_elapsed = time.monotonic() - started
    print(f"finalize_summary={finalize_summary}")
    print(f"finalize elapsed={finalize_elapsed:.1f}s")

    total_actual = draft_elapsed + finalize_elapsed

    items = batch_store.get_batch_items(batch_id)
    finalized = [item for item in items if item.stage == "finalized"]
    needs_attention = [item for item in items if item.stage == "needs_attention"]
    print(
        f"\ntotal items={len(items)} finalized={len(finalized)} "
        f"needs_attention={len(needs_attention)}"
    )
    for item in needs_attention:
        print(
            f"  needs_attention: day={item.day_index} panel={item.panel} "
            f"policy_rejection={item.policy_rejection} error={item.error!r}"
        )

    print("\n--- ETA vs. real ---")
    eta_min = eta["estimated_minutes"]
    print(f"ETA estimado:  {eta['estimated_seconds']:.1f}s ({eta_min} min)")
    print(f"Tiempo real:   {total_actual:.1f}s ({total_actual / 60:.1f} min)")
    print(f"Sobrestimó (nunca subestimar)?  {eta['estimated_seconds'] >= total_actual}")

    summary = batch.summarize_batch(batch_id)
    print(f"\nsummarize_batch: stage_counts={summary['stage_counts']}")


if __name__ == "__main__":
    main()
