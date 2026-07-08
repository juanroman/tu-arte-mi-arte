"""Telegram bot con lista blanca (PRD §7.1, §9): puerta de acceso mínima
antes de conectar el motor de generación (Etapa 2, iteración 2.1).

No dependency on the ADK agent yet — eso llega en 2.2.
"""

import os
import sys

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, ApplicationBuilder, ContextTypes, MessageHandler
from telegram.ext import filters as tg_filters


def load_allowed_user_ids(raw: str | None) -> list[int]:
    """Parses TELEGRAM_ALLOWED_USER_IDS ("123, 456") into a list of int
    Telegram user IDs. Empty/missing input yields an empty list — which
    is a fail-safe whitelist that matches no one (§9).
    """
    if not raw:
        return []
    return [int(piece.strip()) for piece in raw.split(",") if piece.strip()]


async def pong_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text("pong")


def build_application(token: str, allowed_user_ids: list[int]) -> Application:
    """Builds the Application with a single handler gated by the numeric-ID
    whitelist (§7.1): unauthorized messages never reach the callback, so
    they get no reply at all.
    """
    application = ApplicationBuilder().token(token).build()
    application.add_handler(
        MessageHandler(tg_filters.User(user_id=allowed_user_ids), pong_handler)
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
