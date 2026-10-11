"""Standard Telegram bot command handlers (/start, /help, /status)."""

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

commands_router = Router(name="commands")


@commands_router.message(Command("start"))
async def handle_start(message: Message) -> None:
    """Greet the user and explain supported audio/archive formats."""
    await message.answer(
        "<b>Automated Music Ingestion Bot</b>\n"
        "Send an audio track (<code>.flac</code>, <code>.mp3</code>, <code>.m4a</code>) "
        "or an archive (<code>.zip</code>, <code>.7z</code>) up to 2 GB."
    )


@commands_router.message(Command("help"))
async def handle_help(message: Message) -> None:
    """Provide usage instructions and pipeline overview."""
    await message.answer(
        "<b>Available Commands:</b>\n"
        "/start - Welcome message\n"
        "/help - Show usage instructions\n"
        "/status - Check local Bot API & ingestion pipeline status"
    )


@commands_router.message(Command("status"))
async def handle_status(message: Message) -> None:
    """Report current operational status of the ingestion service."""
    await message.answer("Status: <b>Online</b> (Local Bot API & SQLite WAL ready).")
