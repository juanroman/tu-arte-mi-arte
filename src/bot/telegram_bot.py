"""Telegram bot con lista blanca (PRD §7.1, §9) conectado al agente ADK
(§7.11, Etapa 2 iteración 2.2). Cada mensaje autorizado se mapea a una
llamada al `Runner` con el `root_agent` construido en Etapa 1 — mismas
tools, sin reescribir nada.

Sesión persistente en SQLite (PRD §7.2, Etapa 2 iteración 2.3): un
`chat_id` de Telegram se liga a un `(user_id, session_id)` de ADK que
sobrevive a reinicios del bot. El puntero "cuál es la sesión actual de
este chat" vive en `session_store` (SQLite propio, separado del que usa
`DatabaseSessionService`), y rota por comando `/nuevo`, por el botón
persistente "🔄 Empezar de cero", o por timeout de inactividad.

Preview compuesto y confirmación (PRD §7.2/§8, Etapa 2 iteración 2.4): el
preview se manda como foto con un botón inline "Confirmar" atado al
image_id exacto de esa generación (`preview_store`), feedback de progreso
continuo mientras el agente trabaja (los tool calls de generación tardan
minutos), y las respuestas de texto se convierten a MarkdownV2 real de
Telegram en vez de mostrar asteriscos crudos.

Reversibilidad (PRD §7.6, dev_plan §3.5): comando `/revertir [43L|43R|50]`
y un botón inline "↩️ Revertir cambios" que aparece automáticamente
cuando un despliegue queda parcial (algunas TVs sí, otras no) —ambos
actúan directo sobre `engine.tv_deploy`, sin pasar por el Runner de ADK,
porque son acciones de infraestructura deterministas, no correcciones
creativas.
"""

import asyncio
import contextlib
import logging
import os
import sys
import time
import warnings
from dataclasses import dataclass
from pathlib import Path

import telegramify_markdown
from dotenv import load_dotenv
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from google.genai import types
from sqlalchemy import event
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
)
from telegram.ext import filters as tg_filters

from agents.tu_arte_mi_arte.agent import root_agent
from bot import preview_store, session_store
from engine import batch_store, tv_deploy
from engine.batch import run_draft_stage, run_finalize_stage, summarize_batch
from engine.generation import IMAGES_DIR

_logger = logging.getLogger(__name__)

APP_NAME = "tu_arte_mi_arte"
RESET_BUTTON_TEXT = "🔄 Empezar de cero"
DEFAULT_SESSION_TIMEOUT_SECONDS = 10800  # 3h, sugerido por PRD §7.2
DEFAULT_LOG_LEVEL = logging.INFO
_LOG_LEVEL_NAMES = {"DEBUG", "INFO", "WARNING", "ERROR"}
# Librerías de terceros son muy verbosas a INFO/DEBUG (cada request HTTP,
# cada evento interno de PTB/ADK) — se fijan a WARNING sin importar
# LOG_LEVEL, para que subir la verbosidad propia no ahogue el log en ruido
# ajeno.
_QUIET_THIRD_PARTY_LOGGERS = (
    "httpx",
    "httpcore",
    "google_genai",
    "google.adk",
    "telegram",
    "apscheduler",
)

GENERATING_TEXT = "🎨 Generando…"
UPSCALING_TEXT = "🔼 Generando en 4K y subiendo a las pantallas…"
GENERIC_ERROR_TEXT = "⚠️ Algo salió mal generando esto. ¿Lo intentamos de nuevo?"
CONFIRM_CALLBACK_PREFIX = "deploy:"
CONFIRM_BUTTON_TEXT = "✅ Confirmar"
REVERT_CALLBACK_PREFIX = "revert:"
REVERT_BUTTON_TEXT = "↩️ Revertir cambios"
KNOWN_TV_NAMES = ("43L", "43R", "50")
PREVIEW_CAPTION = (
    "🖼️ Así se vería en la sala. Si te gusta, toca *Confirmar* para subir "
    "esta versión a alta resolución."
)
PANELS_ALBUM_CAPTION = "🎞️ Las piezas del conjunto por separado."
STALE_PREVIEW_TEXT = (
    "⏱️ Esta vista previa es de una sesión que ya no está activa (expiró o "
    "se reinició) — pide el preview de nuevo antes de confirmar."
)
UNKNOWN_PREVIEW_TEXT = "🤔 No encuentro esta vista previa (¿un token muy viejo?)."
PARTIAL_DEPLOY_WARNING_TEXT = (
    "⚠️ El despliegue quedó a medias: {succeeded} sí cambiaron, pero "
    "{failed} falló. La pared quedó inconsistente — ¿revertimos las "
    "pantallas que sí cambiaron para dejarlas como estaban antes?"
)
RECONCILE_RESUME_TEXT = (
    "🔄 El bot se reinició mientras tu galería *{theme}* seguía "
    "procesándose — retomando desde donde se quedó, sin perder lo ya "
    "logrado. Te aviso en cuanto termine."
)
RECONCILE_LEGACY_TEXT = (
    "⚠️ Se encontró un lote de galería sin terminar (*{theme}*, "
    "`{batch_id}`) de antes de que el bot supiera a qué chat avisar — no "
    "se puede retomar automáticamente. Si sigue interesando, hay que "
    "pedirlo de nuevo."
)
_TYPING_INTERVAL_SECONDS = 4.0
_PROACTIVE_SEND_PACING_SECONDS = 1.0
_BATCH_ALBUM_PAGE_SIZE = 10

RESET_KEYBOARD = ReplyKeyboardMarkup(
    [[RESET_BUTTON_TEXT]], resize_keyboard=True, is_persistent=True
)


def load_allowed_user_ids(raw: str | None) -> list[int]:
    """Parses TELEGRAM_ALLOWED_USER_IDS ("123, 456") into a list of int
    Telegram user IDs. Empty/missing input yields an empty list — which
    is a fail-safe whitelist that matches no one (§9).
    """
    if not raw:
        return []
    return [int(piece.strip()) for piece in raw.split(",") if piece.strip()]


