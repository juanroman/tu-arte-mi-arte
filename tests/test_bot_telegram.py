import asyncio
import contextlib
import datetime
import logging
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from google.adk.runners import Runner  # noqa: E402
from google.adk.sessions import InMemorySessionService  # noqa: E402
from google.genai import types  # noqa: E402
from telegram import Chat, Message, MessageEntity, Update, User  # noqa: E402
from telegram.ext import (  # noqa: E402
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
)

from bot import (  # noqa: E402
    preview_store,
    session_store,
    telegram_bot,  # noqa: E402
)
from bot.telegram_bot import (  # noqa: E402
    APP_NAME,
    CONFIRM_CALLBACK_PREFIX,
    GENERIC_ERROR_TEXT,
    RESET_BUTTON_TEXT,
    REVERT_CALLBACK_PREFIX,
    STALE_PREVIEW_TEXT,
    UNKNOWN_PREVIEW_TEXT,
    UPSCALING_TEXT,
    _to_markdown_v2,
    build_application,
    confirm_handler,
    global_error_handler,
    handle_message,
    load_allowed_user_ids,
    load_log_level,
    load_session_timeout_seconds,
    reset_handler,
    revert_button_handler,
    revert_command_handler,
    rotate_session,
)
from engine import tv_deploy  # noqa: E402

DEFAULT_TIMEOUT = 10_800


@pytest.fixture(autouse=True)
def _isolate_session_store(tmp_path, monkeypatch):
    monkeypatch.setattr(session_store, "DB_PATH", tmp_path / "bot_state.sqlite3")
    monkeypatch.setattr(preview_store, "DB_PATH", tmp_path / "bot_state.sqlite3")
    monkeypatch.setattr(telegram_bot, "IMAGES_DIR", tmp_path)


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


def test_load_log_level_parses_known_levels_case_insensitive():
    assert load_log_level("DEBUG") == logging.DEBUG
    assert load_log_level("info") == logging.INFO
    assert load_log_level("Warning") == logging.WARNING
    assert load_log_level("ERROR") == logging.ERROR


def test_load_log_level_defaults_to_info_when_missing():
    assert load_log_level(None) == logging.INFO
    assert load_log_level("") == logging.INFO
    assert load_log_level("   ") == logging.INFO


def test_load_log_level_defaults_to_info_when_invalid():
    assert load_log_level("not-a-level") == logging.INFO


def test_to_markdown_v2_escapes_underscores_in_image_ids():
    converted = _to_markdown_v2("listo, img_123")

    assert converted == "listo, img\\_123"
    assert "_img_123_" not in converted


def test_build_application_registers_reset_and_generic_handlers():
    app = build_application("123:fake-token-for-tests", [111, 222])

    group0 = app.handlers.get(0, [])
    group1 = app.handlers.get(1, [])

    command_handlers = [h for h in group0 if isinstance(h, CommandHandler)]
    reset_command_handlers = [
        h for h in command_handlers if h.callback is reset_handler
    ]
    assert len(reset_command_handlers) == 1
    assert reset_command_handlers[0].commands == frozenset({"nuevo"})

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


def test_build_application_registers_confirm_callback_handler():
    app = build_application("123:fake-token-for-tests", [111])

    group0 = app.handlers.get(0, [])
    callback_handlers = [h for h in group0 if isinstance(h, CallbackQueryHandler)]
    confirm_handlers = [h for h in callback_handlers if h.callback is confirm_handler]
    assert len(confirm_handlers) == 1


def test_build_application_registers_revert_command_and_callback_handlers():
    app = build_application("123:fake-token-for-tests", [111])

    group0 = app.handlers.get(0, [])

    command_handlers = [h for h in group0 if isinstance(h, CommandHandler)]
    revert_commands = [
        h for h in command_handlers if h.callback is revert_command_handler
    ]
    assert len(revert_commands) == 1
    assert revert_commands[0].commands == frozenset({"revertir"})

    callback_handlers = [h for h in group0 if isinstance(h, CallbackQueryHandler)]
    revert_callbacks = [
        h for h in callback_handlers if h.callback is revert_button_handler
    ]
    assert len(revert_callbacks) == 1


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


def _compose_preview_call_event(image_43l, image_43r, image_50) -> SimpleNamespace:
    return SimpleNamespace(
        content=types.Content(
            role="model",
            parts=[
                types.Part(
                    function_call=types.FunctionCall(
                        name="compose_preview",
                        args={
                            "image_43l": image_43l,
                            "image_43r": image_43r,
                            "image_50": image_50,
                        },
                    )
                )
            ],
        )
    )


def _compose_preview_response_event(response: dict) -> SimpleNamespace:
    return SimpleNamespace(
        content=types.Content(
            role="model",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        name="compose_preview", response=response
                    )
                )
            ],
        )
    )


def _finalize_response_event(response: dict) -> SimpleNamespace:
    return SimpleNamespace(
        content=types.Content(
            role="model",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        name="finalize_high_res", response=response
                    )
                )
            ],
        )
    )


def _deploy_to_panels_response_event(response: dict) -> SimpleNamespace:
    return SimpleNamespace(
        content=types.Content(
            role="model",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        name="deploy_to_panels", response=response
                    )
                )
            ],
        )
    )


def _materialize_batch_gallery_response_event(response: dict) -> SimpleNamespace:
    return SimpleNamespace(
        content=types.Content(
            role="model",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        name="materialize_batch_gallery", response=response
                    )
                )
            ],
        )
    )


def _fake_run_async_factory(events_by_call):
    """Returns a fake run_async bound method that yields events_by_call[i]
    (a list of lists) on the i-th call, tracking (user_id, session_id,
    text) seen.
    """
    calls = []

    async def fake_run_async(self, *, user_id, session_id, new_message):
        text = new_message.parts[0].text
        calls.append((user_id, session_id, text))
        idx = len(calls) - 1
        await asyncio.sleep(0)  # let the chat-action background task run once
        for event in events_by_call[idx]:
            yield event

    fake_run_async.calls = calls
    return fake_run_async


def _make_bot():
    return SimpleNamespace(
        send_chat_action=AsyncMock(),
        send_photo=AsyncMock(),
        send_media_group=AsyncMock(),
        send_message=AsyncMock(return_value=SimpleNamespace(edit_text=AsyncMock())),
    )


def _make_application(bot_data):
    """Fake `Application` that supports `create_task` well enough to
    exercise the real fire-and-forget wiring (dev_plan_phase_2.md §3.1):
    schedules the coroutine as a genuine `asyncio.Task` on the running
    loop, without waiting for it — same non-blocking contract as PTB's
    own `Application.create_task`. Tasks created are collected on
    `created_tasks` so a test can explicitly `await asyncio.gather(...)`
    them when it needs the background work to have settled before
    asserting on it.
    """
    application = SimpleNamespace(bot_data=bot_data, created_tasks=[])

    def create_task(coro, update=None):
        task = asyncio.ensure_future(coro)
        application.created_tasks.append(task)
        return task

    application.create_task = create_task
    return application


