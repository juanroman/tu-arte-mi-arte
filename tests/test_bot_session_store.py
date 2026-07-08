import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bot import session_store  # noqa: E402


def test_get_current_session_returns_none_when_absent(tmp_path):
    db_path = tmp_path / "bot_state.sqlite3"

    assert session_store.get_current_session(42, path=db_path) is None


def test_set_then_get_current_session_roundtrips(tmp_path):
    db_path = tmp_path / "bot_state.sqlite3"

    session_store.set_current_session(42, "42-abc", 123.0, path=db_path)
    session = session_store.get_current_session(42, path=db_path)

    assert session.session_id == "42-abc"
    assert session.last_activity == 123.0


def test_set_current_session_overwrites_existing_pointer(tmp_path):
    db_path = tmp_path / "bot_state.sqlite3"

    session_store.set_current_session(42, "42-abc", 100.0, path=db_path)
    session_store.set_current_session(42, "42-def", 200.0, path=db_path)
    session = session_store.get_current_session(42, path=db_path)

    assert session.session_id == "42-def"
    assert session.last_activity == 200.0


def test_two_chats_have_independent_pointers(tmp_path):
    db_path = tmp_path / "bot_state.sqlite3"

    session_store.set_current_session(1, "1-abc", 100.0, path=db_path)
    session_store.set_current_session(2, "2-abc", 100.0, path=db_path)

    assert session_store.get_current_session(1, path=db_path).session_id == "1-abc"
    assert session_store.get_current_session(2, path=db_path).session_id == "2-abc"


def test_new_session_id_is_unique_and_includes_chat_id():
    first = session_store.new_session_id(42)
    second = session_store.new_session_id(42)

    assert first != second
    assert first.startswith("42-")
    assert second.startswith("42-")


def test_store_persists_across_separate_connections(tmp_path):
    db_path = tmp_path / "bot_state.sqlite3"

    session_store.set_current_session(42, "42-abc", time.time(), path=db_path)

    reopened = session_store.get_current_session(42, path=db_path)
    assert reopened is not None
    assert reopened.session_id == "42-abc"


def test_get_current_session_closes_its_connection(tmp_path, monkeypatch):
    """`_connect` opens a fresh sqlite3.Connection per call; every public
    function must close it before returning instead of relying on GC to
    reclaim the file handle eventually."""
    db_path = tmp_path / "bot_state.sqlite3"
    opened_connections = []
    real_connect = sqlite3.connect

    def tracking_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        opened_connections.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, "connect", tracking_connect)

    session_store.get_current_session(42, path=db_path)

    assert len(opened_connections) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        opened_connections[0].execute("SELECT 1")


def test_set_current_session_closes_its_connection(tmp_path, monkeypatch):
    db_path = tmp_path / "bot_state.sqlite3"
    opened_connections = []
    real_connect = sqlite3.connect

    def tracking_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        opened_connections.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, "connect", tracking_connect)

    session_store.set_current_session(42, "42-abc", 100.0, path=db_path)

    assert len(opened_connections) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        opened_connections[0].execute("SELECT 1")
