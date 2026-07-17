import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from engine import batch_store  # noqa: E402
from engine.batch_store import ApprovedDay  # noqa: E402


def test_load_batch_config_reads_retry_ceilings():
    config = batch_store.load_batch_config()

    assert config.generation_max_attempts == 2
    assert config.tv_deploy_max_attempts == 3


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
