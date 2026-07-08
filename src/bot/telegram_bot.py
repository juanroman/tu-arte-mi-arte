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
"""

import asyncio
import contextlib
import os
import sys
import time
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
from engine.generation import IMAGES_DIR

APP_NAME = "tu_arte_mi_arte"
RESET_BUTTON_TEXT = "🔄 Empezar de cero"
DEFAULT_SESSION_TIMEOUT_SECONDS = 10800  # 3h, sugerido por PRD §7.2

GENERATING_TEXT = "🎨 Generando…"
GENERIC_ERROR_TEXT = "⚠️ Algo salió mal generando esto. ¿Lo intentamos de nuevo?"
CONFIRM_CALLBACK_PREFIX = "deploy:"
CONFIRM_BUTTON_TEXT = "✅ Confirmar"
PREVIEW_CAPTION = (
    "🖼️ Así se vería en la sala. Si te gusta, toca *Confirmar* para subir "
    "esta versión a alta resolución."
)
STALE_PREVIEW_TEXT = (
    "⏱️ Esta vista previa es de una sesión que ya no está activa (expiró o "
    "se reinició) — pide el preview de nuevo antes de confirmar."
)
UNKNOWN_PREVIEW_TEXT = "🤔 No encuentro esta vista previa (¿un token muy viejo?)."
_TYPING_INTERVAL_SECONDS = 4.0

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


async def _deliver_turn_result(
    bot: object, chat_id: int, session_id: str, progress_message: object, events: list
) -> None:
    """Envía como foto cada preview compuesto en la corrida (con su botón
    de confirmación atado al image_id exacto mostrado), y luego edita el
    mensaje de progreso con el texto final del turno. Una corrida que solo
    finaliza en alta resolución (sin compose_preview) no produce fotos —
    el ciclo de arriba simplemente no itera nada.
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
        await bot.send_photo(  # type: ignore[attr-defined]
            chat_id=chat_id,
            photo=IMAGES_DIR / f"{preview.preview_image_id}.jpg",
            caption=_to_markdown_v2(PREVIEW_CAPTION),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=keyboard,
        )

    reply_text = _final_text(events) or "Listo."
    await progress_message.edit_text(  # type: ignore[attr-defined]
        _to_markdown_v2(reply_text), parse_mode=ParseMode.MARKDOWN_V2
    )


async def _run_and_deliver(
    runner: Runner,
    session_service: DatabaseSessionService,
    bot: object,
    chat_id: int,
    user_id: str,
    session_id: str,
    text: str,
    progress_message: object,
) -> None:
    """Corre un turno con feedback de progreso y entrega su resultado
    (fotos de preview + edición final). Si la corrida lanza una excepción,
    el mensaje de progreso se edita a un error genérico en vez de quedarse
    en "Generando…" para siempre — la taxonomía fina de errores sigue
    siendo trabajo de 2.5, esto solo evita el placeholder colgado.

    Si el turno completó finalize_high_res sin ningún error (aprobación
    exitosa), rota la sesión en silencio al terminar: una sesión
    "expirada" debe significar siempre un encargo abandonado a medias,
    nunca uno que ya se completó (dev_plan §2.4).
    """
    try:
        events = await _run_turn_with_progress(
            runner, bot, chat_id, user_id, session_id, text
        )
    except Exception:
        await progress_message.edit_text(  # type: ignore[attr-defined]
            _to_markdown_v2(GENERIC_ERROR_TEXT), parse_mode=ParseMode.MARKDOWN_V2
        )
        raise

    await _deliver_turn_result(bot, chat_id, session_id, progress_message, events)

    if _finalize_high_res_all_succeeded(events):
        await rotate_session(session_service, chat_id)


async def rotate_session(session_service: DatabaseSessionService, chat_id: int) -> str:
    """Creates a fresh ADK session for `chat_id` and makes it the current
    one in the store. Returns the new session_id.
    """
    user_id = str(chat_id)
    new_id = session_store.new_session_id(chat_id)
    await session_service.create_session(
        app_name=APP_NAME, user_id=user_id, session_id=new_id
    )
    session_store.set_current_session(chat_id, new_id, time.time())
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
        session_id = await rotate_session(session_service, chat_id)
        return session_id, False

    if now - current.last_activity > timeout_seconds:
        session_id = await rotate_session(session_service, chat_id)
        return session_id, True

    session = await session_service.get_session(
        app_name=APP_NAME, user_id=user_id, session_id=current.session_id
    )
    if session is None:
        session_id = await rotate_session(session_service, chat_id)
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
        chat_id, _to_markdown_v2(GENERATING_TEXT), parse_mode=ParseMode.MARKDOWN_V2
    )

    await _run_and_deliver(
        runner,
        session_service,
        context.bot,
        chat_id,
        str(chat_id),
        session_id,
        confirm_text,
        progress_message,
    )


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

    Reset (command `/nuevo` and the persistent button) and the confirm
    inline button are registered in group 0, the generic text handler in
    group 1 — PTB evaluates groups independently, stopping at the first
    match per group, so the button's literal text never falls through to
    the generic handler without needing to manually exclude it there.
    `CallbackQueryHandler` has no `filters=` parameter, so the whitelist
    check for the confirm button happens manually inside `confirm_handler`
    against `allowed_user_ids` stashed in `bot_data`.
    """
    application = ApplicationBuilder().token(token).build()
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
        CallbackQueryHandler(confirm_handler, pattern=f"^{CONFIRM_CALLBACK_PREFIX}"),
        group=0,
    )
    application.add_handler(
        MessageHandler(whitelist & tg_filters.TEXT, handle_message), group=1
    )
    return application


def main() -> None:
    load_dotenv()

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

    application = build_application(token, allowed_user_ids)
    application.run_polling()


if __name__ == "__main__":
    main()
