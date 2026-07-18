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

Nota de migración (dev_plan_phase_2.md §2.4): `batch_item.policy_rejection`
se agregó después de que ya existían bases de datos reales en disco
(`data/batch.sqlite3`, gitignored, con lotes materializados/finalizados de
2.1-2.3) -- `_connect` aplica un `ALTER TABLE` guardado tras el `CREATE
TABLE IF NOT EXISTS` para que esas bases existentes ganen la columna
nueva sin perder sus filas, en vez de exigir borrar y re-crear el archivo.
Primer caso de migración de este módulo; el mismo patrón aplica si un
campo futuro necesita agregarse a una tabla ya poblada.

Nota de migración (dev_plan_phase_2.md §3.3): `batch.chat_id` (nullable)
se agregó con el mismo patrón guardado que `policy_rejection` -- las filas
de `batch` creadas antes de esta iteración quedan con `chat_id=NULL`
(nunca hubo forma de saberlo, el chat vivía solo como parámetro de función
mientras duraba la conversación de Telegram que confirmó el lote). No hay
backfill automático que infiera un `chat_id` retroactivo: la reconciliación
al reiniciar (`telegram_bot.reconcile_batches_on_startup`) trata un lote no
terminal sin `chat_id` como un caso legado -- avisa por broadcast a los
usuarios permitidos en vez de intentar reanudarlo, porque no hay a quién
reportarle el resultado.

Nota de migración (revisión posterior a 3.3, hallazgo de code review):
`batch.report_text_sent`/`report_albums_sent` (mismo patrón guardado que
las dos migraciones anteriores) hacen que `telegram_bot._send_batch_report`
sea reanudable: si el proceso muere a mitad del envío del reporte
proactivo (texto ya mandado, álbum 2 de 3 falla por un error real de red
de Telegram), `batch.status` se queda en `'running'` y una reconciliación
posterior reinvoca `_send_batch_report` desde cero -- sin este progreso
persistido, esa reinvocación repetiría el texto y TODOS los álbumes,
duplicando lo que el usuario ya había recibido antes de la falla.

Nota (dev_plan_phase_2.md §4.1): la etapa de subida a TV
(`engine.batch.run_upload_stage`) escribe `stage='uploaded'` reutilizando
`record_item_attempt` tal cual -- no se agregó un `record_item_upload`
dedicado ni una columna `content_id`, porque el shape existente (una
fila, `image_id` opcional pasado sin producir uno nuevo, `policy_rejection`
en su default `False`, ya que una TV no rechaza contenido por política)
ya cubre el caso completo sin necesidad de una función casi-idéntica.
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
    draft_seconds_per_call: int
    finalize_seconds_per_call: int
    deploy_seconds_per_day: int
    eta_safety_margin: float
    rotation_duration_minutes: int
    rotation_shuffle: bool


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
    chat_id: int | None = None


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
    policy_rejection: bool


@dataclass
class PanelOutcome:
    """Resultado de un intento sobre un panel físico (43L/43R), tal como lo
    escribiría `record_item_attempt` -- usado por `record_split_day_outcome`
    (§2.5) para describir ambos paneles de un día split antes de escribirlos
    en una sola transacción.
    """

    attempts: int
    stage: str
    image_id: str | None = None
    error: str | None = None
    policy_rejection: bool = False


