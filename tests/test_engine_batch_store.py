import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from engine import batch_store  # noqa: E402
from engine.batch_store import (  # noqa: E402
    ApprovedDay,
    PanelOutcome,
    WideOutcome,
)


def test_load_batch_config_reads_retry_ceilings():
    config = batch_store.load_batch_config()

    assert config.generation_max_attempts == 2
    assert config.tv_deploy_max_attempts == 3
    assert config.draft_seconds_per_call == 10
    assert config.finalize_seconds_per_call == 65
    assert config.deploy_seconds_per_day == 90
    assert config.eta_safety_margin == 1.2


def test_materialize_batch_creates_correct_row_counts_for_mixed_modes(tmp_path):
    db_path = tmp_path / "batch.sqlite3"
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
            sub_group="Sub-grupo A",
            prompts={"wide": "escena ancha", "50": "escena 50 dia 2"},
        ),
    ]

    batch_id = batch_store.materialize_batch("Primavera", days, path=db_path)

    batch = batch_store.get_batch(batch_id, path=db_path)
    assert batch is not None
    assert batch.theme == "Primavera"
    assert batch.day_count == 2
    assert batch.status == "materialized"

    batch_days = batch_store.get_batch_days(batch_id, path=db_path)
    assert len(batch_days) == 2

    items = batch_store.get_batch_items(batch_id, path=db_path)
    day_1_items = [item for item in items if item.day_index == 1]
    day_2_items = [item for item in items if item.day_index == 2]
    assert len(day_1_items) == 3
    assert len(day_2_items) == 3
    assert {item.panel for item in day_1_items} == {"43L", "43R", "50"}
    assert {item.panel for item in day_2_items} == {"43L", "43R", "50"}


def test_materialize_batch_split_day_shares_prompt_across_43l_and_43r(tmp_path):
    db_path = tmp_path / "batch.sqlite3"
    days = [
        ApprovedDay(
            day_index=1,
            mode="split",
            sub_group="Sub-grupo A",
            prompts={"wide": "un horizonte compartido", "50": "otra escena"},
        )
    ]

    batch_id = batch_store.materialize_batch("Otoño", days, path=db_path)

    items = {
        item.panel: item for item in batch_store.get_batch_items(batch_id, path=db_path)
    }
    assert items["43L"].prompt == "un horizonte compartido"
    assert items["43R"].prompt == "un horizonte compartido"
    assert items["50"].prompt == "otra escena"


def test_materialize_batch_items_start_pending(tmp_path):
    db_path = tmp_path / "batch.sqlite3"
    days = [
        ApprovedDay(
            day_index=1,
            mode="independiente",
            sub_group="Sub-grupo A",
            prompts={"43L": "a", "43R": "b", "50": "c"},
        )
    ]

    batch_id = batch_store.materialize_batch("Verano", days, path=db_path)

    for item in batch_store.get_batch_items(batch_id, path=db_path):
        assert item.stage == "pending"
        assert item.attempts == 0
        assert item.image_id is None
        assert item.error is None


def test_materialize_batch_assigns_unique_batch_id(tmp_path):
    db_path = tmp_path / "batch.sqlite3"
    days = [
        ApprovedDay(
            day_index=1,
            mode="independiente",
            sub_group="Sub-grupo A",
            prompts={"43L": "a", "43R": "b", "50": "c"},
        )
    ]

    first_id = batch_store.materialize_batch("Tema", days, path=db_path)
    second_id = batch_store.materialize_batch("Tema", days, path=db_path)

    assert first_id != second_id