async def _run_and_await_background_tasks(coro, application) -> None:
    """Awaits `coro`, then awaits every task `application.create_task`
    scheduled during it -- lets a test assert on background batch-engine
    work (dev_plan_phase_2.md §3.1) after it has actually settled.
    """
    await coro
    if application.created_tasks:
        await asyncio.gather(*application.created_tasks)


def _make_update_and_context(
    runner, session_service, chat_id, text="hola", timeout_seconds=DEFAULT_TIMEOUT
):
    update = SimpleNamespace(
        message=SimpleNamespace(
            text=text,
            reply_text=AsyncMock(return_value=SimpleNamespace(edit_text=AsyncMock())),
        ),
        effective_chat=SimpleNamespace(id=chat_id),
    )
    context = SimpleNamespace(
        application=_make_application(
            {
                "runner": runner,
                "session_service": session_service,
                "session_timeout_seconds": timeout_seconds,
                "allowed_user_ids": frozenset({111}),
            }
        ),
        bot=_make_bot(),
    )
    return update, context


def _build_runner_with_fake_run_async(events_by_call):
    session_service = InMemorySessionService()
    runner = Runner(app_name=APP_NAME, agent=object(), session_service=session_service)
    fake_run_async = _fake_run_async_factory(events_by_call)
    runner.run_async = fake_run_async.__get__(runner, Runner)
    return runner, session_service, fake_run_async


def _final_reply(update):
    """The final agent text now goes out via editing the progress
    placeholder returned by reply_text, not a second reply_text call."""
    return update.message.reply_text.return_value.edit_text


def test_first_message_creates_session_and_replies_with_agent_text():
    runner, session_service, fake_run_async = _build_runner_with_fake_run_async(
        [[_text_event("listo, img_123")]]
    )
    update, context = _make_update_and_context(runner, session_service, chat_id=42)

    asyncio.run(handle_message(update, context))

    _final_reply(update).assert_awaited_once()
    args, kwargs = _final_reply(update).call_args
    assert args[0] == _to_markdown_v2("listo, img_123")

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
    (_, session_id_1, _), (_, session_id_2, _) = fake_run_async.calls
    assert session_id_1 == session_id_2


def test_two_different_chats_get_independent_sessions():
    runner, session_service, fake_run_async = _build_runner_with_fake_run_async(
        [[_text_event("uno")], [_text_event("dos")]]
    )
    update1, context1 = _make_update_and_context(runner, session_service, chat_id=1)
    update2, context2 = _make_update_and_context(runner, session_service, chat_id=2)

    asyncio.run(handle_message(update1, context1))
    asyncio.run(handle_message(update2, context2))

    (user_id_1, session_id_1, _), (user_id_2, session_id_2, _) = fake_run_async.calls
    assert user_id_1 != user_id_2
    assert session_id_1 != session_id_2


def test_multiple_text_events_are_concatenated_into_the_reply():
    runner, session_service, _ = _build_runner_with_fake_run_async(
        [[_text_event("primero"), _text_event("segundo")]]
    )
    update, context = _make_update_and_context(runner, session_service, chat_id=42)

    asyncio.run(handle_message(update, context))

    args, _ = _final_reply(update).call_args
    assert args[0] == _to_markdown_v2("primero\nsegundo")


def test_events_without_text_parts_contribute_nothing_to_the_reply():
    runner, session_service, _ = _build_runner_with_fake_run_async(
        [[_function_call_event(), _text_event("listo")]]
    )
    update, context = _make_update_and_context(runner, session_service, chat_id=42)

    asyncio.run(handle_message(update, context))

    args, _ = _final_reply(update).call_args
    assert args[0] == _to_markdown_v2("listo")


def test_no_text_at_all_falls_back_to_a_non_empty_message():
    runner, session_service, _ = _build_runner_with_fake_run_async(
        [[_function_call_event(), _no_text_event()]]
    )
    update, context = _make_update_and_context(runner, session_service, chat_id=42)

    asyncio.run(handle_message(update, context))

    _final_reply(update).assert_awaited_once()
    (reply_arg,), _ = _final_reply(update).call_args
    assert reply_arg


def test_non_text_messages_are_excluded_by_the_registered_filter():
    app = build_application("123:fake-token-for-tests", [111])
    generic_handler = app.handlers[1][0]
    assert generic_handler.filters.check_update(_make_text_update(111, "hola")) is True


def _make_text_update(user_id, text, *, with_command_entity=False):
    entities = (
        [MessageEntity(type=MessageEntity.BOT_COMMAND, offset=0, length=len(text))]
        if with_command_entity
        else []
    )
    message = Message(
        message_id=1,
        date=datetime.datetime.now(datetime.UTC),
        chat=Chat(id=user_id, type="private"),
        text=text,
        entities=entities,
        from_user=User(id=user_id, is_bot=False, first_name="Test"),
    )
    return Update(update_id=1, message=message)


def test_generic_handler_does_not_match_reset_command():
    """Regression test (2026-07-12): `/nuevo` was falling through to the
    generic handle_message handler alongside reset_handler, because
    filters.TEXT accepts command messages too and PTB evaluates handler
    groups independently — a match in group 0 never stopped group 1 from
    also matching. This burned a real Gemini call and polluted a
    freshly-reset session with an unwanted turn, found in manual testing
    of dev_plan §3.7."""
    app = build_application("123:fake-token-for-tests", [111])
    generic_handler = app.handlers[1][0]
    update = _make_text_update(111, "/nuevo", with_command_entity=True)
    assert generic_handler.filters.check_update(update) is False


def test_generic_handler_does_not_match_revert_command():
    app = build_application("123:fake-token-for-tests", [111])
    generic_handler = app.handlers[1][0]
    update = _make_text_update(111, "/revertir", with_command_entity=True)
    assert generic_handler.filters.check_update(update) is False


def test_generic_handler_does_not_match_reset_button_text():
    app = build_application("123:fake-token-for-tests", [111])
    generic_handler = app.handlers[1][0]
    update = _make_text_update(111, RESET_BUTTON_TEXT)
    assert generic_handler.filters.check_update(update) is False


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


