"""Shared Telegram UI/UX primitives: safe in-place mutation, callback lifecycle, and 3-tier HTML layout."""

import inspect
import logging
from collections.abc import Sequence

from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

logger = logging.getLogger(__name__)


def format_screen(
    title: str,
    context_lines: Sequence[str],
    footer_lines: Sequence[str] | None = None,
    *,
    expandable: bool = False,
) -> str:
    """Format a 3-tier Telegram HTML message (Header `<b>`, Context `<blockquote>`, Footer `<code>`/`<i>`)."""
    quote_open = "<blockquote expandable>" if expandable else "<blockquote>"
    body = "\n".join(line for line in context_lines if line is not None)
    parts: list[str] = [
        f"<b>{title}</b>",
        f"{quote_open}{body}</blockquote>",
    ]
    if footer_lines:
        clean_footers = [line for line in footer_lines if line]
        if clean_footers:
            parts.append("\n".join(clean_footers))
    return "\n".join(parts)


def format_loading_card(
    title: str,
    detail: str = "Processing request...",
    breadcrumb: str | None = None,
) -> str:
    """Build an in-place progress card shown immediately before >500ms async operations."""
    footers = [f"<i>{breadcrumb}</i>"] if breadcrumb else ["<i>Please wait...</i>"]
    return format_screen(
        title=f"⏳ {title}",
        context_lines=[detail],
        footer_lines=footers,
    )


