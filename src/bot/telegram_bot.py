"""Telegram bot con lista blanca (PRD §7.1, §9) conectado al agente ADK
(§7.11, Etapa 2 iteración 2.2). Cada mensaje autorizado se mapea a una
llamada al `Runner` con el `root_agent` construido en Etapa 1 — mismas
tools, sin reescribir nada.

Sesión en memoria únicamente (PRD §7.2): un `chat_id` de Telegram se liga
a un `(user_id, session_id)` de ADK que vive mientras el proceso del bot
esté corriendo. Se pierde al reiniciar — la persistencia llega en 2.3.
"""

import os
import sys

from dotenv import load_dotenv
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from telegram import Update
from telegram.ext import Application, ApplicationBuilder, ContextTypes, MessageHandler
from telegram.ext import filters as tg_filters

from agents.tu_arte_mi_arte.agent import root_agent

APP_NAME = "tu_arte_mi_arte"


def load_allowed_user_ids(raw: str | None) -> list[int]:
    """Parses TELEGRAM_ALLOWED_USER_IDS ("123, 456") into a list of int
    Telegram user IDs. Empty/missing input yields an empty list — which
    is a fail-safe whitelist that matches no one (§9).
    """
    if not raw:
        return []
    return [int(piece.strip()) for piece in raw.split(",") if piece.strip()]


def build_runner() -> Runner:
    return Runner(
        app_name=APP_NAME,
        agent=root_agent,
        session_service=InMemorySessionService(),
    )


def _final_text(events: list) -> str:
    texts = []
    for event in events:
        if event.content and event.content.parts:
            for part in event.content.parts:
                if part.text:
                    texts.append(part.text)
    return "\n".join(texts)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text or not update.effective_chat:
        return

    runner: Runner = context.application.bot_data["runner"]
    session_service: InMemorySessionService = context.application.bot_data[
        "session_service"
    ]
    user_id = str(update.effective_chat.id)

    session = await session_service.get_session(
        app_name=APP_NAME, user_id=user_id, session_id=user_id
    )
    if session is None:
        session = await session_service.create_session(
            app_name=APP_NAME, user_id=user_id, session_id=user_id
        )

    events = []
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session.id,
        new_message=types.Content(
            role="user", parts=[types.Part(text=update.message.text)]
        ),
    ):
        events.append(event)

    reply_text = _final_text(events) or "Listo."
    await update.message.reply_text(reply_text)


def build_application(token: str, allowed_user_ids: list[int]) -> Application:
    """Builds the Application with a single handler gated by the numeric-ID
    whitelist (§7.1): unauthorized messages never reach the callback, so
    they get no reply at all. Only text messages reach the agent handler.
    """
    application = ApplicationBuilder().token(token).build()
    application.bot_data["runner"] = build_runner()
    application.bot_data["session_service"] = application.bot_data[
        "runner"
    ].session_service
    application.add_handler(
        MessageHandler(
            tg_filters.User(user_id=allowed_user_ids) & tg_filters.TEXT,
            handle_message,
        )
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
