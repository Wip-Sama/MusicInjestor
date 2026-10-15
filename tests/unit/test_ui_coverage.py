"""Comprehensive unit tests covering UI helper functions, formatters, and error fallbacks."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    User,
)

from src.bot.ui import (
    _attempt_message_edit,
    _call_and_await,
    _read_fsm_dict,
    ack_callback,
    format_loading_card,
    format_screen,
    reset_fsm_state,
    resolve_back_callback,
    safe_answer,
    safe_edit_text,
    send_chat_action_safe,
    strip_copy_text_buttons,
)


def test_format_screen_and_loading_card() -> None:
    """Verify format_screen and format_loading_card rendering."""
    # Standard screen
    screen1 = format_screen(
        title="Title",
        context_lines=["Line 1", "Line 2"],
        footer_lines=["Footer 1", None, ""],
    )
    assert "<b>Title</b>" in screen1
    assert "<blockquote>Line 1\nLine 2</blockquote>" in screen1
    assert "Footer 1" in screen1

    # Expandable screen without footers
    screen2 = format_screen(
        title="Expandable",
        context_lines=["Context"],
        expandable=True,
    )
    assert "<blockquote expandable>Context</blockquote>" in screen2

    # Loading cards
    load1 = format_loading_card("Doing work", "Details here")
    assert "⏳ Doing work" in load1
    assert "<i>Please wait...</i>" in load1

    load2 = format_loading_card("Doing work", "Details", breadcrumb="Step 1/2")
    assert "<i>Step 1/2</i>" in load2


@pytest.mark.anyio
async def test_call_and_await_edge_cases() -> None:
    """Verify _call_and_await handling of non-callable, sync, and async callables."""
    assert await _call_and_await(None) is None
    assert await _call_and_await(lambda x: x * 2, 5) == 10

    async def async_fn(val: str) -> str:
        return f"async_{val}"

    assert await _call_and_await(async_fn, "test") == "async_test"


def test_strip_copy_text_buttons_edge_cases() -> None:
    """Verify strip_copy_text_buttons handles None, no-copy, and copy markup."""
    assert strip_copy_text_buttons(None) is None

    # Markup without CopyTextButton
    normal_markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Btn", callback_data="cb")]]
    )
    assert strip_copy_text_buttons(normal_markup) is None

    # Markup with CopyTextButton
    copy_btn = InlineKeyboardButton(
        text="Copy",
        copy_text=CopyTextButton(text="payload123"),
    )
    cb_btn = InlineKeyboardButton(text="Normal", callback_data="norm")
    mixed_markup = InlineKeyboardMarkup(inline_keyboard=[[copy_btn, cb_btn]])
    stripped = strip_copy_text_buttons(mixed_markup)
    assert stripped is not None
    assert len(stripped.inline_keyboard) == 1
    assert len(stripped.inline_keyboard[0]) == 1
    assert stripped.inline_keyboard[0][0].text == "Normal"


@pytest.mark.anyio
async def test_ack_callback_branches() -> None:
    """Verify ack_callback with various text, alert, and error combinations."""
    # Not callable
    await ack_callback(MagicMock(spec=[]))

    cb = MagicMock(spec=CallbackQuery)
    cb.answer = AsyncMock()

    # Text and show_alert
    await ack_callback(cb, "Alert text", show_alert=True)
    cb.answer.assert_called_with("Alert text", show_alert=True)

    # Just text
    await ack_callback(cb, "Just text")
    cb.answer.assert_called_with("Just text")

    # Just show_alert
    await ack_callback(cb, None, show_alert=True)
    cb.answer.assert_called_with(show_alert=True)

    # Neither
    await ack_callback(cb)
    cb.answer.assert_called_with()

    # TelegramBadRequest suppression
    cb.answer.side_effect = TelegramBadRequest(method=MagicMock(), message="query is too old")
    await ack_callback(cb, "Old query")

    # Other exception suppression
    cb.answer.side_effect = RuntimeError("Other error")
    await ack_callback(cb, "Error query")


@pytest.mark.anyio
async def test_attempt_message_edit() -> None:
    """Verify _attempt_message_edit with caption, text, and bot fallback."""
    # Caption only message
    cap_msg = MagicMock()
    cap_msg.caption = "Old caption"
    cap_msg.text = None
    cap_msg.edit_caption = AsyncMock()
    markup = InlineKeyboardMarkup(inline_keyboard=[])
    assert await _attempt_message_edit(cap_msg, "New caption", markup) is True
    cap_msg.edit_caption.assert_called_once_with(caption="New caption", reply_markup=markup)

    cap_msg.edit_caption.reset_mock()
    assert await _attempt_message_edit(cap_msg, "New caption", None) is True
    cap_msg.edit_caption.assert_called_once_with(caption="New caption")

    # Text message
    text_msg = MagicMock()
    text_msg.caption = None
    text_msg.text = "Old text"
    text_msg.edit_text = AsyncMock()
    assert await _attempt_message_edit(text_msg, "New text", markup) is True
    text_msg.edit_text.assert_called_once_with("New text", reply_markup=markup)

    text_msg.edit_text.reset_mock()
    assert await _attempt_message_edit(text_msg, "New text", None) is True
    text_msg.edit_text.assert_called_once_with("New text")

    # Bot fallback
    bot_msg = MagicMock(spec=[])
    bot_msg.bot = MagicMock()
    bot_msg.bot.edit_message_text = AsyncMock()
    bot_msg.chat = MagicMock()
    bot_msg.chat.id = 123
    bot_msg.message_id = 456
    assert await _attempt_message_edit(bot_msg, "Bot text", markup) is True
    bot_msg.bot.edit_message_text.assert_called_once_with(
        chat_id=123, message_id=456, text="Bot text", reply_markup=markup
    )

    bot_msg.bot.edit_message_text.reset_mock()
    assert await _attempt_message_edit(bot_msg, "Bot text", None) is True
    bot_msg.bot.edit_message_text.assert_called_once_with(chat_id=123, message_id=456, text="Bot text")

    # Completely non-editable
    empty_msg = MagicMock(spec=[])
    assert await _attempt_message_edit(empty_msg, "Text", None) is False


@pytest.mark.anyio
async def test_safe_edit_text_scenarios() -> None:
    """Verify safe_edit_text fallback handling and error recovery."""
    assert await safe_edit_text(None, "text") is False

    # Message is not modified
    msg = MagicMock()
    msg.edit_text = AsyncMock(
        side_effect=TelegramBadRequest(method=MagicMock(), message="Bad Request: message is not modified")
    )
    assert await safe_edit_text(msg, "Same text") is False

    # Copy button retry on TelegramBadRequest
    copy_btn = InlineKeyboardButton(text="Copy", copy_text=CopyTextButton(text="abc"))
    markup = InlineKeyboardMarkup(inline_keyboard=[[copy_btn]])
    msg2 = MagicMock()
    msg2.edit_text = AsyncMock(
        side_effect=[
            TelegramBadRequest(method=MagicMock(), message="Unsupported button"),
            True,
        ]
    )
    assert await safe_edit_text(msg2, "Text", reply_markup=markup) is True

    # Copy button retry raises unmodified
    msg2_unmod = MagicMock()
    msg2_unmod.edit_text = AsyncMock(
        side_effect=[
            TelegramBadRequest(method=MagicMock(), message="Unsupported button"),
            TelegramBadRequest(method=MagicMock(), message="message is not modified"),
        ]
    )
    assert await safe_edit_text(msg2_unmod, "Text", reply_markup=markup) is False

    # No text to edit fallback -> edit_caption
    msg3 = MagicMock()
    msg3.edit_text = AsyncMock(
        side_effect=TelegramBadRequest(method=MagicMock(), message="there is no text in the message to edit")
    )
    msg3.edit_caption = AsyncMock()
    assert await safe_edit_text(msg3, "Caption text", reply_markup=markup) is True
    msg3.edit_caption.assert_called_once_with(caption="Caption text", reply_markup=markup)

    msg3_no_markup = MagicMock()
    msg3_no_markup.edit_text = AsyncMock(
        side_effect=TelegramBadRequest(method=MagicMock(), message="there is no text in the message to edit")
    )
    msg3_no_markup.edit_caption = AsyncMock()
    assert await safe_edit_text(msg3_no_markup, "Caption text", None) is True
    msg3_no_markup.edit_caption.assert_called_once_with(caption="Caption text")

    # Message can't be edited fallback -> answer
    for err_msg in ("message can't be edited", "message to edit not found", "message_id_invalid"):
        msg4 = MagicMock()
        msg4.edit_text = AsyncMock(side_effect=TelegramBadRequest(method=MagicMock(), message=err_msg))
        msg4.answer = AsyncMock()
        assert await safe_edit_text(msg4, "Answer text", reply_markup=markup) is True
        msg4.answer.assert_called_once_with("Answer text", reply_markup=markup)

        msg4_no_markup = MagicMock()
        msg4_no_markup.edit_text = AsyncMock(
            side_effect=TelegramBadRequest(method=MagicMock(), message=err_msg)
        )
        msg4_no_markup.answer = AsyncMock()
        assert await safe_edit_text(msg4_no_markup, "Answer text", None) is True
        msg4_no_markup.answer.assert_called_once_with("Answer text")

    # Other TelegramBadRequest re-raised
    msg5 = MagicMock()
    msg5.edit_text = AsyncMock(side_effect=TelegramBadRequest(method=MagicMock(), message="chat not found"))
    with pytest.raises(TelegramBadRequest):
        await safe_edit_text(msg5, "Text")


@pytest.mark.anyio
async def test_safe_answer_fallbacks() -> None:
    """Verify safe_answer handling of None, success, and TelegramBadRequest stripped copy retry."""
    assert await safe_answer(None, "text") is None

    msg = MagicMock(spec=[])
    assert await safe_answer(msg, "text") is None

    msg_ok = MagicMock()
    msg_ok.answer = AsyncMock(return_value="sent")
    assert await safe_answer(msg_ok, "Hello") == "sent"
    msg_ok.answer.assert_called_once_with("Hello")

    # Retry without copy buttons on TelegramBadRequest
    copy_btn = InlineKeyboardButton(text="Copy", copy_text=CopyTextButton(text="val"))
    markup = InlineKeyboardMarkup(inline_keyboard=[[copy_btn]])

    msg_bad = MagicMock()
    msg_bad.answer = AsyncMock()
    method_mock = MagicMock()
    msg_bad.answer.side_effect = [
        TelegramBadRequest(method=method_mock, message="Unknown button type"),
        "sent_fallback",
    ]
    res = await safe_answer(msg_bad, "Text", reply_markup=markup)
    assert res == "sent_fallback"

    # TelegramBadRequest without copy buttons re-raises
    msg_err = MagicMock()
    normal_markup = InlineKeyboardMarkup(inline_keyboard=[])
    msg_err.answer = AsyncMock(side_effect=TelegramBadRequest(method=method_mock, message="Too long"))
    with pytest.raises(TelegramBadRequest):
        await safe_answer(msg_err, "Text", reply_markup=normal_markup)


@pytest.mark.anyio
async def test_send_chat_action_safe_branches() -> None:
    """Verify send_chat_action_safe handles None, CallbackQuery, Message, and exceptions safely."""
    await send_chat_action_safe(None)

    # Event without bot
    msg_no_bot = MagicMock(spec=Message)
    msg_no_bot.bot = None
    await send_chat_action_safe(msg_no_bot)

    # Valid message
    msg = MagicMock(spec=Message)
    msg.bot = MagicMock()
    msg.bot.send_chat_action = AsyncMock()
    msg.chat = MagicMock()
    msg.chat.id = 555
    await send_chat_action_safe(msg, action=ChatAction.UPLOAD_DOCUMENT)
    msg.bot.send_chat_action.assert_called_once_with(chat_id=555, action=ChatAction.UPLOAD_DOCUMENT)

    # CallbackQuery with message.chat
    cb = MagicMock(spec=CallbackQuery)
    cb.bot = MagicMock()
    cb.bot.send_chat_action = AsyncMock()
    cb.message = MagicMock()
    cb.message.chat = MagicMock()
    cb.message.chat.id = 777
    await send_chat_action_safe(cb)
    cb.bot.send_chat_action.assert_called_once_with(chat_id=777, action=ChatAction.TYPING)

    # CallbackQuery with from_user.id when message.chat is None
    cb_user = MagicMock(spec=CallbackQuery)
    cb_user.bot = MagicMock()
    cb_user.bot.send_chat_action = AsyncMock()
    cb_user.message = None
    cb_user.from_user = User(id=888, is_bot=False, first_name="Test")
    await send_chat_action_safe(cb_user)
    cb_user.bot.send_chat_action.assert_called_once_with(chat_id=888, action=ChatAction.TYPING)

    # Exception suppressed
    cb.bot.send_chat_action.side_effect = RuntimeError("Network down")
    await send_chat_action_safe(cb)


@pytest.mark.anyio
async def test_read_fsm_dict() -> None:
    """Verify _read_fsm_dict with mock, missing, or errored state."""
    assert await _read_fsm_dict(None) == {}
    assert await _read_fsm_dict(object()) == {}

    state_err = MagicMock()
    state_err.get_data = AsyncMock(side_effect=RuntimeError("Store broken"))
    assert await _read_fsm_dict(state_err) == {}


@pytest.mark.anyio
async def test_navigation_stack_and_reset_fsm() -> None:
    """Verify resolve_back_callback and reset_fsm_state."""
    # 1. State is None
    assert await resolve_back_callback(None, None, "any:route", default_back="default:cb") == "default:cb"

    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=10, user_id=10)
    state = FSMContext(storage=storage, key=key)

    # 2. First route is menu:home
    cb = await resolve_back_callback(state, None, "menu:home")
    assert cb == "menu:home"

    # 3. Navigate to songs:list from menu:home with default_back
    cb2 = await resolve_back_callback(state, None, "songs:list", default_back="menu:home")
    assert cb2 == "menu:home"

    # 4. Navigate deeper to songs:view:1
    cb3 = await resolve_back_callback(state, None, "songs:view:1")
    assert cb3 == "songs:list"

    # 5. Navigate backwards to songs:list (route already in stack -> truncates)
    cb4 = await resolve_back_callback(state, None, "songs:list")
    assert cb4 == "menu:home"

    # 6. With specific message ID and reset_to_home=True
    msg = MagicMock(spec=Message)
    msg.message_id = 99
    cb5 = await resolve_back_callback(
        state, msg, "admin:user:1", default_back="admin:users", reset_to_home=True
    )
    assert cb5 == "admin:users"

    # 7. Reset to home when default_back is menu:home
    cb6 = await resolve_back_callback(state, msg, "queue:list", default_back="menu:home", reset_to_home=True)
    assert cb6 == "menu:home"

    # 8. Start from clean state with single home element
    key2 = StorageKey(bot_id=1, chat_id=20, user_id=20)
    state2 = FSMContext(storage=storage, key=key2)
    cb_deep = await resolve_back_callback(state2, None, "songs:edit:1", default_back="songs:view:1")
    assert cb_deep == "songs:view:1"

    # 9. State update_data failure gracefully caught
    failing_state = MagicMock()
    failing_state.get_data = AsyncMock(return_value={"nav_stacks": {}})
    failing_state.update_data = AsyncMock(side_effect=RuntimeError("Storage dead"))
    res = await resolve_back_callback(failing_state, None, "test:route")
    assert res == "menu:home"

    # 10. Reset FSM
    await state.set_state("some_state")
    await state.update_data(active_song_id="123", custom_var="keep_or_check")
    await reset_fsm_state(state)
    assert await state.get_state() is None
    data = await state.get_data()
    assert data.get("active_song_id") is None

    # Reset with clear_nav=True
    await state.update_data(nav_stacks={"_latest": ["menu:home", "page1"]})
    await reset_fsm_state(state, clear_nav=True)
    assert await state.get_data() == {}

    # Reset with state=None or missing methods
    await reset_fsm_state(None)
    await reset_fsm_state(object())
