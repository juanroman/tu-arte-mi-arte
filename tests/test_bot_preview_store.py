import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bot import preview_store  # noqa: E402


def test_get_preview_returns_none_when_absent(tmp_path):
    db_path = tmp_path / "bot_state.sqlite3"

    assert preview_store.get_preview("missing", path=db_path) is None


def test_save_then_get_preview_roundtrips(tmp_path):
    db_path = tmp_path / "bot_state.sqlite3"

    preview_store.save_preview(
        "tok1", 42, "42-abc", "img_l", "img_r", "img_50", 123.0, path=db_path
    )
    preview = preview_store.get_preview("tok1", path=db_path)

    assert preview.chat_id == 42
    assert preview.session_id == "42-abc"
    assert preview.image_43l == "img_l"
    assert preview.image_43r == "img_r"
    assert preview.image_50 == "img_50"
    assert preview.created_at == 123.0


def test_new_token_is_unique():
    first = preview_store.new_token()
    second = preview_store.new_token()

    assert first != second


def test_two_tokens_have_independent_rows(tmp_path):
    db_path = tmp_path / "bot_state.sqlite3"

    preview_store.save_preview(
        "tok1", 1, "1-abc", "img_a1", "img_a2", "img_a3", 100.0, path=db_path
    )
    preview_store.save_preview(
        "tok2", 2, "2-abc", "img_b1", "img_b2", "img_b3", 100.0, path=db_path
    )

    assert preview_store.get_preview("tok1", path=db_path).chat_id == 1
    assert preview_store.get_preview("tok2", path=db_path).chat_id == 2


def test_store_persists_across_separate_connections(tmp_path):
    db_path = tmp_path / "bot_state.sqlite3"

    preview_store.save_preview(
        "tok1", 42, "42-abc", "img_l", "img_r", "img_50", time.time(), path=db_path
    )

    reopened = preview_store.get_preview("tok1", path=db_path)
    assert reopened is not None
    assert reopened.image_43l == "img_l"