def load_session_timeout_seconds(raw: str | None) -> int:
    """Parses SESSION_INACTIVITY_TIMEOUT_SECONDS, falling back to a 3h
    default (PRD §7.2) when unset or invalid.
    """
    if not raw or not raw.strip():
        return DEFAULT_SESSION_TIMEOUT_SECONDS
    try:
        return int(raw.strip())
    except ValueError:
        return DEFAULT_SESSION_TIMEOUT_SECONDS


def load_log_level(raw: str | None) -> int:
    """Parses LOG_LEVEL ("DEBUG"/"INFO"/"WARNING"/"ERROR", case-insensitive),
    falling back to INFO when unset or invalid — mismo patrón de default
    seguro que load_session_timeout_seconds. DEBUG habilita detalle
    completo (prompts crudos, resolución mDNS candidato por candidato) para
    troubleshooting activo; el default INFO da una narrativa legible sin
    texto libre del usuario (§ diseño de logging, dev_plan §3.7).
    """
    if not raw or not raw.strip():
        return DEFAULT_LOG_LEVEL
    name = raw.strip().upper()
    if name not in _LOG_LEVEL_NAMES:
        return DEFAULT_LOG_LEVEL
    return logging.getLevelNamesMapping()[name]


def configure_logging(level: int) -> None:
    """Configura el logging raíz para que llegue a journalctl vía
    StandardOutput=journal (docs/DEPLOY.md) — sin esto, INFO/DEBUG nunca
    salen del proceso porque el logger raíz por default está en WARNING sin
    handler. Silencia librerías de terceros a WARNING para que subir a
    DEBUG amplifique nuestro propio código, no el ruido de httpx/PTB/ADK.
    """
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    for logger_name in _QUIET_THIRD_PARTY_LOGGERS:
        logging.getLogger(logger_name).setLevel(logging.WARNING)


