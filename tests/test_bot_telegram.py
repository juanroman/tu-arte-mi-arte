import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from google.adk.runners import Runner  # noqa: E402
from google.adk.sessions import InMemorySessionService  # noqa: E402
from google.genai import types  # noqa: E402
from telegram.ext import CommandHandler, MessageHandler  # noqa: E402
from telegram.ext import filters as tg_filters  # noqa: E402

from bot import session_store  # noqa: E402
from bot.telegram_bot import (  # noqa: E402
    APP_NAME,
    RESET_BUTTON_TEXT,
    build_application,
    handle_message,
    load_allowed_user_ids,
    load_session_timeout_seconds,
    reset_handler,
)

DEFAULT_TIMEOUT = 10_800


@pytest.fixture(autouse=True)
def _isolate_session_store(tmp_path, monkeypatch):
    monkeypatch.setattr(session_store, "DB_PATH", tmp_path / "bot_state.sqlite3")


def test_load_allowed_user_ids_parses_comma_separated_ints():
    assert load_allowed_user_ids("123,456") == [123, 456]


def test_load_allowed_user_ids_tolerates_extra_whitespace():
    assert load_allowed_user_ids(" 123 , 456 ") == [123, 456]


def test_load_allowed_user_ids_empty_string_yields_empty_list():
    assert load_allowed_user_ids("") == []


def test_load_allowed_user_ids_none_yields_empty_list():
    assert load_allowed_user_ids(None) == []


def test_load_session_timeout_seconds_parses_valid_int():
    assert load_session_timeout_seconds("60") == 60


def test_load_session_timeout_seconds_defaults_when_missing():
    assert load_session_timeout_seconds(None) == DEFAULT_TIMEOUT
    assert load_session_timeout_seconds("") == DEFAULT_TIMEOUT


def test_load_session_timeout_seconds_defaults_when_invalid():
    assert load_session_timeout_seconds("not-a-number") == DEFAULT_TIMEOUT


def test_build_application_registers_reset_and_generic_handlers():
    app = build_application("123:fake-token-for-tests", [111, 222])

    group0 = app.handlers.get(0, [])
    group1 = app.handlers.get(1, [])

    command_handlers = [h for h in group0 if isinstance(h, CommandHandler)]
    assert len(command_handlers) == 1
    assert command_handlers[0].commands == frozenset({"nuevo"})
    assert command_handlers[0].callback is reset_handler

    button_handlers = [h for h in group0 if isinstance(h, MessageHandler)]
    assert len(button_handlers) == 1
    assert button_handlers[0].callback is reset_handler

    assert len(group1) == 1
    generic_handler = group1[0]
    assert isinstance(generic_handler, MessageHandler)
    assert generic_handler.callback is handle_message
    assert generic_handler.filters.base_filter.user_ids == frozenset({111, 222})


def test_build_application_with_empty_whitelist_matches_no_one():
    app = build_application("123:fake-token-for-tests", [])

    generic_handler = app.handlers[1][0]
    assert generic_handler.filters.base_filter.user_ids == frozenset()


def _text_event(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        content=types.Content(role="model", parts=[types.Part(text=text)])
    )


def _function_call_event() -> SimpleNamespace:
    return SimpleNamespace(
        content=types.Content(
            role="model",
            parts=[
                types.Part(
                    function_call=types.FunctionCall(name="generate_image", args={})
                )
            ],
        )
    )


def _no_text_event() -> SimpleNamespace:
    return SimpleNamespace(content=None)


def _fake_run_async_factory(events_by_call):
    """Returns a fake run_async bound method that yields events_by_call[i]
    (a list of lists) on the i-th call, tracking (user_id, session_id) seen.
    """
    calls = []

    async def fake_run_async(self, *, user_id, session_id, new_message):
        calls.append((user_id, session_id))
        idx = len(calls) - 1
        for event in events_by_call[idx]:
            yield event

    fake_run_async.calls = calls
    return fake_run_async


def _make_update_and_context(
    runner, session_service, chat_id, text="hola", timeout_seconds=DEFAULT_TIMEOUT
):
    update = SimpleNamespace(
        message=SimpleNamespace(text=text, reply_text=AsyncMock()),
        effective_chat=SimpleNamespace(id=chat_id),
    )
    context = SimpleNamespace(
        application=SimpleNamespace(
            bot_data={
                "runner": runner,
                "session_service": session_service,
                "session_timeout_seconds": timeout_seconds,
            }
        )
    )
    return update, context


def _build_runner_with_fake_run_async(events_by_call):
    session_service = InMemorySessionService()
    runner = Runner(app_name=APP_NAME, agent=object(), session_service=session_service)
    fake_run_async = _fake_run_async_factory(events_by_call)
    runner.run_async = fake_run_async.__get__(runner, Runner)
    return runner, session_service, fake_run_async


def test_first_message_creates_session_and_replies_with_agent_text():
    runner, session_service, fake_run_async = _build_runner_with_fake_run_async(
        [[_text_event("listo, img_123")]]
    )
    update, context = _make_update_and_context(runner, session_service, chat_id=42)

    asyncio.run(handle_message(update, context))

    update.message.reply_text.assert_awaited_once()
    args, kwargs = update.message.reply_text.call_args
    assert args[0] == "listo, img_123"

    pointer = session_store.get_current_session(42)
    assert pointer is not None
    session = asyncio.run(
        session_service.get_session(
            app_name=APP_NAME, user_id="42", session_id=pointer.session_id
        )
    )
    assert session is not None