def test_batch_day_wide_stage_is_pending_for_split_and_none_for_independiente(
    tmp_path,
):
    db_path = tmp_path / "batch.sqlite3"
    days = [
        ApprovedDay(
            day_index=1,
            mode="independiente",
            sub_group="Sub-grupo A",
            prompts={"43L": "a", "43R": "b", "50": "c"},
        ),
        ApprovedDay(
            day_index=2,
            mode="split",
            sub_group="Sub-grupo A",
            prompts={"wide": "d", "50": "e"},
        ),
    ]

    batch_id = batch_store.materialize_batch("Tema", days, path=db_path)

    batch_days = {
        day.day_index: day for day in batch_store.get_batch_days(batch_id, path=db_path)
    }
    assert batch_days[1].wide_stage is None
    assert batch_days[2].wide_stage == "pending"
    assert batch_days[1].wide_image_id is None
    assert batch_days[2].wide_image_id is None


def test_record_item_attempt_updates_only_the_targeted_item(tmp_path):
    db_path = tmp_path / "batch.sqlite3"
    days = [
        ApprovedDay(
            day_index=1,
            mode="independiente",
            sub_group="Sub-grupo A",
            prompts={"43L": "a", "43R": "b", "50": "c"},
        )
    ]
    batch_id = batch_store.materialize_batch("Tema", days, path=db_path)

    batch_store.record_item_attempt(
        batch_id,
        1,
        "43L",
        attempts=1,
        stage="drafted",
        image_id="img_abc123",
        error=None,
        path=db_path,
    )

    items = {
        item.panel: item for item in batch_store.get_batch_items(batch_id, path=db_path)
    }
    assert items["43L"].attempts == 1
    assert items["43L"].stage == "drafted"
    assert items["43L"].image_id == "img_abc123"
    assert items["43L"].error is None
    assert items["43L"].updated_at is not None
    assert items["43L"].prompt == "a"
    # Los demás paneles del mismo batch quedan intactos.
    assert items["43R"].stage == "pending"
    assert items["43R"].attempts == 0
    assert items["50"].stage == "pending"


def test_record_item_attempt_persists_error_and_needs_attention(tmp_path):
    db_path = tmp_path / "batch.sqlite3"
    days = [
        ApprovedDay(
            day_index=1,
            mode="independiente",
            sub_group="Sub-grupo A",
            prompts={"43L": "a", "43R": "b", "50": "c"},
        )
    ]
    batch_id = batch_store.materialize_batch("Tema", days, path=db_path)

    batch_store.record_item_attempt(
        batch_id,
        1,
        "43L",
        attempts=2,
        stage="needs_attention",
        image_id=None,
        error="rechazo de política",
        path=db_path,
    )

    items = {
        item.panel: item for item in batch_store.get_batch_items(batch_id, path=db_path)
    }
    assert items["43L"].stage == "needs_attention"
    assert items["43L"].attempts == 2
    assert items["43L"].image_id is None
    assert items["43L"].error == "rechazo de política"


def test_record_wide_image_updates_batch_day_without_touching_batch_item(tmp_path):
    db_path = tmp_path / "batch.sqlite3"
    days = [
        ApprovedDay(
            day_index=1,
            mode="split",
            sub_group="Sub-grupo A",
            prompts={"wide": "un horizonte compartido", "50": "otra escena"},
        )
    ]
    batch_id = batch_store.materialize_batch("Tema", days, path=db_path)

    batch_store.record_wide_image(
        batch_id, 1, wide_image_id="img_wide001", wide_stage="drafted", path=db_path
    )

    batch_day = batch_store.get_batch_days(batch_id, path=db_path)[0]
    assert batch_day.wide_image_id == "img_wide001"
    assert batch_day.wide_stage == "drafted"

    items = {
        item.panel: item for item in batch_store.get_batch_items(batch_id, path=db_path)
    }
    assert items["43L"].stage == "pending"
    assert items["43R"].stage == "pending"
    assert items["43L"].image_id is None