def build_session_service() -> DatabaseSessionService:
    db_path = (
        Path(__file__).resolve().parent.parent.parent / "data" / "adk_sessions.sqlite3"
    )
    db_path.parent.mkdir(parents=True, exist_ok=True)
    service = DatabaseSessionService(f"sqlite+aiosqlite:///{db_path}")

    def _set_wal_mode(dbapi_connection: object, _: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    event.listen(service.db_engine.sync_engine, "connect", _set_wal_mode)
    return service


def build_runner() -> Runner:
    return Runner(
        app_name=APP_NAME,
        agent=root_agent,
        session_service=build_session_service(),
    )


def _final_text(events: list) -> str:
    texts = []
    for event_ in events:
        if event_.content and event_.content.parts:
            for part in event_.content.parts:
                if part.text:
                    texts.append(part.text)
    return "\n".join(texts)


def _to_markdown_v2(text: str) -> str:
    """Convierte el markdown de salida del agente/bot (GFM, p. ej.
    **negritas**) al MarkdownV2 de Telegram, ya escapado. Toda salida de
    texto de este módulo pasa por aquí antes de enviarse con
    parse_mode=ParseMode.MARKDOWN_V2 — de lo contrario Telegram muestra
    los asteriscos crudos (hallazgo de 2.4) o rechaza el mensaje por
    caracteres sin escapar.
    """
    return telegramify_markdown.markdownify(text)


async def _keep_typing(bot: object, chat_id: int) -> None:
    """Repite send_chat_action cada ~4s (el indicador de Telegram expira a
    los ~5s) mientras dure la llamada al agente, para que las corridas de
    varios minutos de generate_set_diptico/generate_set_split no se lean
    como el bot caído (§8, motivación reforzada 2026-07-07).
    """
    try:
        while True:
            await bot.send_chat_action(chat_id, ChatAction.TYPING)  # type: ignore[attr-defined]
            await asyncio.sleep(_TYPING_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        pass


async def _run_turn_with_progress(
    runner: Runner, bot: object, chat_id: int, user_id: str, session_id: str, text: str
) -> list:
    """Corre un turno del agente mostrando feedback de progreso continuo
    mientras dura. El typing loop se cancela siempre al terminar, incluso
    si la corrida lanza una excepción (esta se deja propagar; el llamador
    decide cómo comunicarla)."""
    typing_task = asyncio.create_task(_keep_typing(bot, chat_id))
    try:
        events = []
        async for event_ in runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=types.Content(role="user", parts=[types.Part(text=text)]),
        ):
            events.append(event_)
        return events
    finally:
        typing_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await typing_task


@dataclass
class _ComposedPreview:
    preview_image_id: str
    image_43l: str
    image_43r: str
    image_50: str


def _extract_compose_previews(events: list) -> list[_ComposedPreview]:
    """Recorre los eventos de una corrida buscando pares llamada→respuesta
    de compose_preview, para mandar el resultado como foto con su botón de
    confirmación (§2.4). Camina `event.content.parts` a mano, igual que
    `_final_text`, en vez de usar `Event.get_function_calls`/
    `get_function_responses` — mismos datos, pero así los fakes de test
    basados en SimpleNamespace no necesitan implementar esos métodos.
    Correlacionar por adyacencia llamada-luego-respuesta basta porque este
    agente no hace tool calls en paralelo.
    """
    results = []
    pending_args = None
    for event_ in events:
        if not event_.content or not event_.content.parts:
            continue
        for part in event_.content.parts:
            if part.function_call and part.function_call.name == "compose_preview":
                pending_args = part.function_call.args
            elif (
                part.function_response
                and part.function_response.name == "compose_preview"
            ):
                data = part.function_response.response or {}
                if pending_args is not None and "error" not in data:
                    results.append(
                        _ComposedPreview(
                            preview_image_id=data["image_id"],
                            image_43l=pending_args.get("image_43l"),
                            image_43r=pending_args.get("image_43r"),
                            image_50=pending_args.get("image_50"),
                        )
                    )
                pending_args = None
    return results


def _finalize_high_res_all_succeeded(events: list) -> bool:
    """True only if the turn made at least one finalize_high_res call and
    every one of them succeeded. An approved session should close on
    completion, not just on idle timeout — otherwise an expired session
    can't be told apart from a finished commission (dev_plan §2.4). Split
    mode calls finalize_high_res twice (wide→43L/43R, then 50); a partial
    failure must NOT close the session, since the commission isn't
    actually done and the user may want to retry the failed panel in the
    same context.
    """
    responses = []
    for event_ in events:
        if not event_.content or not event_.content.parts:
            continue
        for part in event_.content.parts:
            if (
                part.function_response
                and part.function_response.name == "finalize_high_res"
            ):
                responses.append(part.function_response.response or {})
    return bool(responses) and all("error" not in r for r in responses)


def _extract_deploy_results(events: list) -> dict | None:
    """Devuelve el dict de resultado de la última llamada a deploy_to_panels
    en la corrida ({'43L': {...}, '43R': {...}, '50': {...}}), o None si
    esa tool no se llamó en este turno. Mismo patrón de caminar
    `event.content.parts` que `_extract_compose_previews` — no hace falta
    correlacionar con la llamada, ya que deploy_to_panels no tiene
    argumentos que nos interesen aquí (solo su respuesta).
    """
    result = None
    for event_ in events:
        if not event_.content or not event_.content.parts:
            continue
        for part in event_.content.parts:
            if (
                part.function_response
                and part.function_response.name == "deploy_to_panels"
            ):
                result = part.function_response.response
    return result


def _extract_materialized_batch_ids(events: list) -> list[str]:
    """Devuelve el `batch_id` de cada llamada exitosa a
    `materialize_batch_gallery` en la corrida, en el orden en que
    ocurrieron -- lista vacía si esa tool no se llamó (o siempre falló) en
    este turno. Mismo patrón de caminar `event.content.parts` que
    `_extract_deploy_results`.

    SKILL.md instruye al modelo a llamar esta tool una sola vez por lote
    confirmado, pero nada a nivel de código lo impide -- si el modelo la
    llama más de una vez en el mismo turno (p. ej. tras una respuesta
    ambigua de una tool), cada llamada exitosa ya materializó una fila
    real en SQLite y necesita su propio `chat_id`/corredor de fondo; una
    versión anterior de esta función solo devolvía la última, dejando
    cualquier lote anterior huérfano (`chat_id=NULL` para siempre,
    confundido después con un lote legado por
    `reconcile_batches_on_startup`).
    """
    batch_ids: list[str] = []
    for event_ in events:
        if not event_.content or not event_.content.parts:
            continue
        for part in event_.content.parts:
            if (
                part.function_response
                and part.function_response.name == "materialize_batch_gallery"
            ):
                data = part.function_response.response or {}
                batch_id = data.get("batch_id")
                if "error" not in data and batch_id is not None:
                    batch_ids.append(batch_id)
    return batch_ids


def _format_batch_report_text(summary: dict) -> str:
    """Redacta el texto del reporte final de un lote a partir de
    `engine.batch.summarize_batch` (dev_plan_phase_2.md §3.2). Es
    determinístico, no redactado por el LLM -- no hay un turno de agente
    corriendo cuando el corredor de fondo termina, mismo principio que el
    estimado de tiempo de §2.4. Nunca infiere la distinción
    policy_rejection vs. falla técnica del texto de error: la lee tal
    cual de las dos listas ya separadas que produce `summarize_batch`.

    Un lote sin ningún `needs_attention` produce un mensaje simple de
    éxito, sin la sección de fallas -- no generalizar el peor caso ni
    anunciar fallas que no existieron.
    """
    lines = [
        f"🖼️ Tu galería de *{summary['theme']}* ({summary['day_count']} días) "
        "ya está lista.",
    ]

    policy_failures = summary["needs_attention_policy_rejection"]
    technical_failures = summary["needs_attention_technical"]

    if not policy_failures and not technical_failures:
        lines.append("Todos los paneles se generaron y finalizaron con éxito.")
        return "\n\n".join(lines)

    succeeded = sum(
        count
        for stage, count in summary["stage_counts"].items()
        if stage != "needs_attention"
    )
    lines.append(
        f"{succeeded} panel(es) se lograron. "
        f"{len(policy_failures) + len(technical_failures)} necesitan tu atención:"
    )

    if policy_failures:
        lines.append(
            "\n".join(
                f"❌ Día {failure['day_index']} panel {failure['panel']}: rechazo de "
                "política, no se reintentó -- considera cambiar el tema de ese panel."
                for failure in policy_failures
            )
        )
    if technical_failures:
        lines.append(
            "\n".join(
                f"⚠️ Día {failure['day_index']} panel {failure['panel']}: se agotaron "
                "los reintentos -- puedes pedir que se reintente."
                for failure in technical_failures
            )
        )

    return "\n\n".join(lines)


def _batch_report_albums(summary: dict) -> list[list[InputMediaPhoto]]:
    """Arma los álbumes de fotos del reporte final de un lote, paginados a
    lo más `_BATCH_ALBUM_PAGE_SIZE` fotos por álbum (Requisito duro #8,
    dev_plan_phase_2.md §3.2). Recorre los días en orden y junta los
    `image_id` reales (paneles en `needs_attention` sin imagen se saltan
    -- nunca revienta por `image_id=None`). Mismo patrón de lectura de
    bytes que `_panels_album`: nunca un `Path` crudo a `InputMediaPhoto`.
    Solo la primera foto del primer álbum lleva caption; el detalle por
    día/panel ya vive en el texto del reporte.
    """
    image_ids = [
        panel["image_id"]
        for day in summary["days"]
        for panel in day["panels"].values()
        if panel["image_id"] is not None
    ]

    albums = []
    for page_start in range(0, len(image_ids), _BATCH_ALBUM_PAGE_SIZE):
        page = image_ids[page_start : page_start + _BATCH_ALBUM_PAGE_SIZE]
        albums.append(
            [
                InputMediaPhoto(
                    media=(IMAGES_DIR / f"{image_id}.jpg").read_bytes(),
                    caption=(
                        _to_markdown_v2(PANELS_ALBUM_CAPTION)
                        if page_start == 0 and index == 0
                        else None
                    ),
                    parse_mode=(
                        ParseMode.MARKDOWN_V2
                        if page_start == 0 and index == 0
                        else None
                    ),
                )
                for index, image_id in enumerate(page)
            ]
        )
    return albums


async def _send_batch_report(bot: object, chat_id: int, batch_id: str) -> None:
    """Manda el reporte proactivo final de un lote (PRD §15.3 paso 9,
    §15.6, dev_plan_phase_2.md §3.2): un mensaje de texto con el resumen
    seguido de los álbumes de fotos paginados, nunca como respuesta de
    turno (`_deliver_turn_result` no aplica aquí -- no hay turno activo
    cuando el corredor de fondo termina).

    Respeta el límite de ~1 mensaje/segundo de la API de Telegram
    (Requisito duro #8) pausando `_PROACTIVE_SEND_PACING_SECONDS` antes de
    cada envío después del primero -- incluyendo antes del primer álbum,
    para separarlo del mensaje de texto.

    Reanudable (hallazgo de code review posterior a 3.3): antes de mandar
    el texto o cada álbum, consulta `batch_store.get_batch_report_progress`
    y se salta lo que ya se entregó con éxito en una invocación anterior
    de esta misma función -- si el proceso muere a mitad de camino (texto
    ya mandado, álbum 2 de 3 falla por un error real de red de Telegram),
    la reinvocación disparada por `reconcile_batches_on_startup` continúa
    justo donde se quedó en vez de repetir el reporte completo desde cero.
    """
    summary = summarize_batch(batch_id)
    text_already_sent, albums_already_sent = batch_store.get_batch_report_progress(
        batch_id
    )
    if not text_already_sent:
        await bot.send_message(  # type: ignore[attr-defined]
            chat_id,
            _to_markdown_v2(_format_batch_report_text(summary)),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        batch_store.mark_batch_report_text_sent(batch_id)

    for album in _batch_report_albums(summary)[albums_already_sent:]:
        await asyncio.sleep(_PROACTIVE_SEND_PACING_SECONDS)
        await bot.send_chat_action(chat_id, ChatAction.UPLOAD_PHOTO)  # type: ignore[attr-defined]
        await bot.send_media_group(chat_id=chat_id, media=album)  # type: ignore[attr-defined]
        batch_store.mark_batch_report_album_sent(batch_id)


async def _run_batch_engine_in_background(
    batch_id: str, bot: object, chat_id: int
) -> None:
    """Corre el corredor completo de un lote recién confirmado (draft 1K
    -> finalización 4K, PRD §15.3 paso 8) fuera del turno de Telegram que
    lo disparó (dev_plan_phase_2.md §3.1, requisito duro #7: confirmar un
    lote nunca bloquea el turno). `run_draft_stage`/`run_finalize_stage`
    (Etapa 2, ya probados) son funciones síncronas que pueden tardar
    minutos contra la API real de Gemini -- se corren en un hilo aparte
    (mismo patrón que `revert_command_handler` con `tv_deploy.revert_panels`)
    para no bloquear el loop de eventos del bot mientras corren.

    Al terminar, manda el reporte proactivo (§3.2) al chat que confirmó el
    lote. Marca `batch.status='running'` al arrancar y `'reported'` justo
    después de que el reporte se manda (dev_plan_phase_2.md §3.3) -- es la
    misma función tanto para un arranque en caliente (recién confirmado)
    como para una reanudación tras un reinicio del bot
    (`reconcile_batches_on_startup`), sin una segunda copia de esta
    lógica: `run_draft_stage`/`run_finalize_stage` ya son idempotentes
    (§2.5), así que reinvocarlas sobre un lote a medias simplemente
    continúa donde se quedó. Una excepción real que escape de aquí (no
    capturada por el corredor, p. ej. un fallo de I/O o del propio envío
    del reporte) se propaga a través de `Application.create_task`, que la
    enruta a `global_error_handler` -- nunca desaparece en silencio; el
    lote queda en `'running'`, y la próxima reconciliación al reiniciar lo
    vuelve a recoger igual que uno recién materializado.
    """
    _logger.info("Corredor de lote arrancó en segundo plano: batch_id=%s", batch_id)
    batch_store.set_batch_status(batch_id, "running")
    await asyncio.to_thread(run_draft_stage, batch_id)
    await asyncio.to_thread(run_finalize_stage, batch_id)
    _logger.info("Corredor de lote terminó en segundo plano: batch_id=%s", batch_id)
    await _send_batch_report(bot, chat_id, batch_id)
    batch_store.set_batch_status(batch_id, "reported")


async def reconcile_batches_on_startup(application: object) -> None:
    """Al arrancar el bot, detecta cualquier lote que quedó en un estado no
    terminal (`status != 'reported'`) por un reinicio/crash del proceso a
    la mitad y lo reporta o reanuda -- nunca lo deja huérfano en silencio
    (dev_plan_phase_2.md §3.3, requisito duro #6; PRD §15.6: `JobQueue`/
    tareas en memoria de PTB no sobreviven un reinicio, por eso el estado
    del lote vive en SQLite).

    Para un lote con `chat_id` conocido: avisa que se está retomando y
    dispara `_run_batch_engine_in_background` tal cual -- es la misma
    función que arranca un lote recién confirmado, sin una rama de
    "reanudación" separada, porque `run_draft_stage`/`run_finalize_stage`
    ya son idempotentes (§2.5).

    Para un lote sin `chat_id` (solo posible en filas de antes de esta
    iteración, ver nota de migración en `batch_store`): nunca se intenta
    reanudar -- no hay a quién reportarle el resultado, y arrancar el
    corredor gastaría cuota real de la API de Gemini por un resultado que
    no se puede entregar. Solo se avisa por broadcast a los usuarios
    permitidos y se deja un warning en el log.

    Registrada vía `ApplicationBuilder().post_init(...)` -- PTB la llama
    una sola vez, después de inicializar la `Application` y antes de
    arrancar el polling, con `application.bot_data` ya poblado. En este
    punto `Application.running` todavía es `False`, así que
    `application.create_task` emite un `UserWarning` real ("won't be
    automatically awaited") -- se suprime a propósito: el estado del lote
    vive en SQLite, no en la tarea, así que si el proceso vuelve a cerrarse
    antes de que termine, el siguiente arranque la vuelve a recoger igual.
    """
    allowed_user_ids = application.bot_data.get("allowed_user_ids", frozenset())  # type: ignore[attr-defined]
    for batch in batch_store.list_non_terminal_batches():
        if batch.chat_id is not None:
            _logger.info(
                "Reconciliación al reiniciar: retomando batch_id=%s (chat_id=%s)",
                batch.batch_id,
                batch.chat_id,
            )
            await application.bot.send_message(  # type: ignore[attr-defined]
                batch.chat_id,
                _to_markdown_v2(RECONCILE_RESUME_TEXT.format(theme=batch.theme)),
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore",
                    message=r".*won't be automatically awaited.*",
                    category=UserWarning,
                )
                application.create_task(  # type: ignore[attr-defined]
                    _run_batch_engine_in_background(
                        batch.batch_id, application.bot, batch.chat_id  # type: ignore[attr-defined]
                    )
                )
        else:
            _logger.warning(
                "Reconciliación al reiniciar: batch_id=%s no terminal sin "
                "chat_id conocido (lote de antes de esta iteración) -- "
                "avisando sin reanudar",
                batch.batch_id,
            )
            for user_id in allowed_user_ids:
                await application.bot.send_message(  # type: ignore[attr-defined]
                    user_id,
                    _to_markdown_v2(
                        RECONCILE_LEGACY_TEXT.format(
                            theme=batch.theme, batch_id=batch.batch_id
                        )
                    ),
                    parse_mode=ParseMode.MARKDOWN_V2,
                )


def _partially_failed_tvs(deploy_results: dict) -> list[str]:
    """De un resultado de deploy_to_panels, devuelve los nombres de las TVs
    que sí se desplegaron con éxito, solo si el resultado es una falla
    PARCIAL (al menos un éxito y al menos un error) — dev_plan §3.5: un
    éxito total no tiene nada que revertir, y un fallo total no cambió
    ninguna pantalla, así que tampoco hay nada que revertir en ninguno de
    esos dos casos.
    """
    succeeded = [
        tv_name
        for tv_name in KNOWN_TV_NAMES
        if tv_name in deploy_results and "error" not in deploy_results[tv_name]
    ]
    failed = [
        tv_name
        for tv_name in KNOWN_TV_NAMES
        if tv_name in deploy_results and "error" in deploy_results[tv_name]
    ]
    if succeeded and failed:
        return succeeded
    return []


def _revert_keyboard(tv_names: list[str]) -> InlineKeyboardMarkup:
    callback_data = f"{REVERT_CALLBACK_PREFIX}{','.join(tv_names)}"
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(REVERT_BUTTON_TEXT, callback_data=callback_data)]]
    )