def test_handle_message_edits_placeholder_to_error_on_exception():
    session_service = InMemorySessionService()
    runner = Runner(app_name=APP_NAME, agent=object(), session_service=session_service)

    async def raising_run_async(self, *, user_id, session_id, new_message):
        raise RuntimeError("boom")
        yield  # pragma: no cover - makes this an async generator

    runner.run_async = raising_run_async.__get__(runner, Runner)
    update, context = _make_update_and_context(runner, session_service, chat_id=42)

    with pytest.raises(RuntimeError):
        asyncio.run(handle_message(update, context))

    _final_reply(update).assert_awaited_once()
    args, kwargs = _final_reply(update).call_args
    assert args[0] == _to_markdown_v2(GENERIC_ERROR_TEXT)


def test_delivery_crash_edits_placeholder_to_error_instead_of_hanging(caplog):
    """A crash while delivering the result (album/photo, not the agent run
    itself) must not leave the '🎨 Generando…' placeholder stuck forever —
    same failure mode the keyboard-attachment bug caused in 2.4, here
    triggered by a Telegram API error during _deliver_turn_result instead.
    """
    _write_fixture_images("img_l", "img_r", "img_50")
    runner, session_service, _ = _build_runner_with_fake_run_async(
        [
            [
                _compose_preview_call_event("img_l", "img_r", "img_50"),
                _compose_preview_response_event(
                    {"image_id": "img_preview1", "path": "x"}
                ),
                _text_event("aquí está el preview"),
            ]
        ]
    )
    update, context = _make_update_and_context(runner, session_service, chat_id=42)
    context.bot.send_media_group.side_effect = RuntimeError("telegram API hiccup")

    with caplog.at_level(logging.ERROR, logger="bot.telegram_bot"):
        with pytest.raises(RuntimeError, match="telegram API hiccup"):
            asyncio.run(handle_message(update, context))

    _final_reply(update).assert_awaited_once()
    args, _ = _final_reply(update).call_args
    assert args[0] == _to_markdown_v2(GENERIC_ERROR_TEXT)
    assert any("Turno falló" in record.message for record in caplog.records)


def test_global_error_handler_logs_unhandled_exception(caplog):
    context = SimpleNamespace(error=RuntimeError("boom outside _run_and_deliver"))

    with caplog.at_level(logging.ERROR, logger="bot.telegram_bot"):
        asyncio.run(global_error_handler(None, context))

    assert any("Excepción no manejada" in record.message for record in caplog.records)


def test_handle_message_sends_generating_placeholder_then_edits_with_final_text():
    runner, session_service, _ = _build_runner_with_fake_run_async(
        [[_text_event("listo")]]
    )
    update, context = _make_update_and_context(runner, session_service, chat_id=42)

    asyncio.run(handle_message(update, context))

    first_call_args, _ = update.message.reply_text.call_args_list[0]
    assert "Generando" in first_call_args[0]
    _final_reply(update).assert_awaited_once()


def test_handle_message_sends_chat_action_typing_during_run():
    from telegram.constants import ChatAction

    runner, session_service, _ = _build_runner_with_fake_run_async(
        [[_text_event("listo")]]
    )
    update, context = _make_update_and_context(runner, session_service, chat_id=42)

    asyncio.run(handle_message(update, context))

    context.bot.send_chat_action.assert_any_await(42, ChatAction.TYPING)


def _write_fixture_images(*image_ids):
    for image_id in image_ids:
        (telegram_bot.IMAGES_DIR / f"{image_id}.jpg").write_bytes(b"fake-jpeg-bytes")


def test_compose_preview_response_sends_photo_with_confirm_button():
    _write_fixture_images("img_l", "img_r", "img_50")
    runner, session_service, _ = _build_runner_with_fake_run_async(
        [
            [
                _compose_preview_call_event("img_l", "img_r", "img_50"),
                _compose_preview_response_event(
                    {"image_id": "img_preview1", "path": "x"}
                ),
                _text_event("aquí está el preview"),
            ]
        ]
    )
    update, context = _make_update_and_context(runner, session_service, chat_id=42)

    asyncio.run(handle_message(update, context))

    context.bot.send_photo.assert_awaited_once()
    _, kwargs = context.bot.send_photo.call_args
    assert kwargs["chat_id"] == 42
    assert str(kwargs["photo"]).endswith("img_preview1.jpg")

    keyboard = kwargs["reply_markup"]
    button = keyboard.inline_keyboard[0][0]
    assert button.callback_data.startswith(CONFIRM_CALLBACK_PREFIX)

    token = button.callback_data[len(CONFIRM_CALLBACK_PREFIX) :]
    preview = preview_store.get_preview(token)
    assert preview is not None
    assert preview.chat_id == 42
    assert preview.image_43l == "img_l"
    assert preview.image_43r == "img_r"
    assert preview.image_50 == "img_50"


def test_compose_preview_error_response_sends_no_photo():
    runner, session_service, _ = _build_runner_with_fake_run_async(
        [
            [
                _compose_preview_call_event("img_l", "img_r", "img_50"),
                _compose_preview_response_event({"error": "no existe la foto"}),
                _text_event("hubo un problema"),
            ]
        ]
    )
    update, context = _make_update_and_context(runner, session_service, chat_id=42)

    asyncio.run(handle_message(update, context))

    context.bot.send_photo.assert_not_awaited()


def test_compose_preview_response_sends_panel_album_before_composed_photo():
    _write_fixture_images("img_l", "img_r", "img_50")
    runner, session_service, _ = _build_runner_with_fake_run_async(
        [
            [
                _compose_preview_call_event("img_l", "img_r", "img_50"),
                _compose_preview_response_event(
                    {"image_id": "img_preview1", "path": "x"}
                ),
                _text_event("aquí está el preview"),
            ]
        ]
    )
    update, context = _make_update_and_context(runner, session_service, chat_id=42)
    order = []
    context.bot.send_media_group.side_effect = lambda **_: order.append("album")
    context.bot.send_photo.side_effect = lambda **_: order.append("photo")

    asyncio.run(handle_message(update, context))

    context.bot.send_media_group.assert_awaited_once()
    _, kwargs = context.bot.send_media_group.call_args
    assert kwargs["chat_id"] == 42
    media = kwargs["media"]
    assert len(media) == 3
    assert media[0].caption is not None
    assert media[1].caption is None
    assert media[2].caption is None

    assert order == ["album", "photo"]


def test_compose_preview_error_response_sends_no_album():
    runner, session_service, _ = _build_runner_with_fake_run_async(
        [
            [
                _compose_preview_call_event("img_l", "img_r", "img_50"),
                _compose_preview_response_event({"error": "no existe la foto"}),
                _text_event("hubo un problema"),
            ]
        ]
    )
    update, context = _make_update_and_context(runner, session_service, chat_id=42)

    asyncio.run(handle_message(update, context))

    context.bot.send_media_group.assert_not_awaited()