async def _call_and_await(fn: object, *args: object, **kwargs: object) -> object:
    """Invoke `fn(*args, **kwargs)` and await the returned value when it is awaitable."""
    if not callable(fn):
        return None
    result = fn(*args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


def strip_copy_text_buttons(markup: InlineKeyboardMarkup | None) -> InlineKeyboardMarkup | None:
    """Return a copy of `markup` without `CopyTextButton` entries for older local Bot API servers."""
    if markup is None:
        return None
    has_copy_btn = any(
        getattr(btn, "copy_text", None) is not None for row in markup.inline_keyboard for btn in row
    )
    if not has_copy_btn:
        return None
    clean_rows = [
        [btn for btn in row if getattr(btn, "copy_text", None) is None] for row in markup.inline_keyboard
    ]
    non_empty_rows = [row for row in clean_rows if row]
    return InlineKeyboardMarkup(inline_keyboard=non_empty_rows)


async def ack_callback(
    callback: CallbackQuery,
    text: str | None = None,
    *,
    show_alert: bool = False,
) -> None:
    """Answer `callback` immediately to stop the client loading spinner."""
    answer_fn = getattr(callback, "answer", None)
    if not callable(answer_fn):
        return

    try:
        if text is not None and show_alert:
            await _call_and_await(answer_fn, text, show_alert=True)
        elif text is not None:
            await _call_and_await(answer_fn, text)
        elif show_alert:
            await _call_and_await(answer_fn, show_alert=True)
        else:
            await _call_and_await(answer_fn)
    except TelegramBadRequest as exc:
        logger.debug("Ignored TelegramBadRequest on callback.answer: %s", exc)
    except Exception as exc:
        logger.debug("Callback acknowledgement skipped: %s", exc)


async def _attempt_message_edit(
    message: Message | object,
    text: str,
    reply_markup: InlineKeyboardMarkup | None,
) -> bool:
    """Execute `edit_caption`, `edit_text`, or `bot.edit_message_text` on `message`."""
    has_caption_only = (
        getattr(message, "caption", None) is not None
        and getattr(message, "text", None) is None
        and callable(getattr(message, "edit_caption", None))
    )
    edit_caption_fn = getattr(message, "edit_caption", None) if has_caption_only else None
    edit_text_fn = getattr(message, "edit_text", None)

    if callable(edit_caption_fn):
        if reply_markup is not None:
            await _call_and_await(edit_caption_fn, caption=text, reply_markup=reply_markup)
        else:
            await _call_and_await(edit_caption_fn, caption=text)
        return True

    if callable(edit_text_fn):
        if reply_markup is not None:
            await _call_and_await(edit_text_fn, text, reply_markup=reply_markup)
        else:
            await _call_and_await(edit_text_fn, text)
        return True

    bot = getattr(message, "bot", None)
    chat = getattr(message, "chat", None)
    chat_id = getattr(chat, "id", None)
    message_id = getattr(message, "message_id", None)
    bot_edit_fn = getattr(bot, "edit_message_text", None)
    if callable(bot_edit_fn) and chat_id is not None and message_id is not None:
        if reply_markup is not None:
            await _call_and_await(
                bot_edit_fn,
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=reply_markup,
            )
        else:
            await _call_and_await(
                bot_edit_fn,
                chat_id=chat_id,
                message_id=message_id,
                text=text,
            )
        return True

    return False


async def safe_edit_text(
    message: Message | object | None,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> bool:
    """Mutate `message` in-place via `edit_text` or `edit_caption`, ignoring `message is not modified`."""
    if message is None:
        return False

    try:
        return await _attempt_message_edit(message, text, reply_markup)
    except Exception as exc:
        err_lower = str(exc).lower()
        if "message is not modified" in err_lower:
            logger.debug("Suppressed unmodified message error: %s", exc)
            return False

        if isinstance(exc, TelegramBadRequest):
            stripped_markup = strip_copy_text_buttons(reply_markup)
            if stripped_markup is not None:
                try:
                    return await _attempt_message_edit(message, text, stripped_markup)
                except Exception as retry_exc:
                    if "message is not modified" in str(retry_exc).lower():
                        return False
                    exc = retry_exc
                    err_lower = str(retry_exc).lower()

            edit_caption_fn = getattr(message, "edit_caption", None)
            if "there is no text in the message to edit" in err_lower and callable(edit_caption_fn):
                if reply_markup is not None:
                    await _call_and_await(edit_caption_fn, caption=text, reply_markup=reply_markup)
                else:
                    await _call_and_await(edit_caption_fn, caption=text)
                return True

            answer_fn = getattr(message, "answer", None)
            if callable(answer_fn) and any(
                phrase in err_lower
                for phrase in ("message can't be edited", "message to edit not found", "message_id_invalid")
            ):
                if reply_markup is not None:
                    await _call_and_await(answer_fn, text, reply_markup=reply_markup)
                else:
                    await _call_and_await(answer_fn, text)
                return True

        raise


async def safe_answer(
    message: Message | object | None,
    text: str,
    reply_markup: object | None = None,
) -> object:
    """Send a reply via `message.answer`, retrying without `CopyTextButton` if rejected by local Bot API."""
    if message is None:
        return None
    answer_fn = getattr(message, "answer", None)
    if not callable(answer_fn):
        return None
    try:
        if reply_markup is not None:
            return await _call_and_await(answer_fn, text, reply_markup=reply_markup)
        return await _call_and_await(answer_fn, text)
    except TelegramBadRequest:
        if isinstance(reply_markup, InlineKeyboardMarkup):
            stripped = strip_copy_text_buttons(reply_markup)
            if stripped is not None:
                return await _call_and_await(answer_fn, text, reply_markup=stripped)
        raise


async def send_chat_action_safe(
    event: Message | CallbackQuery | object | None,
    action: ChatAction = ChatAction.TYPING,
) -> None:
    """Dispatch `bot.send_chat_action` for latency feedback when a bot and chat context exist."""
    if event is None:
        return

    bot = getattr(event, "bot", None)
    msg = event.message if isinstance(event, CallbackQuery) else event
    chat = getattr(msg, "chat", None)
    chat_id = getattr(chat, "id", None)
    if chat_id is None and isinstance(event, CallbackQuery):
        from_user = getattr(event, "from_user", None)
        chat_id = getattr(from_user, "id", None)

    send_action_fn = getattr(bot, "send_chat_action", None)
    if bot is None or chat_id is None or not callable(send_action_fn):
        return

    try:
        await _call_and_await(send_action_fn, chat_id=chat_id, action=action)
    except Exception as exc:
        logger.debug("send_chat_action skipped: %s", exc)


async def _read_fsm_dict(state: object | None) -> dict[str, object]:
    """Read FSM data dictionary safely from `state`, returning `{}` on mock or missing state."""
    if state is None:
        return {}
    get_data_fn = getattr(state, "get_data", None)
    if not callable(get_data_fn):
        return {}
    try:
        raw = await _call_and_await(get_data_fn)
        return dict(raw) if isinstance(raw, dict) else {}
    except Exception:
        return {}


async def reset_fsm_state(
    state: object | None,
    *,
    clear_nav: bool = False,
) -> None:
    """Clear active FSM prompt states while preserving `nav_stacks` history unless `clear_nav=True`."""
    if state is None:
        return
    if clear_nav:
        clear_fn = getattr(state, "clear", None)
        if callable(clear_fn):
            await _call_and_await(clear_fn)
        return

    set_state_fn = getattr(state, "set_state", None)
    if callable(set_state_fn):
        await _call_and_await(set_state_fn, None)

    data = await _read_fsm_dict(state)
    if data:
        update_fn = getattr(state, "update_data", None)
        if callable(update_fn):
            await _call_and_await(
                update_fn,
                active_song_id=None,
                active_meta_field=None,
                pending_song_proposal=None,
                job_id=None,
                job_edit_proposal=None,
                job_edit_field=None,
            )


async def resolve_back_callback(
    state: object | None,
    message: Message | object | None,
    current_route: str,
    *,
    default_back: str = "menu:home",
    reset_to_home: bool = False,
) -> str:
    """Update the per-message navigation stack for `current_route` and return the dynamic `◀ Back` target."""
    if state is None:
        return default_back

    data = await _read_fsm_dict(state)
    raw_stacks = data.get("nav_stacks")
    stacks: dict[str, list[str]] = {}
    if isinstance(raw_stacks, dict):
        for k, v in raw_stacks.items():
            if isinstance(k, str) and isinstance(v, list):
                stacks[k] = [str(item) for item in v if isinstance(item, str)]

    msg_id = getattr(message, "message_id", None)
    msg_key = str(msg_id) if isinstance(msg_id, int) else "_latest"
    stack = list(stacks.get(msg_key) or stacks.get("_latest") or ["menu:home"])
    if not stack:
        stack = ["menu:home"]

    if current_route == "menu:home":
        stack = ["menu:home"]
    elif reset_to_home:
        if default_back != "menu:home" and default_back != current_route:
            stack = ["menu:home", default_back, current_route]
        else:
            stack = ["menu:home", current_route]
    elif current_route in stack:
        idx = stack.index(current_route)
        stack = stack[: idx + 1]
    else:
        if stack == ["menu:home"] and default_back != "menu:home" and default_back != current_route:
            stack = ["menu:home", default_back, current_route]
        else:
            stack.append(current_route)

    stacks[msg_key] = stack
    stacks["_latest"] = stack
    update_fn = getattr(state, "update_data", None)
    if callable(update_fn):
        try:
            await _call_and_await(update_fn, nav_stacks=stacks)
        except Exception as exc:
            logger.debug("Skipping nav_stacks update: %s", exc)

    if len(stack) >= 2:
        return stack[-2]
    return default_back
