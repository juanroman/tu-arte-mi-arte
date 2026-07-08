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
"""

import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from google.genai import types
from sqlalchemy import event
from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
)
from telegram.ext import filters as tg_filters

from agents.tu_arte_mi_arte.agent import root_agent
from bot import session_store

APP_NAME = "tu_arte_mi_arte"
RESET_BUTTON_TEXT = "🔄 Empezar de cero"
DEFAULT_SESSION_TIMEOUT_SECONDS = 10800  # 3h, sugerido por PRD §7.2

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
            "⏱️ Tu sesión anterior expiró por inactividad — empezamos de cero.",
            reply_markup=RESET_KEYBOARD,
        )

    events = []
    async for event_ in runner.run_async(
        user_id=user_id,
        session_id=session_id,
        new_message=types.Content(
            role="user", parts=[types.Part(text=update.message.text)]
        ),
    ):
        events.append(event_)

    reply_text = _final_text(events) or "Listo."
    await update.message.reply_text(reply_text, reply_markup=RESET_KEYBOARD)


async def reset_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat:
        return

    session_service: DatabaseSessionService = context.application.bot_data[
        "session_service"
    ]
    await rotate_session(session_service, update.effective_chat.id)

    if update.message:
        await update.message.reply_text(
            "🔄 Listo, empezamos de cero.", reply_markup=RESET_KEYBOARD
        )


def build_application(token: str, allowed_user_ids: list[int]) -> Application:
    """Builds the Application with handlers gated by the numeric-ID
    whitelist (§7.1): unauthorized messages never reach any callback, so
    they get no reply at all.

    Reset (command `/nuevo` and the persistent button) is registered in
    group 0, the generic text handler in group 1 — PTB evaluates groups
    independently, stopping at the first match per group, so the button's
    literal text never falls through to the generic handler without
    needing to manually exclude it there.
    """
    application = ApplicationBuilder().token(token).build()
    application.bot_data["runner"] = build_runner()
    application.bot_data["session_service"] = application.bot_data[
        "runner"
    ].session_service
    application.bot_data["session_timeout_seconds"] = load_session_timeout_seconds(
        os.environ.get("SESSION_INACTIVITY_TIMEOUT_SECONDS")
    )

    whitelist = tg_filters.User(user_id=allowed_user_ids)
    application.add_handler(
        CommandHandler("nuevo", reset_handler, filters=whitelist), group=0
    )
    application.add_handler(
        MessageHandler(whitelist & tg_filters.Text([RESET_BUTTON_TEXT]), reset_handler),
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
