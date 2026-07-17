"""Estado persistido del motor de galería por lotes (PRD §15.5, dev_plan
`dev_plan_phase_2.md` §2.1): tres tablas SQLite (`batch`, `batch_day`,
`batch_item`) más la función que materializa una agrupación+prompts ya
aprobados por el usuario en filas listas para que el corredor (Etapa 2,
`src/engine/batch.py`, iteraciones posteriores) las procese.

Módulo plano, sin dependencia de `samsungtvws`/ADK/Telegram — mismo
patrón de capas que `engine.deploy_history`: testeable en aislado con
`sqlite3` estándar, `path: Path | None = None` en cada función pública
para inyectar una base de datos temporal en tests.

Sin stage `approved` en `batch_item`/`wide_stage`: la aprobación del
lote (agrupación, prompts por sub-grupo, y la confirmación final del
lote completo, PRD §15.3 pasos 1-5+8) ya ocurrió en la conversación
antes de que exista una sola fila aquí — para cuando `materialize_batch`
corre, todo lo que persiste ya está aprobado. `needs_attention` es un
valor lateral alcanzable desde `pending` (generación agotó reintentos) o
`drafted` (finalización agotó reintentos), nunca un paso del camino
feliz: `pending -> drafted -> finalized -> uploaded`.

Nota de schema (día split): el PRD no reserva una columna para el
prompt de la imagen "wide" compartida por 43L/43R antes de partirse —
`materialize_batch` lo guarda duplicado en `batch_item.prompt` de ambas
filas físicas (43L y 43R), ya que describen la misma composición antes
del split; el corredor puede leer cualquiera de las dos para generar la
imagen ancha compartida una sola vez.
"""

import contextlib
import sqlite3
import tomllib
import uuid
from dataclasses import dataclass
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "batch.sqlite3"
CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "batch.toml"


@dataclass
class BatchConfig:
    generation_max_attempts: int
    tv_deploy_max_attempts: int


def load_batch_config(path: Path | None = None) -> BatchConfig:
    """Reads the batch engine's configurable retry ceilings from an
    editable TOML file (PRD §15.5)."""
    with (path or CONFIG_PATH).open("rb") as f:
        data = tomllib.load(f)
    return BatchConfig(**data)


@dataclass
class ApprovedDay:
    day_index: int
    mode: str
    sub_group: str
    prompts: dict[str, str]


@dataclass
class BatchRecord:
    batch_id: str
    theme: str
    day_count: int
    status: str
    schedule_config: str | None
    created_at: str


@dataclass
class BatchDayRecord:
    batch_id: str
    day_index: int
    mode: str
    sub_group: str
    wide_image_id: str | None
    wide_stage: str | None


@dataclass
class BatchItemRecord:
    batch_id: str
    day_index: int
    panel: str
    prompt: str
    stage: str
    image_id: str | None
    attempts: int
    error: str | None
    updated_at: str | None