def test_second_message_in_same_chat_reuses_existing_session():
    runner, session_service, fake_run_async = _build_runner_with_fake_run_async(
        [[_text_event("uno")], [_text_event("dos")]]
    )
    update1, context1 = _make_update_and_context(runner, session_service, chat_id=42)
    update2, context2 = _make_update_and_context(runner, session_service, chat_id=42)

    asyncio.run(handle_message(update1, context1))
    asyncio.run(handle_message(update2, context2))

    assert len(fake_run_async.calls) == 2
    (_, session_id_1), (_, session_id_2) = fake_run_async.calls
    assert session_id_1 == session_id_2


def test_two_different_chats_get_independent_sessions():
    runner, session_service, fake_run_async = _build_runner_with_fake_run_async(
        [[_text_event("uno")], [_text_event("dos")]]
    )
    update1, context1 = _make_update_and_context(runner, session_service, chat_id=1)
    update2, context2 = _make_update_and_context(runner, session_service, chat_id=2)

    asyncio.run(handle_message(update1, context1))
    asyncio.run(handle_message(update2, context2))

    (user_id_1, session_id_1), (user_id_2, session_id_2) = fake_run_async.calls
    assert user_id_1 != user_id_2
    assert session_id_1 != session_id_2


def test_multiple_text_events_are_concatenated_into_the_reply():
    runner, session_service, _ = _build_runner_with_fake_run_async(
        [[_text_event("primero"), _text_event("segundo")]]
    )
    update, context = _make_update_and_context(runner, session_service, chat_id=42)

    asyncio.run(handle_message(update, context))

    args, _ = update.message.reply_text.call_args
    assert args[0] == "primero\nsegundo"


def test_events_without_text_parts_contribute_nothing_to_the_reply():
    runner, session_service, _ = _build_runner_with_fake_run_async(
        [[_function_call_event(), _text_event("listo")]]
    )
    update, context = _make_update_and_context(runner, session_service, chat_id=42)

    asyncio.run(handle_message(update, context))

    args, _ = update.message.reply_text.call_args
    assert args[0] == "listo"


def test_no_text_at_all_falls_back_to_a_non_empty_message():
    runner, session_service, _ = _build_runner_with_fake_run_async(
        [[_function_call_event(), _no_text_event()]]
    )
    update, context = _make_update_and_context(runner, session_service, chat_id=42)

    asyncio.run(handle_message(update, context))

    update.message.reply_text.assert_awaited_once()
    (reply_arg,), _ = update.message.reply_text.call_args
    assert reply_arg


def test_non_text_messages_are_excluded_by_the_registered_filter():
    app = build_application("123:fake-token-for-tests", [111])
    generic_handler = app.handlers[1][0]
    assert isinstance(generic_handler.filters.and_filter, type(tg_filters.TEXT))


def test_exception_in_run_async_propagates():
    session_service = InMemorySessionService()
    runner = Runner(app_name=APP_NAME, agent=object(), session_service=session_service)

    async def raising_run_async(self, *, user_id, session_id, new_message):
        raise RuntimeError("boom")
        yield  # pragma: no cover - makes this an async generator

    runner.run_async = raising_run_async.__get__(runner, Runner)
    update, context = _make_update_and_context(runner, session_service, chat_id=42)

    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(handle_message(update, context))


def test_expired_session_sends_warning_before_agent_reply():
    runner, session_service, fake_run_async = _build_runner_with_fake_run_async(
        [[_text_event("uno")], [_text_event("dos")]]
    )
    update1, context1 = _make_update_and_context(
        runner, session_service, chat_id=42, timeout_seconds=0
    )
    update2, context2 = _make_update_and_context(
        runner, session_service, chat_id=42, timeout_seconds=0
    )

    asyncio.run(handle_message(update1, context1))
    asyncio.run(handle_message(update2, context2))

    assert update2.message.reply_text.await_count == 2
    first_call_args, _ = update2.message.reply_text.await_args_list[0]
    assert "expiró" in first_call_args[0]

    (_, session_id_1), (_, session_id_2) = fake_run_async.calls
    assert session_id_1 != session_id_2


def test_reset_handler_rotates_session_and_replies():
    runner, session_service, fake_run_async = _build_runner_with_fake_run_async(
        [[_text_event("uno")], [_text_event("dos")]]
    )
    update1, context1 = _make_update_and_context(runner, session_service, chat_id=42)
    asyncio.run(handle_message(update1, context1))
    pointer_before = session_store.get_current_session(42)

    reset_update = SimpleNamespace(
        message=SimpleNamespace(reply_text=AsyncMock()),
        effective_chat=SimpleNamespace(id=42),
    )
    asyncio.run(reset_handler(reset_update, context1))
    pointer_after = session_store.get_current_session(42)

    reset_update.message.reply_text.assert_awaited_once()
    args, _ = reset_update.message.reply_text.call_args
    assert "Listo" in args[0]
    assert pointer_before.session_id != pointer_after.session_id

    update2, context2 = _make_update_and_context(runner, session_service, chat_id=42)
    asyncio.run(handle_message(update2, context2))

    (_, session_id_1), (_, session_id_2) = fake_run_async.calls
    assert session_id_1 != session_id_2
    assert session_id_2 == pointer_after.session_id


def test_reset_button_text_matches_constant_used_in_handler_registration():
    app = build_application("123:fake-token-for-tests", [111])
    button_handler = [h for h in app.handlers[0] if isinstance(h, MessageHandler)][0]
    assert RESET_BUTTON_TEXT in button_handler.filters.and_filter.strings
