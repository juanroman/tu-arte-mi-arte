"""Puntero token -> ids de la vista previa mostrada en el chat (PRD §7.2,
Etapa 2 iteración 2.4). Módulo plano, sin dependencia de ADK ni de
`python-telegram-bot` (mismo patrón que `session_store.py`): testeable en
aislado, con `sqlite3` estándar.

Guarda, junto a los tres image_id del conjunto (43L/43R/50), el
`session_id` de ADK activo al momento de componer la vista previa —
necesario para rechazar la confirmación si la sesión del chat ya rotó
(`/nuevo`, botón, timeout) para cuando el usuario toca "Confirmar": el
mensaje sintético de aprobación no debe caer en una sesión nueva que no
tiene memoria de haber producido esos ids (ver nota de diseño en
`telegram_bot.py::confirm_handler`).

Vive en el mismo archivo SQLite que `session_store` (`data/bot_state.sqlite3`,
tabla separada) porque es igualmente "estado propio del bot", no del
esquema interno de ADK.
"""

import contextlib
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "bot_state.sqlite3"


@dataclass
class Preview:
    chat_id: int
    session_id: str
    image_43l: str
    image_43r: str
    image_50: str
    created_at: float


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS preview_tokens ("
        "token TEXT PRIMARY KEY, "
        "chat_id INTEGER NOT NULL, "
        "session_id TEXT NOT NULL, "
        "image_43l TEXT NOT NULL, "
        "image_43r TEXT NOT NULL, "
        "image_50 TEXT NOT NULL, "
        "created_at REAL NOT NULL"
        ")"
    )
    return conn


def new_token() -> str:
    return uuid.uuid4().hex[:12]


def save_preview(
    token: str,
    chat_id: int,
    session_id: str,
    image_43l: str,
    image_43r: str,
    image_50: str,
    created_at: float,
    path: Path | None = None,
) -> None:
    with contextlib.closing(_connect(path or DB_PATH)) as conn, conn:
        conn.execute(
            "INSERT INTO preview_tokens "
            "(token, chat_id, session_id, image_43l, image_43r, image_50, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(token) DO UPDATE SET "
            "chat_id = excluded.chat_id, "
            "session_id = excluded.session_id, "
            "image_43l = excluded.image_43l, "
            "image_43r = excluded.image_43r, "
            "image_50 = excluded.image_50, "
            "created_at = excluded.created_at",
            (token, chat_id, session_id, image_43l, image_43r, image_50, created_at),
        )


def get_preview(token: str, path: Path | None = None) -> Preview | None:
    with contextlib.closing(_connect(path or DB_PATH)) as conn, conn:
        row = conn.execute(
            "SELECT chat_id, session_id, image_43l, image_43r, image_50, created_at "
            "FROM preview_tokens WHERE token = ?",
            (token,),
        ).fetchone()
    if row is None:
        return None
    return Preview(
        chat_id=row[0],
        session_id=row[1],
        image_43l=row[2],
        image_43r=row[3],
        image_50=row[4],
        created_at=row[5],
    )