def test_record_item_attempt_persists_policy_rejection_flag(tmp_path):
    db_path = tmp_path / "batch.sqlite3"
    days = [
        ApprovedDay(
            day_index=1,
            mode="independiente",
            sub_group="Sub-grupo A",
            prompts={"43L": "a", "43R": "b", "50": "c"},
        )
    ]
    batch_id = batch_store.materialize_batch("Tema", days, path=db_path)

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
    # Sin pasar policy_rejection explícito -- debe defaultear a False, sin
    # perturbar el resto de los campos ya escritos por el intento anterior.
    batch_store.record_item_attempt(
        batch_id,
        1,
        "43R",
        attempts=1,
        stage="needs_attention",
        image_id=None,
        error="fallo técnico transitorio",
        path=db_path,
    )

    items = {
        item.panel: item for item in batch_store.get_batch_items(batch_id, path=db_path)
    }
    assert items["43L"].policy_rejection is True
    assert items["43R"].policy_rejection is False


def _materialize_split_day(db_path, day_index=1):
    days = [
        ApprovedDay(
            day_index=day_index,
            mode="split",
            sub_group="Sub-grupo A",
            prompts={"wide": "un horizonte compartido", "50": "otra escena"},
        )
    ]
    return batch_store.materialize_batch("Tema", days, path=db_path)


def test_record_split_day_outcome_writes_both_panels_and_wide_atomically(tmp_path):
    db_path = tmp_path / "batch.sqlite3"
    batch_id = _materialize_split_day(db_path)

    batch_store.record_split_day_outcome(
        batch_id,
        1,
        panel_43l=PanelOutcome(attempts=1, stage="drafted"),
        panel_43r=PanelOutcome(attempts=1, stage="drafted"),
        wide=WideOutcome(wide_image_id="img_wide001", wide_stage="drafted"),
        path=db_path,
    )

    items = {
        item.panel: item for item in batch_store.get_batch_items(batch_id, path=db_path)
    }
    assert items["43L"].stage == "drafted"
    assert items["43R"].stage == "drafted"
    assert items["43L"].attempts == 1
    assert items["43R"].attempts == 1

    day = batch_store.get_batch_days(batch_id, path=db_path)[0]
    assert day.wide_image_id == "img_wide001"
    assert day.wide_stage == "drafted"


def test_record_split_day_outcome_with_wide_none_leaves_batch_day_untouched(tmp_path):
    db_path = tmp_path / "batch.sqlite3"
    batch_id = _materialize_split_day(db_path)
    batch_store.record_wide_image(
        batch_id, 1, wide_image_id="img_wide001", wide_stage="finalized", path=db_path
    )

    batch_store.record_split_day_outcome(
        batch_id,
        1,
        panel_43l=PanelOutcome(attempts=1, stage="finalized", image_id="img_L"),
        panel_43r=PanelOutcome(attempts=1, stage="finalized", image_id="img_R"),
        wide=None,
        path=db_path,
    )

    items = {
        item.panel: item for item in batch_store.get_batch_items(batch_id, path=db_path)
    }
    assert items["43L"].image_id == "img_L"
    assert items["43R"].image_id == "img_R"

    # batch_day no se tocó -- sigue con el valor escrito antes de esta llamada.
    day = batch_store.get_batch_days(batch_id, path=db_path)[0]
    assert day.wide_image_id == "img_wide001"
    assert day.wide_stage == "finalized"


def test_record_split_day_outcome_rolls_back_fully_on_mid_transaction_failure(
    tmp_path,
):
    db_path = tmp_path / "batch.sqlite3"
    batch_id = _materialize_split_day(db_path)

    # `stage` es NOT NULL -- forzar una violación real de SQLite a mitad de
    # la transacción (después de que 43L ya se "escribió" dentro de la misma
    # transacción, antes de commitear) para confirmar que el fallo revierte
    # TODAS las filas, no solo dejarlas a medio escribir.
    with pytest.raises(sqlite3.IntegrityError):
        batch_store.record_split_day_outcome(
            batch_id,
            1,
            panel_43l=PanelOutcome(attempts=1, stage="drafted"),
            panel_43r=PanelOutcome(attempts=1, stage=None),  # type: ignore[arg-type]
            wide=WideOutcome(wide_image_id="img_wide001", wide_stage="drafted"),
            path=db_path,
        )

    items = {
        item.panel: item for item in batch_store.get_batch_items(batch_id, path=db_path)
    }
    # Ninguna fila quedó modificada -- ni 43L (escrito primero en la misma
    # transacción), ni batch_day.
    assert items["43L"].stage == "pending"
    assert items["43L"].attempts == 0
    assert items["43R"].stage == "pending"

    day = batch_store.get_batch_days(batch_id, path=db_path)[0]
    assert day.wide_stage == "pending"


