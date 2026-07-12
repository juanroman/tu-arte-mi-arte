import asyncio
import datetime
import sys
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
    handle_message,
    load_allowed_user_ids,
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
        application=SimpleNamespace(
            bot_data={
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


def test_delivery_crash_edits_placeholder_to_error_instead_of_hanging():
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

    with pytest.raises(RuntimeError, match="telegram API hiccup"):
        asyncio.run(handle_message(update, context))

    _final_reply(update).assert_awaited_once()
    args, _ = _final_reply(update).call_args
    assert args[0] == _to_markdown_v2(GENERIC_ERROR_TEXT)


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
        application=SimpleNamespace(
            bot_data={
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
