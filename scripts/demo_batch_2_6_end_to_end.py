"""Demo manual de cierre de Etapa 2 (iteración 2.6, NO es un test de
pytest): materializa un lote real de 3 días (mezcla independiente/split) y
corre el corredor completo (draft 1K -> finalización 4K) combinando
generación REAL contra Gemini con dos hooks deterministas -- una falla
forzada (rechazo de política) y una interrupción de proceso simulada
(crash) -- para demostrar en un solo run el criterio de cierre de 2.6:

  "un lote... con al menos un día independiente y un día split, produce
  los finales en disco tolerando al menos una falla forzada sin detener
  el resto, y sobrevive una interrupción simulada sin perder ni duplicar
  trabajo."

Por qué no monkeypatch de pytest ni un test unitario: los tests de 2.2/2.5
ya prueban estos invariantes con fakes puros (sin gastar en la API real).
Esta demo existe para confirmarlos en un contexto de generación REAL
mixta -- igual que demo_batch_2_3_finalize.py confirmó el volumen real y
demo_batch_2_4_eta_vs_actual.py confirmó el ETA contra tiempo real, esta
demo confirma tolerancia a fallas + resumibilidad contra la API real, no
solo contra fakes.

Estrategia concreta (sin usar pytest.monkeypatch, que no aplica fuera de
un test): se guardan las referencias reales de
`batch.generate_image`/`batch.generate_final_high_res` ANTES de asignar
un wrapper sobre el nombre del módulo -- mismo mecanismo que usan los
tests de test_engine_batch.py vía `monkeypatch.setattr(batch, ...)`,
aplicado aquí con asignación directa (igual que
`agent.generate_image_ai = _fake_generate_image_ai` en
scripts/eval_partial_failure.py). El wrapper intercepta por un substring
único del prompt/imagen -- nunca por conteo de llamadas -- y delega a la
función real en cualquier otro caso, así que la mayoría de los paneles sí
generan contra la API real.

Orden de los 3 días (importante, no arbitrario): el corredor procesa por
day_index ascendente y una excepción no capturada detiene el resto del
lote en esa invocación -- así que el día con rechazo de política (que
NO lanza excepción, solo devuelve un dict de error) debe ir ANTES del
día con el crash simulado (que sí lanza y detiene ahí mismo), para poder
demostrar en una sola llamada a run_draft_stage/run_finalize_stage tanto
"la falla forzada no bloquea al resto" (día 2 no detiene el día 3... salvo
que el día 3 sea el que crashea) como "el crash detiene ahí, sin perder
lo ya commiteado" (día 1 y 2 ya escritos en disco cuando el día 3 truena).

  - Día 1 (independiente): generación 100% real, camino feliz.
  - Día 2 (independiente): panel 43R fuerza un rechazo de política
    determinista (dict {'policy_rejection': True}, sin tocar la API real
    -- no es determinista ni gratis pedirle a Gemini que rechace algo de
    verdad); 43L/50 sí generan real. Demuestra que una falla forzada no
    detiene el resto del lote (requisito duro #2), incluso con
    generación real de otros paneles corriendo en el mismo run.
  - Día 3 (split): la imagen wide fuerza un RuntimeError determinista en
    su primer intento -- tanto en draft como en finalize -- simulando un
    `kill -9` real del proceso a mitad del lote. El wrapper se
    "desarma" tras el primer disparo por etapa, así que reinvocar el
    corredor (el "reinicio") complete sin volver a lanzar.

Hits the real API for most panels, costs money, tarda unos minutos
(procesamiento secuencial, ver docstring de src/engine/batch.py) --
correr manualmente con
`uv run python scripts/demo_batch_2_6_end_to_end.py`.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from engine import batch, batch_store  # noqa: E402
from engine.batch_store import ApprovedDay  # noqa: E402
from engine.generation import IMAGES_DIR  # noqa: E402

THEME = "Cierre de Etapa 2: alrededor de un valle de montaña"

POLICY_REJECTION_MARKER = "farol ceremonial DEMO-POLICY-DIA2"
CRASH_MARKER = "cordillera nevada DEMO-CRASH-DIA3"

DAYS = [
    ApprovedDay(
        day_index=1,
        mode="independiente",
        sub_group="Valle en calma",
        prompts={
            "43L": "un sendero de tierra entre pinos al amanecer",
            "43R": "musgo sobre una roca húmeda junto a un arroyo, macro",
            "50": "un valle verde visto desde un mirador, niebla baja",
        },
    ),
    ApprovedDay(
        day_index=2,
        mode="independiente",
        sub_group="Valle en calma",
        prompts={
            "43L": "una cabaña de piedra con humo saliendo de la chimenea",
            "43R": f"un {POLICY_REJECTION_MARKER} colgando de un poste de madera",
            "50": "ovejas pastando en una ladera al atardecer",
        },
    ),
    ApprovedDay(
        day_index=3,
        mode="split",
        sub_group="Alturas",
        prompts={
            "wide": f"una {CRASH_MARKER} bajo un cielo despejado, panorámica",
            "50": "un lago glaciar reflejando picos nevados",
        },
    ),
]


def _fake_policy_rejection() -> dict:
    return {
        "error": "El modelo rechazó la solicitud (política o derechos).",
        "policy_rejection": True,
    }


def _make_draft_wrapper(real_generate_image, crash_armed: dict):
    def wrapper(prompt: str, aspect_ratio: str) -> dict:
        if POLICY_REJECTION_MARKER in prompt:
            return _fake_policy_rejection()
        if CRASH_MARKER in prompt and crash_armed["draft"]:
            crash_armed["draft"] = False
            raise RuntimeError(
                "proceso murió generando la imagen ancha del día 3 (simulado)"
            )
        return real_generate_image(prompt, aspect_ratio)

    return wrapper


def _make_finalize_wrapper(
    real_generate_final_high_res, crash_armed: dict, target: list
):
    def wrapper(image_id: str) -> dict:
        if target and image_id == target[0] and crash_armed["finalize"]:
            crash_armed["finalize"] = False
            raise RuntimeError(
                "proceso murió finalizando la imagen ancha del día 3 (simulado)"
            )
        return real_generate_final_high_res(image_id)

    return wrapper


def _print_items(batch_id: str) -> None:
    for item in batch_store.get_batch_items(batch_id):
        print(
            f"  day={item.day_index} panel={item.panel} stage={item.stage} "
            f"attempts={item.attempts} policy_rejection={item.policy_rejection} "
            f"image_id={item.image_id}"
        )
    for day in batch_store.get_batch_days(batch_id):
        print(
            f"  day={day.day_index} wide_stage={day.wide_stage} "
            f"wide_image_id={day.wide_image_id}"
        )


def main() -> None:
    real_generate_image = batch.generate_image
    real_generate_final_high_res = batch.generate_final_high_res
    crash_armed = {"draft": True, "finalize": True}

    batch_id = batch_store.materialize_batch(THEME, DAYS)
    print(f"batch_id={batch_id} day_count={len(DAYS)}")

    # --- Etapa de draft: crash simulado en el primer intento del día 3 ---
    batch.generate_image = _make_draft_wrapper(real_generate_image, crash_armed)
    try:
        batch.run_draft_stage(batch_id)
        raise AssertionError("se esperaba un RuntimeError simulando el crash")
    except RuntimeError as exc:
        print(f"\n[crash simulado en draft] {exc}")

    print("\nEstado tras el crash (día 1 drafted, día 2 aislado, día 3 pending):")
    _print_items(batch_id)

    print("\n[reinicio] reinvocando run_draft_stage sobre el mismo batch_id...")
    draft_summary_2 = batch.run_draft_stage(batch_id)
    print(f"draft_summary (tras reinicio)={draft_summary_2}")
    batch.generate_image = real_generate_image

    print("\nEstado tras completar el draft:")
    _print_items(batch_id)

    # --- Etapa de finalización: crash simulado en la fuente wide del día 3 ---
    day_3 = next(d for d in batch_store.get_batch_days(batch_id) if d.day_index == 3)
    wide_target = [day_3.wide_image_id]
    batch.generate_final_high_res = _make_finalize_wrapper(
        real_generate_final_high_res, crash_armed, wide_target
    )
    try:
        batch.run_finalize_stage(batch_id)
        raise AssertionError("se esperaba un RuntimeError simulando el crash")
    except RuntimeError as exc:
        print(f"\n[crash simulado en finalize] {exc}")

    print("\n[reinicio] reinvocando run_finalize_stage sobre el mismo batch_id...")
    finalize_summary_2 = batch.run_finalize_stage(batch_id)
    print(f"finalize_summary (tras reinicio)={finalize_summary_2}")
    batch.generate_final_high_res = real_generate_final_high_res

    print("\nEstado final:")
    _print_items(batch_id)

    items = batch_store.get_batch_items(batch_id)
    finalized = [item for item in items if item.stage == "finalized"]
    needs_attention = [item for item in items if item.stage == "needs_attention"]
    print(
        f"\ntotal items={len(items)} finalized={len(finalized)} "
        f"needs_attention={len(needs_attention)}"
    )

    print("\nConfirmando en disco los image_id finalizados:")
    for item in finalized:
        path = IMAGES_DIR / f"{item.image_id}.jpg"
        size = path.stat().st_size if path.exists() else 0
        print(f"  {item.image_id}: existe={path.exists()} size={size}")

    summary = batch.summarize_batch(batch_id)
    print(f"\nsummarize_batch: {summary}")


if __name__ == "__main__":
    main()