def _new_batch_id() -> str:
    return f"batch_{uuid.uuid4().hex[:8]}"


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS batch ("
        "batch_id TEXT PRIMARY KEY, "
        "theme TEXT NOT NULL, "
        "day_count INTEGER NOT NULL, "
        "status TEXT NOT NULL, "
        "schedule_config TEXT, "
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS batch_day ("
        "batch_id TEXT NOT NULL, "
        "day_index INTEGER NOT NULL, "
        "mode TEXT NOT NULL, "
        "sub_group TEXT NOT NULL, "
        "wide_image_id TEXT, "
        "wide_stage TEXT, "
        "PRIMARY KEY (batch_id, day_index)"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS batch_item ("
        "batch_id TEXT NOT NULL, "
        "day_index INTEGER NOT NULL, "
        "panel TEXT NOT NULL, "
        "prompt TEXT NOT NULL, "
        "stage TEXT NOT NULL, "
        "image_id TEXT, "
        "attempts INTEGER NOT NULL DEFAULT 0, "
        "error TEXT, "
        "updated_at TEXT, "
        "PRIMARY KEY (batch_id, day_index, panel)"
        ")"
    )
    return conn


def materialize_batch(
    theme: str,
    days: list[ApprovedDay],
    schedule_config: str | None = None,
    path: Path | None = None,
) -> str:
    """Persiste un lote ya aprobado (agrupación + prompts por sub-grupo +
    confirmación del lote completo, PRD §15.3 pasos 1-5+8) como un nuevo
    `batch_id`, una fila `batch_day` por día, y las filas `batch_item`
    correspondientes (3 por día, siempre — un registro por panel físico
    desplegable, sin importar el modo): para un día 'independiente' los
    prompts vienen directo de `prompts['43L']/['43R']/['50']`; para un
    día 'split' se usa `prompts['wide']` para las filas 43L y 43R (misma
    composición antes de partirse, ver nota de módulo) y
    `prompts['50']` para la fila 50, y se marca la fila `batch_day` con
    `wide_stage='pending'`. Todo `batch_item` arranca en
    `stage='pending'`, `attempts=0`. El batch arranca en
    `status='materialized'`. Devuelve el `batch_id` nuevo.
    """
    batch_id = _new_batch_id()
    with contextlib.closing(_connect(path or DB_PATH)) as conn, conn:
        conn.execute(
            "INSERT INTO batch (batch_id, theme, day_count, status, schedule_config) "
            "VALUES (?, ?, ?, 'materialized', ?)",
            (batch_id, theme, len(days), schedule_config),
        )
        for day in days:
            wide_stage = "pending" if day.mode == "split" else None
            conn.execute(
                "INSERT INTO batch_day "
                "(batch_id, day_index, mode, sub_group, wide_image_id, wide_stage) "
                "VALUES (?, ?, ?, ?, NULL, ?)",
                (batch_id, day.day_index, day.mode, day.sub_group, wide_stage),
            )
            if day.mode == "split":
                wide_prompt = day.prompts["wide"]
                panel_prompts = {
                    "43L": wide_prompt,
                    "43R": wide_prompt,
                    "50": day.prompts["50"],
                }
            else:
                panel_prompts = {
                    "43L": day.prompts["43L"],
                    "43R": day.prompts["43R"],
                    "50": day.prompts["50"],
                }
            for panel, prompt in panel_prompts.items():
                conn.execute(
                    "INSERT INTO batch_item "
                    "(batch_id, day_index, panel, prompt, stage, attempts) "
                    "VALUES (?, ?, ?, ?, 'pending', 0)",
                    (batch_id, day.day_index, panel, prompt),
                )
    return batch_id


def get_batch(batch_id: str, path: Path | None = None) -> BatchRecord | None:
    with contextlib.closing(_connect(path or DB_PATH)) as conn, conn:
        row = conn.execute(
            "SELECT batch_id, theme, day_count, status, schedule_config, created_at "
            "FROM batch WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()
    if row is None:
        return None
    return BatchRecord(*row)


def get_batch_days(batch_id: str, path: Path | None = None) -> list[BatchDayRecord]:
    with contextlib.closing(_connect(path or DB_PATH)) as conn, conn:
        rows = conn.execute(
            "SELECT batch_id, day_index, mode, sub_group, wide_image_id, wide_stage "
            "FROM batch_day WHERE batch_id = ? ORDER BY day_index",
            (batch_id,),
        ).fetchall()
    return [BatchDayRecord(*row) for row in rows]


def get_batch_items(batch_id: str, path: Path | None = None) -> list[BatchItemRecord]:
    with contextlib.closing(_connect(path or DB_PATH)) as conn, conn:
        rows = conn.execute(
            "SELECT batch_id, day_index, panel, prompt, stage, image_id, "
            "attempts, error, updated_at "
            "FROM batch_item WHERE batch_id = ? ORDER BY day_index, panel",
            (batch_id,),
        ).fetchall()
    return [BatchItemRecord(*row) for row in rows]