def _panels_album(preview: "_ComposedPreview") -> list[InputMediaPhoto]:
    """Arma el álbum de las tres piezas por separado (43L, 43R, 50) para
    darle al usuario detalle real de cada panel — la foto compuesta del
    preview los muestra pegados a la foto de la sala, demasiado chicos
    para juzgar composición/nitidez individual (hallazgo de uso real,
    2026-07-08). Solo la primera imagen del álbum lleva caption (limitación
    de Telegram en sendMediaGroup); las tres ya están identificadas por
    panel en el mensaje de texto que las acompaña.

    Se leen los bytes de cada archivo en vez de pasar el `Path` directo: a
    diferencia de `send_photo` (que respeta el `local_mode` real del bot),
    `InputMediaPhoto` fija `local_mode=True` de forma interna al parsear
    rutas, lo que produce un URI `file://` que la Bot API real rechaza
    (`Invalid file http url specified`) — confirmado en verificación
    manual contra el bot real, no es un caso hipotético. Pasar bytes evita
    esa rama por completo sin dejar descriptores de archivo abiertos.
    """
    panels = [
        ("43L", preview.image_43l),
        ("43R", preview.image_43r),
        ("50", preview.image_50),
    ]
    return [
        InputMediaPhoto(
            media=(IMAGES_DIR / f"{image_id}.jpg").read_bytes(),
            caption=_to_markdown_v2(PANELS_ALBUM_CAPTION) if index == 0 else None,
            parse_mode=ParseMode.MARKDOWN_V2 if index == 0 else None,
        )
        for index, (_, image_id) in enumerate(panels)
    ]


