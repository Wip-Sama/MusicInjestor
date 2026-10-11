"""Finite State Machine (FSM) states for interactive user verification and admin flows in Telegram."""

from aiogram.fsm.state import State, StatesGroup


class IngestionStates(StatesGroup):
    """FSM states during interactive track/album metadata disambiguation."""

    awaiting_confirmation = State()
    awaiting_manual_query = State()


class AdminStates(StatesGroup):
    """FSM states during interactive administrator user onboarding/management."""

    awaiting_new_user_id = State()
