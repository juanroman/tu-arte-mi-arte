import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from telegram.ext import MessageHandler

from bot.telegram_bot import build_application, load_allowed_user_ids, pong_handler


def test_load_allowed_user_ids_parses_comma_separated_ints():
    assert load_allowed_user_ids("123,456") == [123, 456]


def test_load_allowed_user_ids_tolerates_extra_whitespace():
    assert load_allowed_user_ids(" 123 , 456 ") == [123, 456]


def test_load_allowed_user_ids_empty_string_yields_empty_list():
    assert load_allowed_user_ids("") == []


def test_load_allowed_user_ids_none_yields_empty_list():
    assert load_allowed_user_ids(None) == []


def test_build_application_registers_a_single_whitelisted_handler():
    app = build_application("123:fake-token-for-tests", [111, 222])

    handlers = [h for group in app.handlers.values() for h in group]
    assert len(handlers) == 1

    handler = handlers[0]
    assert isinstance(handler, MessageHandler)
    assert handler.callback is pong_handler
    assert handler.filters.user_ids == frozenset({111, 222})


def test_build_application_with_empty_whitelist_matches_no_one():
    app = build_application("123:fake-token-for-tests", [])

    handlers = [h for group in app.handlers.values() for h in group]
    assert handlers[0].filters.user_ids == frozenset()