async def _deliver_turn_result(
    application: object,
    bot: object,
    chat_id: int,
    session_id: str,
    progress_message: object,
    events: list,
) -> None:
    """Por cada preview compuesto en la corrida: manda primero un álbum
    con las tres piezas por separado (detalle real de cada panel), luego
    la foto compuesta sobre la sala con su botón de confirmación atado al
    image_id exacto mostrado, y al final edita el mensaje de progreso con
    el texto final del turno. Una corrida que solo finaliza en alta
    resolución (sin compose_preview) no produce fotos — el ciclo de arriba
    simplemente no itera nada.

    Si el turno incluyó un deploy_to_panels que quedó parcial (algunas
    pantallas sí, otras no), manda además un aviso con un botón "↩️
    Revertir cambios" atado solo a las TVs que sí cambiaron — nunca a la
    que falló, porque esa nunca se tocó (dev_plan §3.5).

    Si el turno materializó uno o más lotes de galería
    (`materialize_batch_gallery`, dev_plan_phase_2.md §3.1 -- normalmente
    uno solo por turno, pero nada a nivel de código impide que el modelo
    la llame más de una vez), persiste primero a qué chat pertenece cada
    uno (`batch_store.set_batch_chat_id`, §3.3 -- es lo único que le
    permite a una reconciliación tras un reinicio saber a dónde reportar)
    y luego dispara el corredor completo (draft -> finalización 4K) de
    cada uno como tarea de fondo vía `application.create_task` — el turno
    no espera a que terminen, cumpliendo el requisito duro #7 (confirmar
    un lote nunca bloquea el turno de Telegram).
    """
    for preview in _extract_compose_previews(events):
        token = preview_store.new_token()
        preview_store.save_preview(
            token,
            chat_id,
            session_id,
            preview.image_43l,
            preview.image_43r,
            preview.image_50,
            time.time(),
        )
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        CONFIRM_BUTTON_TEXT,
                        callback_data=f"{CONFIRM_CALLBACK_PREFIX}{token}",
                    )
                ]
            ]
        )
        await bot.send_chat_action(chat_id, ChatAction.UPLOAD_PHOTO)  # type: ignore[attr-defined]
        await bot.send_media_group(  # type: ignore[attr-defined]
            chat_id=chat_id, media=_panels_album(preview)
        )
        await bot.send_photo(  # type: ignore[attr-defined]
            chat_id=chat_id,
            photo=IMAGES_DIR / f"{preview.preview_image_id}.jpg",
            caption=_to_markdown_v2(PREVIEW_CAPTION),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=keyboard,
        )

    deploy_results = _extract_deploy_results(events)
    if deploy_results is not None:
        succeeded = _partially_failed_tvs(deploy_results)
        if succeeded:
            failed = [
                f"{tv_name} ({deploy_results[tv_name]['error']})"
                for tv_name in KNOWN_TV_NAMES
                if tv_name in deploy_results and "error" in deploy_results[tv_name]
            ]
            await bot.send_message(  # type: ignore[attr-defined]
                chat_id,
                _to_markdown_v2(
                    PARTIAL_DEPLOY_WARNING_TEXT.format(
                        succeeded=", ".join(succeeded), failed=", ".join(failed)
                    )
                ),
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=_revert_keyboard(succeeded),
            )

    for materialized_batch_id in _extract_materialized_batch_ids(events):
        batch_store.set_batch_chat_id(materialized_batch_id, chat_id)
        application.create_task(  # type: ignore[attr-defined]
            _run_batch_engine_in_background(materialized_batch_id, bot, chat_id)
        )

    reply_text = _final_text(events) or "Listo."
    await progress_message.edit_text(  # type: ignore[attr-defined]
        _to_markdown_v2(reply_text), parse_mode=ParseMode.MARKDOWN_V2
    )