def test_successful_finalize_rotates_session_silently():
    runner, session_service, fake_run_async = _build_runner_with_fake_run_async(
        [[_finalize_response_event({"image_id": "img_final1"}), _text_event("listo")]]
    )
    update, context = _make_update_and_context(runner, session_service, chat_id=42)

    asyncio.run(handle_message(update, context))

    pointer_after = session_store.get_current_session(42)
    _, session_id_used, _ = fake_run_async.calls[0]
    assert pointer_after.session_id != session_id_used

    # Silent: no extra reply beyond the placeholder-turned-final-text.
    assert update.message.reply_text.await_count == 1


def test_partial_finalize_failure_does_not_rotate_session():
    runner, session_service, fake_run_async = _build_runner_with_fake_run_async(
        [
            [
                _finalize_response_event({"image_id": "img_final1"}),
                _finalize_response_event({"error": "fallo transitorio"}),
                _text_event("una pieza falló"),
            ]
        ]
    )
    update, context = _make_update_and_context(runner, session_service, chat_id=42)

    asyncio.run(handle_message(update, context))

    pointer_after = session_store.get_current_session(42)
    _, session_id_used, _ = fake_run_async.calls[0]
    assert pointer_after.session_id == session_id_used


def test_turn_without_finalize_does_not_rotate_session():
    runner, session_service, fake_run_async = _build_runner_with_fake_run_async(
        [[_text_event("listo, img_123")]]
    )
    update, context = _make_update_and_context(runner, session_service, chat_id=42)

    asyncio.run(handle_message(update, context))

    pointer_after = session_store.get_current_session(42)
    _, session_id_used, _ = fake_run_async.calls[0]
    assert pointer_after.session_id == session_id_used


def test_message_right_after_finalize_reuses_rotated_session_without_warning():
    runner, session_service, fake_run_async = _build_runner_with_fake_run_async(
        [
            [
                _finalize_response_event({"image_id": "img_final1"}),
                _text_event("listo"),
            ],
            [_text_event("un tema nuevo")],
        ]
    )
    update1, context1 = _make_update_and_context(runner, session_service, chat_id=42)
    asyncio.run(handle_message(update1, context1))
    rotated_session_id = session_store.get_current_session(42).session_id

    update2, context2 = _make_update_and_context(runner, session_service, chat_id=42)
    asyncio.run(handle_message(update2, context2))

    # No expiry warning: the rotated session was freshly closed, not abandoned.
    assert update2.message.reply_text.await_count == 1
    _, session_id_used, _ = fake_run_async.calls[1]
    assert session_id_used == rotated_session_id


def test_long_absence_after_finalize_still_shows_expiry_warning():
    """Locks in current behavior: the session rotated by a successful
    finalize is otherwise a normal session, so if the user's next message
    comes after the inactivity timeout, get_or_rotate_session treats it
    like any other stale pointer and shows the "expiró por inactividad"
    warning — even though nothing was actually abandoned mid-work, since
    the prior session was cleanly closed on approval. This is a known,
    accepted imprecision in the warning copy, not a bug to fix here.
    """
    runner, session_service, fake_run_async = _build_runner_with_fake_run_async(
        [
            [
                _finalize_response_event({"image_id": "img_final1"}),
                _text_event("listo"),
            ],
            [_text_event("un tema nuevo, 3 días después")],
        ]
    )
    update1, context1 = _make_update_and_context(
        runner, session_service, chat_id=42, timeout_seconds=0
    )
    asyncio.run(handle_message(update1, context1))
    rotated_session_id = session_store.get_current_session(42).session_id

    update2, context2 = _make_update_and_context(
        runner, session_service, chat_id=42, timeout_seconds=0
    )
    asyncio.run(handle_message(update2, context2))

    assert update2.message.reply_text.await_count == 2
    first_call_args, _ = update2.message.reply_text.await_args_list[0]
    assert "expiró" in first_call_args[0]

    _, session_id_used, _ = fake_run_async.calls[1]
    assert session_id_used != rotated_session_id


def test_confirm_handler_rotates_session_after_successful_finalize():
    runner, session_service, fake_run_async = _build_runner_with_fake_run_async(
        [[_finalize_response_event({"image_id": "img_final1"}), _text_event("listo")]]
    )
    session_id = asyncio.run(rotate_session(session_service, 42))
    preview_store.save_preview("tok1", 42, session_id, "img_l", "img_r", "img_50", 0.0)
    update, context, query = _make_callback_update_and_context(
        runner, session_service, chat_id=42, preview_token="tok1"
    )

    asyncio.run(confirm_handler(update, context))

    pointer_after = session_store.get_current_session(42)
    assert pointer_after.session_id != session_id


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

    (_, session_id_1, _), (_, session_id_2, _) = fake_run_async.calls
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

    (_, session_id_1, _), (_, session_id_2, _) = fake_run_async.calls
    assert session_id_1 != session_id_2
    assert session_id_2 == pointer_after.session_id


def test_reset_button_text_matches_constant_used_in_handler_registration():
    app = build_application("123:fake-token-for-tests", [111])
    button_handler = [h for h in app.handlers[0] if isinstance(h, MessageHandler)][0]
    assert RESET_BUTTON_TEXT in button_handler.filters.and_filter.strings


def _make_callback_update_and_context(
    runner,
    session_service,
    chat_id,
    preview_token,
    user_id=111,
    allowed_user_ids=frozenset({111}),
    timeout_seconds=DEFAULT_TIMEOUT,
):
    query = SimpleNamespace(
        data=f"{CONFIRM_CALLBACK_PREFIX}{preview_token}", answer=AsyncMock()
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_chat=SimpleNamespace(id=chat_id),
        effective_user=SimpleNamespace(id=user_id),
    )
    context = SimpleNamespace(
        application=_make_application(
            {
                "runner": runner,
                "session_service": session_service,
                "session_timeout_seconds": timeout_seconds,
                "allowed_user_ids": allowed_user_ids,
            }
        ),
        bot=_make_bot(),
    )
    return update, context, query


def test_confirm_handler_resolves_token_and_sends_synthetic_message():
    runner, session_service, fake_run_async = _build_runner_with_fake_run_async(
        [[_text_event("subido")]]
    )
    session_id = asyncio.run(rotate_session(session_service, 42))
    preview_store.save_preview("tok1", 42, session_id, "img_l", "img_r", "img_50", 0.0)
    update, context, query = _make_callback_update_and_context(
        runner, session_service, chat_id=42, preview_token="tok1"
    )

    asyncio.run(confirm_handler(update, context))

    query.answer.assert_awaited_once()
    assert len(fake_run_async.calls) == 1
    _, _, text = fake_run_async.calls[0]
    assert "img_l" in text
    assert "img_r" in text
    assert "img_50" in text


