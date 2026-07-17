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