async def _run_and_deliver(
    runner: Runner,
    session_service: DatabaseSessionService,
    application: object,
    bot: object,
    chat_id: int,
    user_id: str,
    session_id: str,
    text: str,
    progress_message: object,
) -> None:
    """Corre un turno con feedback de progreso y entrega su resultado
    (fotos de preview + edición final). Si la corrida del agente o la
    entrega posterior (álbum/foto/edición final) lanzan una excepción, el
    mensaje de progreso se edita a un error genérico en vez de quedarse en
    "Generando…" para siempre — la entrega también puede fallar por su
    cuenta (p. ej. un rate limit de Telegram al mandar fotos), no solo la
    corrida del agente, así que ambas quedan bajo la misma red de
    seguridad (§2.5). La taxonomía fina de errores de tool sigue siendo
    trabajo del propio agente vía 'MANEJO DE ERRORES' en su instrucción.

    Si el turno completó finalize_high_res sin ningún error (aprobación
    exitosa) y la entrega también terminó sin excepción, rota la sesión en
    silencio al terminar: una sesión "expirada" debe significar siempre un
    encargo abandonado a medias, nunca uno que ya se completó (dev_plan
    §2.4) — una entrega que truena a medias no cuenta como encargo
    cerrado, mismo criterio que ya aplica a un fallo parcial de
    finalize_high_res.
    """
    try:
        events = await _run_turn_with_progress(
            runner, bot, chat_id, user_id, session_id, text
        )
        await _deliver_turn_result(
            application, bot, chat_id, session_id, progress_message, events
        )
    except Exception:
        _logger.exception("Turno falló para chat_id=%s", chat_id)
        await progress_message.edit_text(  # type: ignore[attr-defined]
            _to_markdown_v2(GENERIC_ERROR_TEXT), parse_mode=ParseMode.MARKDOWN_V2
        )
        raise

    if _finalize_high_res_all_succeeded(events):
        await rotate_session(session_service, chat_id, reason="aprobacion_exitosa")


async def rotate_session(
    session_service: DatabaseSessionService, chat_id: int, reason: str = "manual"
) -> str:
    """Creates a fresh ADK session for `chat_id` and makes it the current
    one in the store. Returns the new session_id.
    """
    user_id = str(chat_id)
    new_id = session_store.new_session_id(chat_id)
    await session_service.create_session(
        app_name=APP_NAME, user_id=user_id, session_id=new_id
    )
    session_store.set_current_session(chat_id, new_id, time.time())
    _logger.info(
        "Sesión rotada: chat_id=%s reason=%s new_session_id=%s",
        chat_id,
        reason,
        new_id,
    )
    return new_id


async def get_or_rotate_session(
    session_service: DatabaseSessionService, chat_id: int, timeout_seconds: int
) -> tuple[str, bool]:
    """Resolves the current ADK session_id for `chat_id`, rotating to a
    new one when there's none yet, the inactivity timeout has elapsed, or
    the stored pointer is out of sync with ADK's own store. Returns
    (session_id, expired) — `expired` is True only when an existing
    session was rotated away due to timeout, so the caller can warn the
    user (§7.9 escalera de gracia: never mutate state silently).
    """
    user_id = str(chat_id)
    current = session_store.get_current_session(chat_id)
    now = time.time()

    if current is None:
        session_id = await rotate_session(session_service, chat_id, reason="sin_sesion")
        return session_id, False

    if now - current.last_activity > timeout_seconds:
        session_id = await rotate_session(session_service, chat_id, reason="timeout")
        return session_id, True

    session = await session_service.get_session(
        app_name=APP_NAME, user_id=user_id, session_id=current.session_id
    )
    if session is None:
        _logger.warning(
            "Puntero de sesión desincronizado con el store de ADK: "
            "chat_id=%s session_id=%s",
            chat_id,
            current.session_id,
        )
        session_id = await rotate_session(
            session_service, chat_id, reason="desincronizada"
        )
        return session_id, False

    session_store.set_current_session(chat_id, current.session_id, now)
    return current.session_id, False


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text or not update.effective_chat:
        return

    runner: Runner = context.application.bot_data["runner"]
    session_service: DatabaseSessionService = context.application.bot_data[
        "session_service"
    ]
    timeout_seconds: int = context.application.bot_data["session_timeout_seconds"]
    chat_id = update.effective_chat.id
    user_id = str(chat_id)

    session_id, expired = await get_or_rotate_session(
        session_service, chat_id, timeout_seconds
    )
    _logger.info(
        "Mensaje recibido: chat_id=%s len=%d session_id=%s expired=%s",
        chat_id,
        len(update.message.text),
        session_id,
        expired,
    )
    _logger.debug("Texto del mensaje: chat_id=%s text=%r", chat_id, update.message.text)
    if expired:
        await update.message.reply_text(
            _to_markdown_v2(
                "⏱️ Tu sesión anterior expiró por inactividad — empezamos de cero."
            ),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=RESET_KEYBOARD,
        )

    progress_message = await update.message.reply_text(
        _to_markdown_v2(GENERATING_TEXT), parse_mode=ParseMode.MARKDOWN_V2
    )

    await _run_and_deliver(
        runner,
        session_service,
        context.application,
        context.bot,
        chat_id,
        user_id,
        session_id,
        update.message.text,
        progress_message,
    )