def test_confirm_handler_sends_upscaling_placeholder_not_generic_one():
    """Aprobar dispara alta resolución + despliegue a las TVs (§3.3), no
    una generación cualquiera — el placeholder debe decirlo, en vez del
    genérico "Generando..." que confunde al usuario sobre cuánto falta."""
    runner, session_service, _ = _build_runner_with_fake_run_async(
        [[_text_event("subido")]]
    )
    session_id = asyncio.run(rotate_session(session_service, 42))
    preview_store.save_preview("tok1", 42, session_id, "img_l", "img_r", "img_50", 0.0)
    update, context, _ = _make_callback_update_and_context(
        runner, session_service, chat_id=42, preview_token="tok1"
    )

    asyncio.run(confirm_handler(update, context))

    args, _ = context.bot.send_message.call_args_list[0]
    assert args[1] == _to_markdown_v2(UPSCALING_TEXT)


def test_confirm_handler_ignores_unauthorized_user():
    runner, session_service, fake_run_async = _build_runner_with_fake_run_async(
        [[_text_event("subido")]]
    )
    session_id = asyncio.run(rotate_session(session_service, 42))
    preview_store.save_preview("tok1", 42, session_id, "img_l", "img_r", "img_50", 0.0)
    update, context, query = _make_callback_update_and_context(
        runner,
        session_service,
        chat_id=42,
        preview_token="tok1",
        user_id=999,
        allowed_user_ids=frozenset({111}),
    )

    asyncio.run(confirm_handler(update, context))

    query.answer.assert_awaited_once()
    assert len(fake_run_async.calls) == 0


def test_confirm_handler_unknown_token_replies_gracefully():
    runner, session_service, fake_run_async = _build_runner_with_fake_run_async([])
    update, context, query = _make_callback_update_and_context(
        runner, session_service, chat_id=42, preview_token="does-not-exist"
    )

    asyncio.run(confirm_handler(update, context))

    query.answer.assert_awaited_once()
    context.bot.send_message.assert_awaited_once()
    args, kwargs = context.bot.send_message.call_args
    assert args[1] == _to_markdown_v2(UNKNOWN_PREVIEW_TEXT)
    assert len(fake_run_async.calls) == 0


def test_confirm_handler_rejects_stale_token_after_session_rotated():
    runner, session_service, fake_run_async = _build_runner_with_fake_run_async(
        [[_text_event("subido")]]
    )
    old_session_id = asyncio.run(rotate_session(session_service, 42))
    preview_store.save_preview(
        "tok1", 42, old_session_id, "img_l", "img_r", "img_50", 0.0
    )

    asyncio.run(rotate_session(session_service, 42))

    update, context, query = _make_callback_update_and_context(
        runner, session_service, chat_id=42, preview_token="tok1"
    )

    asyncio.run(confirm_handler(update, context))

    query.answer.assert_awaited_once()
    context.bot.send_message.assert_awaited_once()
    args, kwargs = context.bot.send_message.call_args
    assert args[1] == _to_markdown_v2(STALE_PREVIEW_TEXT)
    assert len(fake_run_async.calls) == 0


def _make_command_update_and_context(chat_id, args=None, user_id=111):
    update = SimpleNamespace(
        message=SimpleNamespace(reply_text=AsyncMock()),
        effective_chat=SimpleNamespace(id=chat_id),
        effective_user=SimpleNamespace(id=user_id),
    )
    context = SimpleNamespace(
        args=args or [],
        application=SimpleNamespace(bot_data={"allowed_user_ids": frozenset({111})}),
        bot=_make_bot(),
    )
    return update, context


def test_revert_command_without_args_reverts_all_three_panels(monkeypatch):
    calls = []

    def fake_revert_panels(tv_names):
        calls.append(tv_names)
        return {name: {"content_id": f"MY_{name}"} for name in tv_names}

    monkeypatch.setattr(tv_deploy, "revert_panels", fake_revert_panels)
    update, context = _make_command_update_and_context(chat_id=42)

    asyncio.run(revert_command_handler(update, context))

    assert calls == [["43L", "43R", "50"]]
    update.message.reply_text.assert_awaited_once()


def test_revert_command_with_valid_arg_reverts_only_that_panel(monkeypatch):
    calls = []

    def fake_revert_panels(tv_names):
        calls.append(tv_names)
        return {name: {"content_id": f"MY_{name}"} for name in tv_names}

    monkeypatch.setattr(tv_deploy, "revert_panels", fake_revert_panels)
    update, context = _make_command_update_and_context(chat_id=42, args=["43l"])

    asyncio.run(revert_command_handler(update, context))

    assert calls == [["43L"]]


def test_revert_command_with_invalid_arg_replies_without_calling_revert(monkeypatch):
    calls = []
    monkeypatch.setattr(
        tv_deploy, "revert_panels", lambda tv_names: calls.append(tv_names)
    )
    update, context = _make_command_update_and_context(chat_id=42, args=["99"])

    asyncio.run(revert_command_handler(update, context))

    assert calls == []
    update.message.reply_text.assert_awaited_once()


def test_partial_deploy_sends_warning_with_revert_button_for_succeeded_tvs():
    runner, session_service, _ = _build_runner_with_fake_run_async(
        [
            [
                _deploy_to_panels_response_event(
                    {
                        "43L": {"content_id": "MY_43L"},
                        "43R": {"error": "no se pudo conectar"},
                        "50": {"content_id": "MY_50"},
                    }
                ),
                _text_event("desplegado parcialmente"),
            ]
        ]
    )
    update, context = _make_update_and_context(runner, session_service, chat_id=42)

    asyncio.run(handle_message(update, context))

    context.bot.send_message.assert_awaited_once()
    args, kwargs = context.bot.send_message.call_args
    keyboard = kwargs["reply_markup"]
    button = keyboard.inline_keyboard[0][0]
    assert button.callback_data == f"{REVERT_CALLBACK_PREFIX}43L,50"
    sent_text = args[1]
    assert "43R" in sent_text
    assert "no se pudo conectar" in sent_text


def test_full_success_deploy_sends_no_revert_button():
    runner, session_service, _ = _build_runner_with_fake_run_async(
        [
            [
                _deploy_to_panels_response_event(
                    {
                        "43L": {"content_id": "MY_43L"},
                        "43R": {"content_id": "MY_43R"},
                        "50": {"content_id": "MY_50"},
                    }
                ),
                _text_event("desplegado"),
            ]
        ]
    )
    update, context = _make_update_and_context(runner, session_service, chat_id=42)

    asyncio.run(handle_message(update, context))

    context.bot.send_message.assert_not_awaited()


