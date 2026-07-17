import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from engine import (
    batch,  # noqa: E402
    batch_store,  # noqa: E402
)
from engine.batch_store import ApprovedDay  # noqa: E402


def _materialize_independiente_day(db_path, day_index=1):
    days = [
        ApprovedDay(
            day_index=day_index,
            mode="independiente",
            sub_group="Sub-grupo A",
            prompts={"43L": "escena l", "43R": "escena r", "50": "escena 50"},
        )
    ]
    return batch_store.materialize_batch("Tema", days, path=db_path)


def _materialize_split_day(db_path, day_index=1):
    days = [
        ApprovedDay(
            day_index=day_index,
            mode="split",
            sub_group="Sub-grupo A",
            prompts={"wide": "escena ancha", "50": "escena 50"},
        )
    ]
    return batch_store.materialize_batch("Tema", days, path=db_path)


def _succeeding_generate_image(calls):
    def fake(prompt, aspect_ratio):
        calls.append((prompt, aspect_ratio))
        return {"image_id": f"img_{len(calls)}", "path": "/tmp/fake.jpg"}

    return fake


def _draft_successfully(batch_id, db_path, monkeypatch):
    """Runs the real draft stage with an always-succeeding fake, leaving
    every item in 'drafted' -- setup shared by the finalize-stage tests
    below (2.3), which only care about the finalize transition itself.
    """
    monkeypatch.setattr(batch, "generate_image", _succeeding_generate_image([]))
    batch.run_draft_stage(batch_id, path=db_path)