async def confirm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Maneja el botón inline "✅ Confirmar" de un preview (§2.4, PRD
    §7.2). Resuelve el token a los image_id exactos que ese preview
    mostró, y —solo si la sesión del chat sigue siendo la misma de
    entonces— manda un mensaje sintético nombrando esos ids a la sesión de
    ADK, dejando que las instrucciones de APROBACIÓN ya existentes en
    `agent.py` decidan cómo llamar finalize_high_res (díptico vs. split):
    el bot nunca reimplementa esa lógica.
    """
    query = update.callback_query
    if (
        query is None
        or not query.data
        or not query.data.startswith(CONFIRM_CALLBACK_PREFIX)
    ):
        return

    await query.answer()

    allowed_user_ids = context.application.bot_data["allowed_user_ids"]
    if (
        update.effective_user is None
        or update.effective_user.id not in allowed_user_ids
    ):
        return

    if not update.effective_chat:
        return
    chat_id = update.effective_chat.id

    token = query.data[len(CONFIRM_CALLBACK_PREFIX) :]
    preview = preview_store.get_preview(token)
    if preview is None:
        _logger.info("Confirmar: token desconocido chat_id=%s token=%s", chat_id, token)
        await context.bot.send_message(
            chat_id,
            _to_markdown_v2(UNKNOWN_PREVIEW_TEXT),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    session_service: DatabaseSessionService = context.application.bot_data[
        "session_service"
    ]
    timeout_seconds: int = context.application.bot_data["session_timeout_seconds"]
    session_id, expired = await get_or_rotate_session(
        session_service, chat_id, timeout_seconds
    )
    if expired or session_id != preview.session_id:
        _logger.info(
            "Confirmar: preview obsoleto chat_id=%s expired=%s "
            "session_id=%s preview_session_id=%s",
            chat_id,
            expired,
            session_id,
            preview.session_id,
        )
        await context.bot.send_message(
            chat_id,
            _to_markdown_v2(STALE_PREVIEW_TEXT),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=RESET_KEYBOARD,
        )
        return

    runner: Runner = context.application.bot_data["runner"]
    confirm_text = (
        "Aprueba y sube a alta resolución exactamente estas piezas: "
        f"43L={preview.image_43l}, 43R={preview.image_43r}, 50={preview.image_50}."
    )
    progress_message = await context.bot.send_message(
        chat_id, _to_markdown_v2(UPSCALING_TEXT), parse_mode=ParseMode.MARKDOWN_V2
    )

    await _run_and_deliver(
        runner,
        session_service,
        context.application,
        context.bot,
        chat_id,
        str(chat_id),
        session_id,
        confirm_text,
        progress_message,
    )


def _format_revert_report(results: dict) -> str:
    lines = []
    for tv_name, result in results.items():
        if "error" in result:
            lines.append(f"❌ {tv_name}: {result['error']}")
        else:
            lines.append(f"↩️ {tv_name}: revertida ({result['content_id']}).")
    return "\n".join(lines)


def _log_revert_results(tv_names: list[str], results: dict) -> None:
    succeeded = [name for name in tv_names if "error" not in results.get(name, {})]
    failed = [name for name in tv_names if "error" in results.get(name, {})]
    _logger.info(
        "Revert solicitado: tv_names=%s succeeded=%s failed=%s",
        tv_names,
        succeeded,
        failed,
    )


async def revert_command_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Comando `/revertir [43L|43R|50]` (dev_plan §3.5, PRD §7.6): revierte
    una TV física a la versión desplegada justo antes de la actual, sin
    pasar por el agente ADK — es una acción de infraestructura
    determinista, no una corrección creativa. Sin argumento, revierte las
    tres pantallas; con un nombre, revierte solo esa.
    """
    if not update.message or not update.effective_chat:
        return

    args = context.args or []
    tv_names: list[str]
    if not args:
        tv_names = list(KNOWN_TV_NAMES)
    else:
        requested = args[0].upper()
        if requested not in KNOWN_TV_NAMES:
            await update.message.reply_text(
                _to_markdown_v2(
                    f"🤔 No conozco una TV llamada {args[0]!r}. Usa una de: "
                    f"{', '.join(KNOWN_TV_NAMES)}."
                ),
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return
        tv_names = [requested]

    results = await asyncio.to_thread(tv_deploy.revert_panels, tv_names)
    _log_revert_results(tv_names, results)
    await update.message.reply_text(
        _to_markdown_v2(_format_revert_report(results)),
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def revert_button_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Maneja el botón inline "↩️ Revertir cambios" que aparece tras un
    despliegue parcial (dev_plan §3.5). El `callback_data` ya trae los
    nombres exactos de las TVs a revertir (las que sí cambiaron) — igual
    que `revert_command_handler`, actúa directo sobre `tv_deploy` sin pasar
    por el agente ADK.
    """
    query = update.callback_query
    if (
        query is None
        or not query.data
        or not query.data.startswith(REVERT_CALLBACK_PREFIX)
    ):
        return

    await query.answer()

    allowed_user_ids = context.application.bot_data["allowed_user_ids"]
    if (
        update.effective_user is None
        or update.effective_user.id not in allowed_user_ids
    ):
        return

    if not update.effective_chat:
        return
    chat_id = update.effective_chat.id

    tv_names = [
        name
        for name in query.data[len(REVERT_CALLBACK_PREFIX) :].split(",")
        if name in KNOWN_TV_NAMES
    ]
    if not tv_names:
        return

    results = await asyncio.to_thread(tv_deploy.revert_panels, tv_names)
    _log_revert_results(tv_names, results)
    await context.bot.send_message(
        chat_id,
        _to_markdown_v2(_format_revert_report(results)),
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def global_error_handler(
    _update: object, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Red de seguridad final de PTB (§2.5): cualquier excepción que escape
    de un handler *fuera* del try/except propio de `_run_and_deliver` (p.
    ej. en `confirm_handler`/`revert_button_handler` antes de llegar ahí)
    llegaría aquí sin dejar rastro alguno en journalctl si no se registra
    explícitamente — PTB por si solo la traga en su propio logger interno,
    que nunca se configuró con basicConfig.
    """
    _logger.error("Excepción no manejada en un handler de PTB", exc_info=context.error)


async def reset_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat:
        return

    session_service: DatabaseSessionService = context.application.bot_data[
        "session_service"
    ]
    await rotate_session(session_service, update.effective_chat.id)

    if update.message:
        await update.message.reply_text(
            _to_markdown_v2("🔄 Listo, empezamos de cero."),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=RESET_KEYBOARD,
        )


def build_application(token: str, allowed_user_ids: list[int]) -> Application:
    """Builds the Application with handlers gated by the numeric-ID
    whitelist (§7.1): unauthorized messages never reach any callback, so
    they get no reply at all.

    Reset (command `/nuevo` and the persistent button), `/revertir`, and
    the confirm inline button are registered in group 0, the generic text
    handler in group 1. PTB evaluates every group independently per
    update — a match in group 0 does NOT stop group 1 from also matching
    and firing (`Application.process_update` only stops later groups on
    `ApplicationHandlerStop`). `filters.TEXT` explicitly accepts command
    messages too (PTB's own docs), so without an exclusion, `/nuevo` and
    `/revertir` were falling through to `handle_message` as if their
    literal text were a theme — a real bug found in manual testing
    (2026-07-12) that burned a Gemini call and polluted a freshly-reset
    session. The generic handler's filter excludes `filters.COMMAND` and
    the reset button's own text to prevent this double-fire.
    `CallbackQueryHandler` has no `filters=` parameter, so the whitelist
    check for the confirm button happens manually inside `confirm_handler`
    against `allowed_user_ids` stashed in `bot_data`.

    `post_init(reconcile_batches_on_startup)` (dev_plan_phase_2.md §3.3):
    PTB awaits this once, right after `Application.initialize()` and
    before polling starts, with `bot_data["allowed_user_ids"]` already set
    below -- the natural hook point to detect and resume/report any batch
    gallery left mid-flight by a previous crash/restart.
    """
    application = (
        ApplicationBuilder()
        .token(token)
        .post_init(reconcile_batches_on_startup)
        .build()
    )
    application.bot_data["runner"] = build_runner()
    application.bot_data["session_service"] = application.bot_data[
        "runner"
    ].session_service
    application.bot_data["session_timeout_seconds"] = load_session_timeout_seconds(
        os.environ.get("SESSION_INACTIVITY_TIMEOUT_SECONDS")
    )
    application.bot_data["allowed_user_ids"] = frozenset(allowed_user_ids)

    whitelist = tg_filters.User(user_id=allowed_user_ids)
    application.add_handler(
        CommandHandler("nuevo", reset_handler, filters=whitelist), group=0
    )
    application.add_handler(
        MessageHandler(whitelist & tg_filters.Text([RESET_BUTTON_TEXT]), reset_handler),
        group=0,
    )
    application.add_handler(
        CommandHandler("revertir", revert_command_handler, filters=whitelist),
        group=0,
    )
    application.add_handler(
        CallbackQueryHandler(
            revert_button_handler, pattern=f"^{REVERT_CALLBACK_PREFIX}"
        ),
        group=0,
    )
    application.add_handler(
        CallbackQueryHandler(confirm_handler, pattern=f"^{CONFIRM_CALLBACK_PREFIX}"),
        group=0,
    )
    not_a_reset_command = tg_filters.TEXT & ~tg_filters.COMMAND
    not_the_reset_button = ~tg_filters.Text([RESET_BUTTON_TEXT])
    application.add_handler(
        MessageHandler(
            whitelist & (not_a_reset_command & not_the_reset_button),
            handle_message,
        ),
        group=1,
    )
    application.add_error_handler(global_error_handler)
    return application


def main() -> None:
    load_dotenv()
    configure_logging(load_log_level(os.environ.get("LOG_LEVEL")))

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        sys.exit("TELEGRAM_BOT_TOKEN no está configurada (revisa tu .env).")

    allowed_user_ids = load_allowed_user_ids(
        os.environ.get("TELEGRAM_ALLOWED_USER_IDS")
    )
    if not allowed_user_ids:
        sys.exit(
            "TELEGRAM_ALLOWED_USER_IDS está vacía: el bot no respondería a "
            "nadie (revisa tu .env)."
        )

    timeout_seconds = load_session_timeout_seconds(
        os.environ.get("SESSION_INACTIVITY_TIMEOUT_SECONDS")
    )
    _logger.info(
        "Arrancando bot: allowed_users=%d session_timeout_seconds=%d " "log_level=%s",
        len(allowed_user_ids),
        timeout_seconds,
        logging.getLevelName(_logger.getEffectiveLevel()),
    )
    _logger.debug("Allowed user ids: %s", allowed_user_ids)

    application = build_application(token, allowed_user_ids)
    application.run_polling()


if __name__ == "__main__":
    main()