def test_full_failure_deploy_sends_no_revert_button():
    runner, session_service, _ = _build_runner_with_fake_run_async(
        [
            [
                _deploy_to_panels_response_event(
                    {
                        "43L": {"error": "no se pudo conectar"},
                        "43R": {"error": "no se pudo conectar"},
                        "50": {"error": "no se pudo conectar"},
                    }
                ),
                _text_event("no se pudo desplegar"),
            ]
        ]
    )
    update, context = _make_update_and_context(runner, session_service, chat_id=42)

    asyncio.run(handle_message(update, context))

    context.bot.send_message.assert_not_awaited()


def test_materialize_batch_gallery_success_triggers_background_batch_engine(
    monkeypatch,
):
    """Confirmar un lote (paso 8, PRD §15.3) debe arrancar el corredor
    (draft -> finalización 4K) sin que el turno de Telegram lo espere --
    dev_plan_phase_2.md §3.1, requisito duro #7.
    """
    calls = []
    monkeypatch.setattr(
        telegram_bot,
        "run_draft_stage",
        lambda batch_id: calls.append(("draft", batch_id)),
    )
    monkeypatch.setattr(
        telegram_bot,
        "run_finalize_stage",
        lambda batch_id: calls.append(("finalize", batch_id)),
    )
    monkeypatch.setattr(
        telegram_bot, "summarize_batch", lambda batch_id: _batch_summary()
    )
    runner, session_service, _ = _build_runner_with_fake_run_async(
        [
            [
                _materialize_batch_gallery_response_event(
                    {"batch_id": "batch_abc123", "day_count": 3}
                ),
                _text_event("lote guardado"),
            ]
        ]
    )
    update, context = _make_update_and_context(runner, session_service, chat_id=42)

    asyncio.run(
        _run_and_await_background_tasks(
            handle_message(update, context), context.application
        )
    )

    assert calls == [
        ("draft", "batch_abc123"),
        ("finalize", "batch_abc123"),
    ]


def test_materialize_batch_gallery_success_does_not_block_the_turn(monkeypatch):
    """Same non-blocking guarantee as
    test_revert_handlers_do_not_block_the_event_loop: the handler must
    return (and the progress message must be edited) well before a slow
    corredor finishes -- dev_plan_phase_2.md §3.1, requisito duro #7.
    Records the tick time from inside the coroutine rather than timing
    the whole `asyncio.run` call, since `asyncio.run`'s own shutdown
    phase waits for any still-pending background task before returning
    control, which would make the outer wall-clock misleading here.
    """

    def slow_run_draft_stage(batch_id):
        time.sleep(0.2)

    monkeypatch.setattr(telegram_bot, "run_draft_stage", slow_run_draft_stage)
    monkeypatch.setattr(telegram_bot, "run_finalize_stage", lambda batch_id: None)
    runner, session_service, _ = _build_runner_with_fake_run_async(
        [
            [
                _materialize_batch_gallery_response_event(
                    {"batch_id": "batch_abc123", "day_count": 3}
                ),
                _text_event("lote guardado"),
            ]
        ]
    )
    update, context = _make_update_and_context(runner, session_service, chat_id=42)

    start = time.monotonic()
    finished_at = []

    async def run_and_record():
        await handle_message(update, context)
        finished_at.append(time.monotonic() - start)

    asyncio.run(run_and_record())

    assert finished_at[0] < 0.1
    _final_reply(update).assert_awaited_once()


def test_materialize_batch_gallery_error_response_does_not_trigger_background_engine(
    monkeypatch,
):
    calls = []
    monkeypatch.setattr(
        telegram_bot, "run_draft_stage", lambda batch_id: calls.append(batch_id)
    )
    monkeypatch.setattr(telegram_bot, "run_finalize_stage", lambda batch_id: None)
    runner, session_service, _ = _build_runner_with_fake_run_async(
        [
            [
                _materialize_batch_gallery_response_event(
                    {"error": "day_index no es consecutivo"}
                ),
                _text_event("faltó un día"),
            ]
        ]
    )
    update, context = _make_update_and_context(runner, session_service, chat_id=42)

    asyncio.run(
        _run_and_await_background_tasks(
            handle_message(update, context), context.application
        )
    )

    assert calls == []
    assert context.application.created_tasks == []


def test_turn_without_materialize_batch_gallery_does_not_trigger_background_engine(
    monkeypatch,
):
    calls = []
    monkeypatch.setattr(
        telegram_bot, "run_draft_stage", lambda batch_id: calls.append(batch_id)
    )
    monkeypatch.setattr(telegram_bot, "run_finalize_stage", lambda batch_id: None)
    runner, session_service, _ = _build_runner_with_fake_run_async(
        [[_text_event("bicicletas vintage en Santorini, listo")]]
    )
    update, context = _make_update_and_context(runner, session_service, chat_id=42)

    asyncio.run(
        _run_and_await_background_tasks(
            handle_message(update, context), context.application
        )
    )

    assert calls == []
    assert context.application.created_tasks == []


