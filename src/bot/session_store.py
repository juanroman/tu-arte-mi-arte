"""Puntero persistente `chat_id -> session_id` de ADK actual (§7.2,
Etapa 2 iteración 2.3). Módulo plano, sin dependencia de ADK ni de
`python-telegram-bot` (mismo patrón de capas que `src/engine/*`):
testeable en aislado, con `sqlite3` estándar.

Vive en un archivo SQLite separado del que usa `DatabaseSessionService`
para no acoplarnos al esquema interno de ADK, que migra entre versiones.
"""

import contextlib
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "bot_state.sqlite3"


@dataclass
class ChatSession:
    session_id: str
    last_activity: float


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS chat_sessions ("
        "chat_id INTEGER PRIMARY KEY, "
        "session_id TEXT NOT NULL, "
        "last_activity REAL NOT NULL"
        ")"
    )
    return conn


def get_current_session(chat_id: int, path: Path | None = None) -> ChatSession | None:
    with contextlib.closing(_connect(path or DB_PATH)) as conn, conn:
        row = conn.execute(
            "SELECT session_id, last_activity FROM chat_sessions WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
    if row is None:
        return None
    return ChatSession(session_id=row[0], last_activity=row[1])


def set_current_session(
    chat_id: int, session_id: str, last_activity: float, path: Path | None = None
) -> None:
    with contextlib.closing(_connect(path or DB_PATH)) as conn, conn:
        conn.execute(
            "INSERT INTO chat_sessions (chat_id, session_id, last_activity) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET "
            "session_id = excluded.session_id, "
            "last_activity = excluded.last_activity",
            (chat_id, session_id, last_activity),
        )


def new_session_id(chat_id: int) -> str:
    return f"{chat_id}-{uuid.uuid4().hex}"
