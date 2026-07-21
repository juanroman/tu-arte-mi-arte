"""Historial de despliegue por TV (PRD §7.6, dev_plan §3.5): guarda solo el
image_id actual y el inmediatamente anterior por pantalla, para poder
revertir un despliegue indeseado o parcial. Un solo nivel de historial (no
una pila) — revertir dos veces seguidas alterna entre las dos últimas
versiones, que es lo que el caso de uso real pide.

Módulo plano, sin dependencia de `samsungtvws`/ADK/Telegram (mismo patrón
de capas que `src/bot/session_store.py`/`preview_store.py`): testeable en
aislado, con `sqlite3` estándar. Vive en `src/engine/` (no en `src/bot/`)
porque es estado del dominio de despliegue a TVs, reusable sin importar la
interfaz que lo dispare (Telegram, un comando, o el agente).
"""

import contextlib
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

DB_PATH = (
    Path(__file__).resolve().parent.parent.parent / "data" / "tv_deploy_history.sqlite3"
)

# Guards the read-modify-write below: record_deploy reads the current row
# and writes the shifted current/previous pair as two separate connections,
# not one transaction. Without serializing, two concurrent calls for the
# same tv_name (a deploy and a revert issued in quick succession) can both
# read the same stale snapshot and each commit a write based on it -- the
# second commit silently discards whichever image_id the first call was
# about to shift into `previous`.
_record_deploy_lock = threading.Lock()


@dataclass
class DeployHistory:
    current_image_id: str | None
    previous_image_id: str | None


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS tv_deploy_history ("
        "tv_name TEXT PRIMARY KEY, "
        "current_image_id TEXT, "
        "previous_image_id TEXT"
        ")"
    )
    return conn


def get_history(tv_name: str, path: Path | None = None) -> DeployHistory | None:
    with contextlib.closing(_connect(path or DB_PATH)) as conn, conn:
        row = conn.execute(
            "SELECT current_image_id, previous_image_id "
            "FROM tv_deploy_history WHERE tv_name = ?",
            (tv_name,),
        ).fetchone()
    if row is None:
        return None
    return DeployHistory(current_image_id=row[0], previous_image_id=row[1])


def record_deploy(tv_name: str, image_id: str, path: Path | None = None) -> None:
    """Registra `image_id` como el nuevo `current` de `tv_name`, desplazando
    el `current` anterior (si había uno) a `previous`. El read-modify-write
    (leer el `current` vigente, luego escribirlo desplazado a `previous`) se
    serializa con un lock: dos llamadas concurrentes para el mismo
    `tv_name` (p. ej. un deploy y un revert seguidos, o un doble-tap en
    Telegram) podrían, sin el lock, leer el mismo snapshot y hacer que la
    segunda en escribir pise silenciosamente el resultado de la primera.
    """
    with _record_deploy_lock:
        existing = get_history(tv_name, path)
        new_previous = existing.current_image_id if existing else None
        with contextlib.closing(_connect(path or DB_PATH)) as conn, conn:
            conn.execute(
                "INSERT INTO tv_deploy_history "
                "(tv_name, current_image_id, previous_image_id) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(tv_name) DO UPDATE SET "
                "current_image_id = excluded.current_image_id, "
                "previous_image_id = excluded.previous_image_id",
                (tv_name, image_id, new_previous),
            )