def test_batch_engine_crash_in_background_is_routed_to_global_error_handler(
    monkeypatch, caplog
):
    """A real crash inside the background corredor (e.g. an unhandled
    I/O error) must not vanish silently -- PTB's own `create_task`
    re-raises after `process_error`, which `global_error_handler`
    (registered on the real Application) logs. Exercised here against a
    real `python-telegram-bot` `Application`, not the test double, to
    confirm the actual wiring PTB provides.
    """
    from telegram.ext import ApplicationBuilder

    monkeypatch.setattr(
        telegram_bot,
        "run_draft_stage",
        lambda batch_id: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    async def scenario():
        application = ApplicationBuilder().token("123:fake-token-for-tests").build()
        application.add_error_handler(telegram_bot.global_error_handler)
        with pytest.warns(UserWarning, match="won't be automatically awaited"):
            task = application.create_task(
                telegram_bot._run_batch_engine_in_background(
                    "batch_abc123", _make_bot(), 42
                )
            )
        with contextlib.suppress(RuntimeError):
            await task

    with caplog.at_level(logging.ERROR, logger="bot.telegram_bot"):
        asyncio.run(scenario())

    assert any("Excepción no manejada" in record.message for record in caplog.records)


def _batch_summary(
    *,
    theme="Otoño",
    day_count=1,
    stage_counts=None,
    needs_attention_policy_rejection=None,
    needs_attention_technical=None,
    days=None,
) -> dict:
    """Fabrica un dict con el shape exacto de
    `engine.batch.summarize_batch` (§2.4) para monkeypatchear
    `telegram_bot.summarize_batch` sin tocar SQLite real -- mismo
    principio que los demás fakes de este archivo, que trabajan a nivel
    de tool-call-event en vez de contra el estado persistido real.
    """
    return {
        "batch_id": "batch_abc123",
        "theme": theme,
        "day_count": day_count,
        "stage_counts": stage_counts if stage_counts is not None else {"finalized": 3},
        "needs_attention_policy_rejection": needs_attention_policy_rejection or [],
        "needs_attention_technical": needs_attention_technical or [],
        "days": days if days is not None else [],
    }


def _batch_day_summary(day_index, panel_image_ids: dict) -> dict:
    return {
        "day_index": day_index,
        "mode": "independiente",
        "sub_group": "Sub-grupo 1",
        "panels": {
            panel: {"stage": "finalized", "image_id": image_id, "error": None}
            for panel, image_id in panel_image_ids.items()
        },
    }


def test_batch_report_full_success_sends_summary_and_one_album(monkeypatch):
    """Un lote sin ningún needs_attention manda un texto de éxito simple
    (sin sección de fallas) seguido de un único álbum con todas las fotos
    -- dev_plan_phase_2.md §3.2.
    """
    _write_fixture_images("img_1", "img_2", "img_3")
    monkeypatch.setattr(telegram_bot, "run_draft_stage", lambda batch_id: None)
    monkeypatch.setattr(telegram_bot, "run_finalize_stage", lambda batch_id: None)
    monkeypatch.setattr(
        telegram_bot,
        "summarize_batch",
        lambda batch_id: _batch_summary(
            day_count=1,
            days=[
                _batch_day_summary(1, {"43L": "img_1", "43R": "img_2", "50": "img_3"})
            ],
        ),
    )
    runner, session_service, _ = _build_runner_with_fake_run_async(
        [
            [
                _materialize_batch_gallery_response_event(
                    {"batch_id": "batch_abc123", "day_count": 1}
                ),
                _text_event("lote guardado"),
            ]
        ]
    )
    update, context = _make_update_and_context(runner, session_service, chat_id=42)

    asyncio.run(
        _run_and_await_background_tasks(
            handle_message(update, context), context.application
        )
    )

    context.bot.send_message.assert_awaited_once()
    text_args, _ = context.bot.send_message.call_args
    assert "lista" in text_args[1]
    assert "atención" not in text_args[1]

    context.bot.send_media_group.assert_awaited_once()
    _, media_kwargs = context.bot.send_media_group.call_args
    assert len(media_kwargs["media"]) == 3


def test_batch_report_paginates_albums_over_ten_photos(monkeypatch):
    """Requisito duro #8: un lote de más de 10 fotos manda varios álbumes,
    cada uno de a lo más 10, pausados entre sí en vez de en una sola
    llamada.
    """
    image_ids = [f"img_{i}" for i in range(12)]
    _write_fixture_images(*image_ids)
    monkeypatch.setattr(telegram_bot, "run_draft_stage", lambda batch_id: None)
    monkeypatch.setattr(telegram_bot, "run_finalize_stage", lambda batch_id: None)
    monkeypatch.setattr(
        telegram_bot,
        "summarize_batch",
        lambda batch_id: _batch_summary(
            day_count=4,
            stage_counts={"finalized": 12},
            days=[
                _batch_day_summary(
                    i + 1,
                    {
                        "43L": image_ids[i * 3],
                        "43R": image_ids[i * 3 + 1],
                        "50": image_ids[i * 3 + 2],
                    },
                )
                for i in range(4)
            ],
        ),
    )
    sleep_calls = []
    real_sleep = asyncio.sleep

    async def spying_sleep(seconds):
        sleep_calls.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(telegram_bot.asyncio, "sleep", spying_sleep)
    runner, session_service, _ = _build_runner_with_fake_run_async(
        [
            [
                _materialize_batch_gallery_response_event(
                    {"batch_id": "batch_abc123", "day_count": 4}
                ),
                _text_event("lote guardado"),
            ]
        ]
    )
    update, context = _make_update_and_context(runner, session_service, chat_id=42)

    asyncio.run(
        _run_and_await_background_tasks(
            handle_message(update, context), context.application
        )
    )

    assert context.bot.send_media_group.await_count == 2
    album_sizes = [
        len(call.kwargs["media"])
        for call in context.bot.send_media_group.await_args_list
    ]
    assert album_sizes == [10, 2]
    assert sleep_calls.count(telegram_bot._PROACTIVE_SEND_PACING_SECONDS) == 2


def test_batch_report_mixed_failure_distinguishes_policy_vs_technical(monkeypatch):
    """El texto del reporte distingue un rechazo de política (nunca
    reintentado, ofrece pivote) de una falla técnica agotada (ofrece
    reintento) -- nunca infiere la distinción del texto de error, la lee
    tal cual de las dos listas separadas de `summarize_batch`. Los
    paneles sin `image_id` (los que fallaron) no rompen el álbum.
    """
    _write_fixture_images("img_1")
    monkeypatch.setattr(telegram_bot, "run_draft_stage", lambda batch_id: None)
    monkeypatch.setattr(telegram_bot, "run_finalize_stage", lambda batch_id: None)
    monkeypatch.setattr(
        telegram_bot,
        "summarize_batch",
        lambda batch_id: _batch_summary(
            day_count=2,
            stage_counts={"finalized": 1, "needs_attention": 2},
            needs_attention_policy_rejection=[
                {"day_index": 1, "panel": "43R", "error": "policy"}
            ],
            needs_attention_technical=[
                {"day_index": 2, "panel": "50", "error": "timeout", "attempts": 2}
            ],
            days=[
                _batch_day_summary(1, {"43L": "img_1"}),
            ],
        ),
    )
    runner, session_service, _ = _build_runner_with_fake_run_async(
        [
            [
                _materialize_batch_gallery_response_event(
                    {"batch_id": "batch_abc123", "day_count": 2}
                ),
                _text_event("lote guardado"),
            ]
        ]
    )
    update, context = _make_update_and_context(runner, session_service, chat_id=42)

    asyncio.run(
        _run_and_await_background_tasks(
            handle_message(update, context), context.application
        )
    )

    text_args, _ = context.bot.send_message.call_args
    report_text = text_args[1]
    assert "rechazo de política" in report_text
    assert "reintentos" in report_text

    context.bot.send_media_group.assert_awaited_once()
    _, media_kwargs = context.bot.send_media_group.call_args
    assert len(media_kwargs["media"]) == 1


def test_batch_report_total_failure_sends_text_but_no_album(monkeypatch):
    """Un lote donde ningún panel produjo imagen manda el texto (con la
    sección de fallas) pero nunca llama a send_media_group -- no hay
    fotos que mandar.
    """
    monkeypatch.setattr(telegram_bot, "run_draft_stage", lambda batch_id: None)
    monkeypatch.setattr(telegram_bot, "run_finalize_stage", lambda batch_id: None)
    monkeypatch.setattr(
        telegram_bot,
        "summarize_batch",
        lambda batch_id: _batch_summary(
            day_count=1,
            stage_counts={"needs_attention": 3},
            needs_attention_technical=[
                {"day_index": 1, "panel": "43L", "error": "timeout", "attempts": 2},
                {"day_index": 1, "panel": "43R", "error": "timeout", "attempts": 2},
                {"day_index": 1, "panel": "50", "error": "timeout", "attempts": 2},
            ],
            days=[_batch_day_summary(1, {})],
        ),
    )
    runner, session_service, _ = _build_runner_with_fake_run_async(
        [
            [
                _materialize_batch_gallery_response_event(
                    {"batch_id": "batch_abc123", "day_count": 1}
                ),
                _text_event("lote guardado"),
            ]
        ]
    )
    update, context = _make_update_and_context(runner, session_service, chat_id=42)

    asyncio.run(
        _run_and_await_background_tasks(
            handle_message(update, context), context.application
        )
    )

    context.bot.send_message.assert_awaited_once()
    context.bot.send_media_group.assert_not_awaited()


def test_batch_report_does_not_block_the_turn(monkeypatch):
    """Mismo criterio de no bloqueo que
    test_materialize_batch_gallery_success_does_not_block_the_turn, ahora
    para el envío del reporte proactivo: el turno retorna (y el reporte
    todavía no se manda) mucho antes de que un corredor lento termine.
    """
    _write_fixture_images("img_1")

    def slow_run_draft_stage(batch_id):
        time.sleep(0.2)

    monkeypatch.setattr(telegram_bot, "run_draft_stage", slow_run_draft_stage)
    monkeypatch.setattr(telegram_bot, "run_finalize_stage", lambda batch_id: None)
    monkeypatch.setattr(
        telegram_bot,
        "summarize_batch",
        lambda batch_id: _batch_summary(days=[_batch_day_summary(1, {"43L": "img_1"})]),
    )
    runner, session_service, _ = _build_runner_with_fake_run_async(
        [
            [
                _materialize_batch_gallery_response_event(
                    {"batch_id": "batch_abc123", "day_count": 1}
                ),
                _text_event("lote guardado"),
            ]
        ]
    )
    update, context = _make_update_and_context(runner, session_service, chat_id=42)

    start = time.monotonic()
    finished_at = []
    report_sent_before_turn_finished = []

    async def run_and_record():
        await handle_message(update, context)
        finished_at.append(time.monotonic() - start)
        report_sent_before_turn_finished.append(
            context.bot.send_message.await_count > 0
        )

    asyncio.run(run_and_record())

    assert finished_at[0] < 0.1
    assert report_sent_before_turn_finished == [False]
    _final_reply(update).assert_awaited_once()


def _make_revert_callback_update_and_context(
    chat_id, tv_names, user_id=111, allowed_user_ids=frozenset({111})
):
    query = SimpleNamespace(
        data=f"{REVERT_CALLBACK_PREFIX}{','.join(tv_names)}", answer=AsyncMock()
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_chat=SimpleNamespace(id=chat_id),
        effective_user=SimpleNamespace(id=user_id),
    )
    context = SimpleNamespace(
        application=SimpleNamespace(bot_data={"allowed_user_ids": allowed_user_ids}),
        bot=_make_bot(),
    )
    return update, context, query


def test_revert_button_handler_reverts_the_named_tvs(monkeypatch):
    calls = []

    def fake_revert_panels(tv_names):
        calls.append(tv_names)
        return {name: {"content_id": f"MY_{name}"} for name in tv_names}

    monkeypatch.setattr(tv_deploy, "revert_panels", fake_revert_panels)
    update, context, query = _make_revert_callback_update_and_context(
        chat_id=42, tv_names=["43L", "50"]
    )

    asyncio.run(revert_button_handler(update, context))

    query.answer.assert_awaited_once()
    assert calls == [["43L", "50"]]
    context.bot.send_message.assert_awaited_once()


def test_revert_button_handler_ignores_unauthorized_user(monkeypatch):
    calls = []
    monkeypatch.setattr(
        tv_deploy, "revert_panels", lambda tv_names: calls.append(tv_names) or {}
    )
    update, context, query = _make_revert_callback_update_and_context(
        chat_id=42, tv_names=["43L"], user_id=999, allowed_user_ids=frozenset({111})
    )

    asyncio.run(revert_button_handler(update, context))

    query.answer.assert_awaited_once()
    assert calls == []


@pytest.mark.parametrize("handler_name", ["command", "button"])
def test_revert_handlers_do_not_block_the_event_loop(monkeypatch, handler_name):
    """revert_panels can block for up to ~35s per TV against an
    unresponsive TV (tv_deploy's own watchdog deadline + grace period) —
    calling it synchronously inside these async handlers would freeze the
    bot's single event loop for every other concurrent chat during that
    window. It must be offloaded to a thread.
    """

    def slow_revert_panels(tv_names):
        time.sleep(0.2)
        return {name: {"content_id": f"MY_{name}"} for name in tv_names}

    monkeypatch.setattr(tv_deploy, "revert_panels", slow_revert_panels)

    if handler_name == "command":
        update, context = _make_command_update_and_context(chat_id=42)
        coro = revert_command_handler(update, context)
    else:
        update, context, _query = _make_revert_callback_update_and_context(
            chat_id=42, tv_names=["43L"]
        )
        coro = revert_button_handler(update, context)

    start = time.monotonic()
    first_tick_at = []

    async def ticker():
        await asyncio.sleep(0.01)
        first_tick_at.append(time.monotonic() - start)

    async def run_both():
        await asyncio.gather(coro, ticker())

    asyncio.run(run_both())

    assert first_tick_at[0] < 0.1


def test_revert_button_handler_ignores_malformed_callback_data(monkeypatch):
    calls = []
    monkeypatch.setattr(
        tv_deploy, "revert_panels", lambda tv_names: calls.append(tv_names) or {}
    )
    query = SimpleNamespace(data=f"{REVERT_CALLBACK_PREFIX}", answer=AsyncMock())
    update = SimpleNamespace(
        callback_query=query,
        effective_chat=SimpleNamespace(id=42),
        effective_user=SimpleNamespace(id=111),
    )
    context = SimpleNamespace(
        application=SimpleNamespace(bot_data={"allowed_user_ids": frozenset({111})}),
        bot=_make_bot(),
    )

    asyncio.run(revert_button_handler(update, context))

    assert calls == []
    context.bot.send_message.assert_not_awaited()