@dataclass
class WideOutcome:
    """Resultado de un intento sobre la imagen ancha compartida de un día
    split, tal como lo escribiría `record_wide_image` -- usado por
    `record_split_day_outcome` (§2.5).
    """

    wide_image_id: str | None
    wide_stage: str


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
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "chat_id INTEGER"
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
    with contextlib.suppress(sqlite3.OperationalError):
        conn.execute(
            "ALTER TABLE batch_item "
            "ADD COLUMN policy_rejection INTEGER NOT NULL DEFAULT 0"
        )
    with contextlib.suppress(sqlite3.OperationalError):
        conn.execute("ALTER TABLE batch ADD COLUMN chat_id INTEGER")
    with contextlib.suppress(sqlite3.OperationalError):
        conn.execute(
            "ALTER TABLE batch "
            "ADD COLUMN report_text_sent INTEGER NOT NULL DEFAULT 0"
        )
    with contextlib.suppress(sqlite3.OperationalError):
        conn.execute(
            "ALTER TABLE batch ADD COLUMN report_albums_sent INTEGER NOT NULL DEFAULT 0"
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
            "SELECT batch_id, theme, day_count, status, schedule_config, "
            "created_at, chat_id FROM batch WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()
    if row is None:
        return None
    return BatchRecord(*row)


def list_non_terminal_batches(path: Path | None = None) -> list[BatchRecord]:
    """Lotes cuyo `status` todavía no es `'reported'` (dev_plan_phase_2.md
    §3.3, requisito duro #6) -- incluye tanto los recién materializados
    (`'materialized'`) como los que ya arrancaron a correr pero el proceso
    murió antes de que el reporte final se mandara (`'running'`). Al
    arrancar, el bot reconcilia cada uno de estos: los reanuda si conoce su
    `chat_id`, o avisa por broadcast si no (lote de antes de esta
    iteración, ver nota de módulo sobre migración).
    """
    with contextlib.closing(_connect(path or DB_PATH)) as conn, conn:
        rows = conn.execute(
            "SELECT batch_id, theme, day_count, status, schedule_config, "
            "created_at, chat_id FROM batch WHERE status != 'reported' "
            "ORDER BY created_at"
        ).fetchall()
    return [BatchRecord(*row) for row in rows]


def set_batch_chat_id(batch_id: str, chat_id: int, path: Path | None = None) -> None:
    """Persiste a qué chat de Telegram pertenece un lote, en el momento en
    que se confirma (dev_plan_phase_2.md §3.3) -- es lo único que permite a
    la reconciliación al reiniciar saber a dónde reportar un lote que
    quedó a medias tras un crash del proceso.
    """
    with contextlib.closing(_connect(path or DB_PATH)) as conn, conn:
        conn.execute(
            "UPDATE batch SET chat_id = ? WHERE batch_id = ?", (chat_id, batch_id)
        )


def set_batch_status(batch_id: str, status: str, path: Path | None = None) -> None:
    """Actualiza `batch.status` (dev_plan_phase_2.md §3.3): `'materialized'`
    (recién confirmado) -> `'running'` (el corredor de fondo arrancó,
    fresco o reanudado tras un reinicio, da igual) -> `'reported'`
    (terminal, el reporte proactivo final ya se mandó). Un lote nunca
    "falla" a este nivel -- las fallas ya viven por `batch_item`
    (`needs_attention`), y el corredor siempre termina en un estado
    reportable, así que no existe un tercer valor terminal de error.
    """
    with contextlib.closing(_connect(path or DB_PATH)) as conn, conn:
        conn.execute(
            "UPDATE batch SET status = ? WHERE batch_id = ?", (status, batch_id)
        )


def get_batch_report_progress(
    batch_id: str, path: Path | None = None
) -> tuple[bool, int]:
    """Devuelve `(report_text_sent, report_albums_sent)` -- cuánto del
    reporte proactivo final (`telegram_bot._send_batch_report`, §3.2) ya
    se entregó con éxito para este lote. Hace reanudable el envío del
    reporte: si el proceso muere a mitad de camino (p. ej. el texto ya se
    mandó pero el segundo de tres álbumes falla por un error real de red),
    una reinvocación posterior (disparada por `reconcile_batches_on_startup`
    tras un reinicio) puede saltarse lo ya entregado en vez de repetirlo
    desde cero -- hallazgo de code review posterior a 3.3: antes de esto,
    `_send_batch_report` no persistía ningún progreso y duplicaba todo el
    reporte en cada reintento.
    """
    with contextlib.closing(_connect(path or DB_PATH)) as conn, conn:
        row = conn.execute(
            "SELECT report_text_sent, report_albums_sent FROM batch "
            "WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()
    if row is None:
        return (False, 0)
    return (bool(row[0]), row[1])


def mark_batch_report_text_sent(batch_id: str, path: Path | None = None) -> None:
    """Marca que el mensaje de texto del reporte proactivo ya se mandó
    (§3.2) -- una reinvocación de `_send_batch_report` no debe repetirlo.
    """
    with contextlib.closing(_connect(path or DB_PATH)) as conn, conn:
        conn.execute(
            "UPDATE batch SET report_text_sent = 1 WHERE batch_id = ?", (batch_id,)
        )


def mark_batch_report_album_sent(batch_id: str, path: Path | None = None) -> None:
    """Incrementa el conteo de álbumes ya entregados del reporte proactivo
    (§3.2), en el orden en que `_batch_report_albums` los produce -- una
    reinvocación de `_send_batch_report` usa este conteo para saltarse los
    álbumes ya mandados y solo continuar con el resto.
    """
    with contextlib.closing(_connect(path or DB_PATH)) as conn, conn:
        conn.execute(
            "UPDATE batch SET report_albums_sent = report_albums_sent + 1 "
            "WHERE batch_id = ?",
            (batch_id,),
        )


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
            "attempts, error, updated_at, policy_rejection "
            "FROM batch_item WHERE batch_id = ? ORDER BY day_index, panel",
            (batch_id,),
        ).fetchall()
    return [
        BatchItemRecord(
            batch_id=row[0],
            day_index=row[1],
            panel=row[2],
            prompt=row[3],
            stage=row[4],
            image_id=row[5],
            attempts=row[6],
            error=row[7],
            updated_at=row[8],
            policy_rejection=bool(row[9]),
        )
        for row in rows
    ]


def _execute_item_update(
    conn: sqlite3.Connection,
    batch_id: str,
    day_index: int,
    panel: str,
    *,
    attempts: int,
    stage: str,
    image_id: str | None,
    error: str | None,
    policy_rejection: bool,
) -> None:
    conn.execute(
        "UPDATE batch_item SET attempts = ?, stage = ?, image_id = ?, "
        "error = ?, policy_rejection = ?, updated_at = CURRENT_TIMESTAMP "
        "WHERE batch_id = ? AND day_index = ? AND panel = ?",
        (
            attempts,
            stage,
            image_id,
            error,
            int(policy_rejection),
            batch_id,
            day_index,
            panel,
        ),
    )


def _execute_wide_update(
    conn: sqlite3.Connection,
    batch_id: str,
    day_index: int,
    *,
    wide_image_id: str | None,
    wide_stage: str,
) -> None:
    conn.execute(
        "UPDATE batch_day SET wide_image_id = ?, wide_stage = ? "
        "WHERE batch_id = ? AND day_index = ?",
        (wide_image_id, wide_stage, batch_id, day_index),
    )


def record_item_attempt(
    batch_id: str,
    day_index: int,
    panel: str,
    *,
    attempts: int,
    stage: str,
    image_id: str | None = None,
    error: str | None = None,
    policy_rejection: bool = False,
    path: Path | None = None,
) -> None:
    """Persiste el resultado de un intento de generación/finalización sobre
    un `batch_item` (corredor, Etapa 2). Siempre escribe el estado completo
    resultante del intento -- no una actualización parcial -- para que un
    reinicio a medias del corredor pueda reanudar leyendo `attempts` sin
    ambigüedad sobre qué campos quedaron a medio escribir. `policy_rejection`
    persiste tal cual la clave homónima de `engine.generation` (§7.9) para
    que un reporte de lote (dev_plan_phase_2.md §2.4) distinga un rechazo de
    política de una falla técnica agotada sin tener que inferirlo del texto
    de `error`.

    Escribe una sola fila -- ya atómico por sí solo. Para un día split, donde
    un intento se escribe en varias filas a la vez (43L/43R y opcionalmente
    `batch_day`), usar `record_split_day_outcome` (§2.5) en su lugar: llamar
    esta función varias veces seguidas para el mismo intento deja una ventana
    real de inconsistencia ante un crash de proceso a la mitad.
    """
    with contextlib.closing(_connect(path or DB_PATH)) as conn, conn:
        _execute_item_update(
            conn,
            batch_id,
            day_index,
            panel,
            attempts=attempts,
            stage=stage,
            image_id=image_id,
            error=error,
            policy_rejection=policy_rejection,
        )


def record_wide_image(
    batch_id: str,
    day_index: int,
    *,
    wide_image_id: str | None,
    wide_stage: str,
    path: Path | None = None,
) -> None:
    """Persiste el resultado de un intento sobre la imagen ancha compartida
    de un día split (`batch_day.wide_image_id`/`wide_stage`) -- mismo
    principio de escritura completa que `record_item_attempt`. Ver la misma
    advertencia sobre `record_split_day_outcome` cuando este intento también
    implica escribir 43L/43R en la misma operación lógica.
    """
    with contextlib.closing(_connect(path or DB_PATH)) as conn, conn:
        _execute_wide_update(
            conn,
            batch_id,
            day_index,
            wide_image_id=wide_image_id,
            wide_stage=wide_stage,
        )


def record_split_day_outcome(
    batch_id: str,
    day_index: int,
    *,
    panel_43l: PanelOutcome,
    panel_43r: PanelOutcome,
    wide: WideOutcome | None = None,
    path: Path | None = None,
) -> None:
    """Persiste en una sola transacción SQLite el resultado de un intento
    sobre un día split que toca varias filas a la vez: los dos `batch_item`
    físicos (43L/43R) y, si `wide` no es `None`, también `batch_day.wide_image_id`/
    `wide_stage` (§2.5, requisito duro #5).

    Existe porque escribir estas filas con llamadas separadas
    (`record_item_attempt` × 2 + `record_wide_image`, cada una su propia
    transacción) deja una ventana real de inconsistencia ante un crash de
    proceso a la mitad: los paneles pueden quedar en `drafted`/`needs_attention`
    mientras `wide_stage` sigue en `pending` (una reinvocación no reconoce el
    trabajo ya hecho y vuelve a llamar al modelo -- gasto duplicado, y si fue
    un `policy_rejection`, lo reintenta, violando el requisito duro #1), o
    43R puede quedar huérfano si el proceso muere justo después de escribir
    43L. Con una sola transacción, un crash a mitad de la operación dejará
    todas las filas sin tocar (rollback) en vez de a medio escribir.

    `wide=None` cuando el intento solo re-escribe los paneles físicos porque
    la fuente ancha ya se finalizó en un paso previo (ya atómico por sí solo,
    de una sola fila) -- no toca `batch_day`.
    """
    with contextlib.closing(_connect(path or DB_PATH)) as conn, conn:
        _execute_item_update(
            conn,
            batch_id,
            day_index,
            "43L",
            attempts=panel_43l.attempts,
            stage=panel_43l.stage,
            image_id=panel_43l.image_id,
            error=panel_43l.error,
            policy_rejection=panel_43l.policy_rejection,
        )
        _execute_item_update(
            conn,
            batch_id,
            day_index,
            "43R",
            attempts=panel_43r.attempts,
            stage=panel_43r.stage,
            image_id=panel_43r.image_id,
            error=panel_43r.error,
            policy_rejection=panel_43r.policy_rejection,
        )
        if wide is not None:
            _execute_wide_update(
                conn,
                batch_id,
                day_index,
                wide_image_id=wide.wide_image_id,
                wide_stage=wide.wide_stage,
            )