def _materialize_one_day_batch(db_path):
    days = [
        ApprovedDay(
            day_index=1,
            mode="independiente",
            sub_group="Grupo 1",
            prompts={"43L": "a", "43R": "b", "50": "c"},
        )
    ]
    return batch_store.materialize_batch("Tema", days, path=db_path)


def test_get_batch_chat_id_defaults_to_none_when_never_set(tmp_path):
    db_path = tmp_path / "batch.sqlite3"
    batch_id = _materialize_one_day_batch(db_path)

    batch = batch_store.get_batch(batch_id, path=db_path)

    assert batch.chat_id is None


def test_set_batch_chat_id_persists_and_is_read_back_via_get_batch(tmp_path):
    db_path = tmp_path / "batch.sqlite3"
    batch_id = _materialize_one_day_batch(db_path)

    batch_store.set_batch_chat_id(batch_id, 424242, path=db_path)

    batch = batch_store.get_batch(batch_id, path=db_path)
    assert batch.chat_id == 424242


def test_set_batch_status_updates_status_without_touching_other_columns(tmp_path):
    db_path = tmp_path / "batch.sqlite3"
    batch_id = _materialize_one_day_batch(db_path)

    batch_store.set_batch_status(batch_id, "running", path=db_path)

    batch = batch_store.get_batch(batch_id, path=db_path)
    assert batch.status == "running"
    assert batch.theme == "Tema"
    assert batch.day_count == 1


def test_list_non_terminal_batches_excludes_reported_and_includes_everything_else(
    tmp_path,
):
    db_path = tmp_path / "batch.sqlite3"
    materialized_id = _materialize_one_day_batch(db_path)
    running_id = _materialize_one_day_batch(db_path)
    reported_id = _materialize_one_day_batch(db_path)
    batch_store.set_batch_status(running_id, "running", path=db_path)
    batch_store.set_batch_status(reported_id, "reported", path=db_path)

    non_terminal_ids = {
        batch.batch_id for batch in batch_store.list_non_terminal_batches(path=db_path)
    }

    assert non_terminal_ids == {materialized_id, running_id}
    assert reported_id not in non_terminal_ids


def test_list_non_terminal_batches_on_empty_db_returns_empty_list(tmp_path):
    db_path = tmp_path / "batch.sqlite3"

    assert batch_store.list_non_terminal_batches(path=db_path) == []


def test_chat_id_column_migrates_onto_a_pre_existing_database_missing_it(tmp_path):
    db_path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE batch ("
        "batch_id TEXT PRIMARY KEY, "
        "theme TEXT NOT NULL, "
        "day_count INTEGER NOT NULL, "
        "status TEXT NOT NULL, "
        "schedule_config TEXT, "
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
        ")"
    )
    conn.execute(
        "INSERT INTO batch (batch_id, theme, day_count, status) "
        "VALUES ('batch_legacy', 'Tema viejo', 2, 'materialized')"
    )
    conn.commit()
    conn.close()

    batch = batch_store.get_batch("batch_legacy", path=db_path)

    assert batch is not None
    assert batch.chat_id is None
    assert batch_store.list_non_terminal_batches(path=db_path)[0].batch_id == (
        "batch_legacy"
    )