def test_independiente_day_drafts_all_three_panels_on_first_success(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "batch.sqlite3"
    batch_id = _materialize_independiente_day(db_path)
    calls = []
    monkeypatch.setattr(batch, "generate_image", _succeeding_generate_image(calls))

    summary = batch.run_draft_stage(batch_id, path=db_path)

    assert set(summary["drafted"]) == {"1:43L", "1:43R", "1:50"}
    assert summary["needs_attention"] == []
    items = {
        item.panel: item for item in batch_store.get_batch_items(batch_id, path=db_path)
    }
    for panel in ("43L", "43R", "50"):
        assert items[panel].stage == "drafted"
        assert items[panel].attempts == 1
        assert items[panel].image_id is not None
        assert items[panel].error is None
    assert len(calls) == 3


def test_policy_rejection_never_retries_and_goes_straight_to_needs_attention(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "batch.sqlite3"
    batch_id = _materialize_independiente_day(db_path)
    calls = []

    def fake(prompt, aspect_ratio):
        calls.append((prompt, aspect_ratio))
        return {"error": "rechazo de política", "policy_rejection": True}

    monkeypatch.setattr(batch, "generate_image", fake)

    batch.run_draft_stage(batch_id, path=db_path)

    items = {
        item.panel: item for item in batch_store.get_batch_items(batch_id, path=db_path)
    }
    # Cada panel independiente recibe su propia llamada, cada una rechazada
    # una sola vez -- nunca reintentada pese a tener presupuesto restante.
    assert items["43L"].stage == "needs_attention"
    assert items["43L"].attempts == 1
    assert items["43L"].error == "rechazo de política"
    assert items["43L"].image_id is None
    assert items["43L"].policy_rejection is True
    assert len(calls) == 3  # una llamada por panel, ninguna reintentada


def test_item_exhausts_configured_retry_ceiling_before_needs_attention(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "batch.sqlite3"
    batch_id = _materialize_independiente_day(db_path)

    def fake(prompt, aspect_ratio):
        # Todas fallan siempre, con un error genérico (no política).
        return {"error": "fallo técnico transitorio"}

    monkeypatch.setattr(batch, "generate_image", fake)
    max_attempts = batch_store.load_batch_config().generation_max_attempts

    batch.run_draft_stage(batch_id, path=db_path)

    items = {
        item.panel: item for item in batch_store.get_batch_items(batch_id, path=db_path)
    }
    for panel in ("43L", "43R", "50"):
        assert items[panel].stage == "needs_attention"
        assert items[panel].attempts == max_attempts
        assert items[panel].error == "fallo técnico transitorio"
        assert items[panel].policy_rejection is False


def test_item_retries_generic_error_then_succeeds(tmp_path, monkeypatch):
    db_path = tmp_path / "batch.sqlite3"
    days = [
        ApprovedDay(
            day_index=1,
            mode="independiente",
            sub_group="Sub-grupo A",
            prompts={"43L": "escena l", "43R": "escena r", "50": "escena 50"},
        )
    ]
    batch_id = batch_store.materialize_batch("Tema", days, path=db_path)

    attempts_per_panel = {}

    def fake(prompt, aspect_ratio):
        # La primera llamada de cada panel falla genérico, la segunda éxito.
        key = prompt
        attempts_per_panel[key] = attempts_per_panel.get(key, 0) + 1
        if attempts_per_panel[key] == 1:
            return {"error": "fallo técnico transitorio"}
        return {"image_id": f"img_{key[:4]}_{attempts_per_panel[key]}"}

    monkeypatch.setattr(batch, "generate_image", fake)

    batch.run_draft_stage(batch_id, path=db_path)

    items = {
        item.panel: item for item in batch_store.get_batch_items(batch_id, path=db_path)
    }
    for panel in ("43L", "43R", "50"):
        assert items[panel].stage == "drafted"
        assert items[panel].attempts == 2
        assert items[panel].image_id is not None


def test_one_item_failure_does_not_block_the_others_of_the_same_batch(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "batch.sqlite3"
    batch_id = _materialize_independiente_day(db_path)

    def fake(prompt, aspect_ratio):
        if aspect_ratio == "9:16":
            return {"error": "rechazo de política", "policy_rejection": True}
        return {"image_id": "img_ok"}

    monkeypatch.setattr(batch, "generate_image", fake)

    batch.run_draft_stage(batch_id, path=db_path)

    items = {
        item.panel: item for item in batch_store.get_batch_items(batch_id, path=db_path)
    }
    assert items["43L"].stage == "needs_attention"
    assert items["43R"].stage == "needs_attention"
    assert items["50"].stage == "drafted"
    assert items["50"].image_id == "img_ok"


def test_split_day_generates_wide_image_once_for_43l_and_43r(tmp_path, monkeypatch):
    db_path = tmp_path / "batch.sqlite3"
    batch_id = _materialize_split_day(db_path)
    calls = []
    monkeypatch.setattr(batch, "generate_image", _succeeding_generate_image(calls))

    batch.run_draft_stage(batch_id, path=db_path)

    items = {
        item.panel: item for item in batch_store.get_batch_items(batch_id, path=db_path)
    }
    assert items["43L"].stage == "drafted"
    assert items["43R"].stage == "drafted"
    assert items["43L"].attempts == items["43R"].attempts == 1
    # El panel físico de un día split no recibe image_id propio en 2.2 --
    # se puebla en la finalización (2.3) vía split.split_wide_image.
    assert items["43L"].image_id is None
    assert items["43R"].image_id is None

    day = batch_store.get_batch_days(batch_id, path=db_path)[0]
    assert day.wide_stage == "drafted"
    assert day.wide_image_id is not None

    # Wide (una llamada) + panel 50 (otra) = 2 llamadas totales, nunca 3.
    assert len(calls) == 2


def test_split_day_exhausting_retries_leaves_both_panels_and_wide_in_needs_attention(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "batch.sqlite3"
    batch_id = _materialize_split_day(db_path)
    max_attempts = batch_store.load_batch_config().generation_max_attempts
    wide_calls = {"n": 0}

    def fake(prompt, aspect_ratio):
        if aspect_ratio == "16:9":
            return {"image_id": "img_50"}
        wide_calls["n"] += 1
        return {"error": "fallo técnico transitorio"}

    monkeypatch.setattr(batch, "generate_image", fake)

    batch.run_draft_stage(batch_id, path=db_path)

    items = {
        item.panel: item for item in batch_store.get_batch_items(batch_id, path=db_path)
    }
    assert items["43L"].stage == "needs_attention"
    assert items["43R"].stage == "needs_attention"
    assert items["43L"].attempts == max_attempts
    assert items["43R"].attempts == max_attempts
    assert items["43L"].policy_rejection is False
    assert items["43R"].policy_rejection is False
    assert items["50"].stage == "drafted"

    day = batch_store.get_batch_days(batch_id, path=db_path)[0]
    assert day.wide_stage == "needs_attention"
    assert day.wide_image_id is None
    assert wide_calls["n"] == max_attempts


def test_reinvoking_run_draft_stage_skips_items_already_past_pending(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "batch.sqlite3"
    batch_id = _materialize_independiente_day(db_path)
    calls = []
    monkeypatch.setattr(batch, "generate_image", _succeeding_generate_image(calls))

    first_summary = batch.run_draft_stage(batch_id, path=db_path)
    assert len(calls) == 3

    second_summary = batch.run_draft_stage(batch_id, path=db_path)

    # Ninguna llamada nueva -- todos los ítems ya estaban en 'drafted'.
    assert len(calls) == 3
    assert second_summary["drafted"] == []
    assert set(second_summary["skipped"]) == {"1:43L", "1:43R", "1:50"}
    assert first_summary["drafted"] != []


def _succeeding_generate_final_high_res(calls):
    def fake(image_id):
        calls.append(image_id)
        return {"image_id": f"final_{image_id}", "path": "/tmp/fake_final.jpg"}

    return fake


def _succeeding_split_wide_image(calls):
    def fake(image_id, gap_fraction):
        calls.append((image_id, gap_fraction))
        return {
            "left": {"image_id": f"final_{image_id}_L", "path": "/tmp/left.jpg"},
            "right": {"image_id": f"final_{image_id}_R", "path": "/tmp/right.jpg"},
        }

    return fake


def test_finalize_item_succeeds_on_first_attempt_for_independiente_day(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "batch.sqlite3"
    batch_id = _materialize_independiente_day(db_path)
    _draft_successfully(batch_id, db_path, monkeypatch)
    draft_items = {
        item.panel: item for item in batch_store.get_batch_items(batch_id, path=db_path)
    }

    calls = []
    monkeypatch.setattr(
        batch, "generate_final_high_res", _succeeding_generate_final_high_res(calls)
    )

    summary = batch.run_finalize_stage(batch_id, path=db_path)

    assert set(summary["finalized"]) == {"1:43L", "1:43R", "1:50"}
    items = {
        item.panel: item for item in batch_store.get_batch_items(batch_id, path=db_path)
    }
    for panel in ("43L", "43R", "50"):
        assert items[panel].stage == "finalized"
        assert items[panel].attempts == 1
        assert items[panel].image_id == f"final_{draft_items[panel].image_id}"
        assert items[panel].image_id != draft_items[panel].image_id
    assert len(calls) == 3


def test_finalize_policy_rejection_never_retries_and_preserves_draft_image(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "batch.sqlite3"
    batch_id = _materialize_independiente_day(db_path)
    _draft_successfully(batch_id, db_path, monkeypatch)
    draft_items = {
        item.panel: item for item in batch_store.get_batch_items(batch_id, path=db_path)
    }

    calls = []

    def fake(image_id):
        calls.append(image_id)
        return {"error": "rechazo de política", "policy_rejection": True}

    monkeypatch.setattr(batch, "generate_final_high_res", fake)

    batch.run_finalize_stage(batch_id, path=db_path)

    items = {
        item.panel: item for item in batch_store.get_batch_items(batch_id, path=db_path)
    }
    assert items["43L"].stage == "needs_attention"
    assert items["43L"].attempts == 1
    assert items["43L"].error == "rechazo de política"
    assert items["43L"].policy_rejection is True
    # El draft original se preserva -- una falla de finalización nunca lo destruye.
    assert items["43L"].image_id == draft_items["43L"].image_id
    assert len(calls) == 3  # una llamada por panel, ninguna reintentada


def test_finalize_exhausts_retry_ceiling_before_needs_attention(tmp_path, monkeypatch):
    db_path = tmp_path / "batch.sqlite3"
    batch_id = _materialize_independiente_day(db_path)
    _draft_successfully(batch_id, db_path, monkeypatch)
    max_attempts = batch_store.load_batch_config().generation_max_attempts

    def fake(image_id):
        return {"error": "fallo técnico transitorio"}

    monkeypatch.setattr(batch, "generate_final_high_res", fake)

    batch.run_finalize_stage(batch_id, path=db_path)

    items = {
        item.panel: item for item in batch_store.get_batch_items(batch_id, path=db_path)
    }
    for panel in ("43L", "43R", "50"):
        assert items[panel].stage == "needs_attention"
        assert items[panel].attempts == max_attempts
        assert items[panel].error == "fallo técnico transitorio"
        assert items[panel].policy_rejection is False


def test_finalize_retries_generic_error_then_succeeds(tmp_path, monkeypatch):
    db_path = tmp_path / "batch.sqlite3"
    batch_id = _materialize_independiente_day(db_path)
    _draft_successfully(batch_id, db_path, monkeypatch)

    attempts_per_image = {}

    def fake(image_id):
        attempts_per_image[image_id] = attempts_per_image.get(image_id, 0) + 1
        if attempts_per_image[image_id] == 1:
            return {"error": "fallo técnico transitorio"}
        return {"image_id": f"final_{image_id}"}

    monkeypatch.setattr(batch, "generate_final_high_res", fake)

    batch.run_finalize_stage(batch_id, path=db_path)

    items = {
        item.panel: item for item in batch_store.get_batch_items(batch_id, path=db_path)
    }
    for panel in ("43L", "43R", "50"):
        assert items[panel].stage == "finalized"
        assert items[panel].attempts == 2


def test_one_item_finalize_failure_does_not_block_the_others(tmp_path, monkeypatch):
    db_path = tmp_path / "batch.sqlite3"
    batch_id = _materialize_independiente_day(db_path)
    _draft_successfully(batch_id, db_path, monkeypatch)
    draft_items = {
        item.panel: item for item in batch_store.get_batch_items(batch_id, path=db_path)
    }
    fifty_draft_image_id = draft_items["50"].image_id

    def fake(image_id):
        if image_id == fifty_draft_image_id:
            return {"image_id": f"final_{image_id}"}
        return {"error": "rechazo de política", "policy_rejection": True}

    monkeypatch.setattr(batch, "generate_final_high_res", fake)

    batch.run_finalize_stage(batch_id, path=db_path)

    items = {
        item.panel: item for item in batch_store.get_batch_items(batch_id, path=db_path)
    }
    assert items["43L"].stage == "needs_attention"
    assert items["43R"].stage == "needs_attention"
    assert items["50"].stage == "finalized"
    assert items["50"].image_id == f"final_{fifty_draft_image_id}"


def test_split_day_finalizes_wide_once_and_splits_into_43l_43r(tmp_path, monkeypatch):
    db_path = tmp_path / "batch.sqlite3"
    batch_id = _materialize_split_day(db_path)
    _draft_successfully(batch_id, db_path, monkeypatch)
    draft_day = batch_store.get_batch_days(batch_id, path=db_path)[0]
    draft_wide_image_id = draft_day.wide_image_id

    finalize_calls = []
    split_calls = []
    monkeypatch.setattr(
        batch,
        "generate_final_high_res",
        _succeeding_generate_final_high_res(finalize_calls),
    )
    monkeypatch.setattr(
        batch, "split_wide_image", _succeeding_split_wide_image(split_calls)
    )

    summary = batch.run_finalize_stage(batch_id, path=db_path)

    assert set(summary["finalized"]) == {"1:wide", "1:50"}
    items = {
        item.panel: item for item in batch_store.get_batch_items(batch_id, path=db_path)
    }
    assert items["43L"].stage == "finalized"
    assert items["43R"].stage == "finalized"
    finalized_wide_image_id = f"final_{draft_wide_image_id}"
    assert items["43L"].image_id == f"final_{finalized_wide_image_id}_L"
    assert items["43R"].image_id == f"final_{finalized_wide_image_id}_R"

    day = batch_store.get_batch_days(batch_id, path=db_path)[0]
    assert day.wide_stage == "finalized"
    assert day.wide_image_id == finalized_wide_image_id

    # La fuente ancha se finaliza una sola vez (+1 finalización del panel 50,
    # nunca 3 llamadas separadas para 43L/43R/wide).
    assert finalize_calls.count(draft_wide_image_id) == 1
    assert len(finalize_calls) == 2
    assert split_calls == [(finalized_wide_image_id, split_calls[0][1])]


def test_split_day_finalize_wide_failure_preserves_draft_wide_image_id(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "batch.sqlite3"
    batch_id = _materialize_split_day(db_path)
    _draft_successfully(batch_id, db_path, monkeypatch)
    draft_day = batch_store.get_batch_days(batch_id, path=db_path)[0]
    draft_wide_image_id = draft_day.wide_image_id

    def fake_finalize(image_id):
        return {"error": "fallo técnico transitorio"}

    monkeypatch.setattr(batch, "generate_final_high_res", fake_finalize)

    batch.run_finalize_stage(batch_id, path=db_path)

    day = batch_store.get_batch_days(batch_id, path=db_path)[0]
    assert day.wide_stage == "needs_attention"
    assert day.wide_image_id == draft_wide_image_id

    items = {
        item.panel: item for item in batch_store.get_batch_items(batch_id, path=db_path)
    }
    assert items["43L"].stage == "needs_attention"
    assert items["43R"].stage == "needs_attention"
    assert items["43L"].image_id is None
    assert items["43R"].image_id is None


def test_split_day_split_failure_retries_only_the_split_not_the_wide_source(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "batch.sqlite3"
    batch_id = _materialize_split_day(db_path)
    _draft_successfully(batch_id, db_path, monkeypatch)
    draft_wide_image_id = batch_store.get_batch_days(batch_id, path=db_path)[
        0
    ].wide_image_id

    finalize_calls = []
    monkeypatch.setattr(
        batch,
        "generate_final_high_res",
        _succeeding_generate_final_high_res(finalize_calls),
    )

    def failing_split(image_id, gap_fraction):
        return {"error": "no existe una imagen fuente"}

    monkeypatch.setattr(batch, "split_wide_image", failing_split)

    first_summary = batch.run_finalize_stage(batch_id, path=db_path)

    day = batch_store.get_batch_days(batch_id, path=db_path)[0]
    assert day.wide_stage == "finalized"  # requisito duro #4: la fuente sí quedó lista
    items = {
        item.panel: item for item in batch_store.get_batch_items(batch_id, path=db_path)
    }
    assert items["43L"].stage == "needs_attention"
    assert items["43R"].stage == "needs_attention"
    assert set(first_summary["needs_attention"]) == {"1:wide"}
    # Solo 1 llamada de finalización para la fuente ancha (+1 para el panel 50).
    assert finalize_calls.count(draft_wide_image_id) == 1
    assert len(finalize_calls) == 2

    split_calls = []
    monkeypatch.setattr(
        batch, "split_wide_image", _succeeding_split_wide_image(split_calls)
    )

    second_summary = batch.run_finalize_stage(batch_id, path=db_path)

    # La fuente ancha nunca se vuelve a finalizar -- solo se reintenta el split.
    assert finalize_calls.count(draft_wide_image_id) == 1
    assert len(finalize_calls) == 2
    assert len(split_calls) == 1
    assert set(second_summary["finalized"]) == {"1:wide"}
    items = {
        item.panel: item for item in batch_store.get_batch_items(batch_id, path=db_path)
    }
    assert items["43L"].stage == "finalized"
    assert items["43R"].stage == "finalized"


def test_reinvoking_run_finalize_stage_skips_items_already_finalized(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "batch.sqlite3"
    batch_id = _materialize_independiente_day(db_path)
    _draft_successfully(batch_id, db_path, monkeypatch)

    calls = []
    monkeypatch.setattr(
        batch, "generate_final_high_res", _succeeding_generate_final_high_res(calls)
    )

    first_summary = batch.run_finalize_stage(batch_id, path=db_path)
    assert len(calls) == 3

    second_summary = batch.run_finalize_stage(batch_id, path=db_path)

    assert len(calls) == 3
    assert second_summary["finalized"] == []
    assert set(second_summary["skipped"]) == {"1:43L", "1:43R", "1:50"}
    assert first_summary["finalized"] != []


def test_estimate_batch_duration_counts_model_calls_by_mode():
    result = batch.estimate_batch_duration(["independiente", "split", "independiente"])

    assert result["day_count"] == 3
    assert result["independent_days"] == 2
    assert result["split_days"] == 1
    # 2 días independiente (3 llamadas c/u) + 1 día split (2 llamadas) = 8.
    assert result["total_model_calls"] == 8
    config = batch_store.load_batch_config()
    expected_generation_seconds = 8 * (
        config.draft_seconds_per_call + config.finalize_seconds_per_call
    )
    expected_deploy_seconds = 3 * config.deploy_seconds_per_day
    expected_seconds = (
        expected_generation_seconds + expected_deploy_seconds
    ) * config.eta_safety_margin
    assert result["estimated_seconds"] == expected_seconds


def test_estimate_batch_duration_never_underestimates_the_real_2_3_demo_batch():
    """Ancla de regresión (dev_plan_phase_2.md §2.4): el usuario pidió
    explícitamente que el estimado nunca quede por debajo de la duración
    real -- este test lo verifica contra la medición real de la demo de
    2.3 (batch_09ab27e8: 10 días independiente + 4 split, 319.1s de draft
    + 913.0s de finalización = 1232.1s reales, sin monkeypatch, contra la
    API real de Gemini). El estimado incluye además un término de
    despliegue (placeholder, Etapa 4) que la demo de 2.3 no ejerció -- solo
    hace la cota más holgada, nunca la reduce.
    """
    day_modes = ["independiente"] * 10 + ["split"] * 4
    real_measured_seconds = 319.1 + 913.0

    result = batch.estimate_batch_duration(day_modes)

    assert result["estimated_seconds"] >= real_measured_seconds


def test_estimate_batch_duration_never_underestimates_the_real_2_4_verification_batch():
    """Segunda ancla de regresión (dev_plan_phase_2.md §2.4, hallazgo
    post-cierre): la primera versión de esta iteración SÍ subestimó un
    lote real -- batch_86bd3e0f (3 días independiente, 9 llamadas de
    generación) tardó 95.9s de draft + 559.2s de finalización = 655.1s
    reales, contra un estimado inicial de solo 432.0s. Este test ancla la
    corrección (finalize_seconds_per_call subido de 30 a 65) contra esa
    misma medición real para que una futura baja accidental de la
    constante no reintroduzca el mismo fallo.
    """
    day_modes = ["independiente"] * 3
    real_measured_seconds = 95.9 + 559.2

    result = batch.estimate_batch_duration(day_modes)

    assert result["estimated_seconds"] >= real_measured_seconds


def test_estimate_batch_duration_includes_a_deploy_time_term():
    """PRD §15.2 objetivo 4 pide estimar 'generación final 4K +
    despliegue', no solo generación -- este test confirma que el término
    de despliegue (placeholder sin medición real todavía, ver docstring
    de estimate_batch_duration) participa en el cálculo, escalando por
    día del lote (no por panel, ya que las TVs de un día se despliegan en
    paralelo entre sí).
    """
    config = batch_store.load_batch_config()
    result_one_day = batch.estimate_batch_duration(["independiente"])
    result_two_days = batch.estimate_batch_duration(["independiente"] * 2)

    generation_seconds_per_day = 3 * (
        config.draft_seconds_per_call + config.finalize_seconds_per_call
    )
    expected_delta = (
        generation_seconds_per_day + config.deploy_seconds_per_day
    ) * config.eta_safety_margin
    assert (
        result_two_days["estimated_seconds"] - result_one_day["estimated_seconds"]
        == expected_delta
    )


def _materialize_two_days(db_path):
    days = [
        ApprovedDay(
            day_index=1,
            mode="independiente",
            sub_group="Sub-grupo A",
            prompts={"43L": "escena l", "43R": "escena r", "50": "escena 50"},
        ),
        ApprovedDay(
            day_index=2,
            mode="split",
            sub_group="Sub-grupo B",
            prompts={"wide": "escena ancha", "50": "escena 50 dos"},
        ),
    ]
    return batch_store.materialize_batch("Tema del lote", days, path=db_path)


def test_summarize_batch_returns_error_for_unknown_batch_id(tmp_path):
    db_path = tmp_path / "batch.sqlite3"

    result = batch.summarize_batch("batch_no_existe", path=db_path)

    assert "error" in result


def test_summarize_batch_distinguishes_policy_rejection_from_technical_failure(
    tmp_path,
):
    db_path = tmp_path / "batch.sqlite3"
    batch_id = _materialize_two_days(db_path)

    # Día 1: 43L rechazo de política, 43R falla técnica agotada, 50 finalizado.
    batch_store.record_item_attempt(
        batch_id,
        1,
        "43L",
        attempts=1,
        stage="needs_attention",
        image_id=None,
        error="rechazo de política",
        policy_rejection=True,
        path=db_path,
    )
    batch_store.record_item_attempt(
        batch_id,
        1,
        "43R",
        attempts=2,
        stage="needs_attention",
        image_id=None,
        error="fallo técnico transitorio",
        policy_rejection=False,
        path=db_path,
    )
    batch_store.record_item_attempt(
        batch_id,
        1,
        "50",
        attempts=1,
        stage="finalized",
        image_id="img_ok",
        error=None,
        path=db_path,
    )

    result = batch.summarize_batch(batch_id, path=db_path)

    assert result["batch_id"] == batch_id
    assert result["theme"] == "Tema del lote"
    assert result["day_count"] == 2
    assert result["stage_counts"]["needs_attention"] == 2
    assert result["stage_counts"]["finalized"] == 1
    assert result["stage_counts"]["pending"] == 3  # día 2 (wide, 50) + 50 sin tocar

    policy_rejections = result["needs_attention_policy_rejection"]
    assert len(policy_rejections) == 1
    assert policy_rejections[0]["day_index"] == 1
    assert policy_rejections[0]["panel"] == "43L"

    technical_failures = result["needs_attention_technical"]
    assert len(technical_failures) == 1
    assert technical_failures[0]["day_index"] == 1
    assert technical_failures[0]["panel"] == "43R"
    assert technical_failures[0]["attempts"] == 2

    day_1 = next(day for day in result["days"] if day["day_index"] == 1)
    assert day_1["mode"] == "independiente"
    assert day_1["sub_group"] == "Sub-grupo A"
    assert day_1["panels"]["43L"]["stage"] == "needs_attention"
    assert day_1["panels"]["50"]["image_id"] == "img_ok"

    day_2 = next(day for day in result["days"] if day["day_index"] == 2)
    assert day_2["mode"] == "split"
    assert day_2["sub_group"] == "Sub-grupo B"
