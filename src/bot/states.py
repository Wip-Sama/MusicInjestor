"""Finite State Machine (FSM) states for interactive user verification and admin flows in Telegram."""

from aiogram.fsm.state import State, StatesGroup


class IngestionStates(StatesGroup):
    """FSM states during interactive track/album metadata disambiguation and full-pipeline job tagging."""

    awaiting_confirmation = State()
    awaiting_manual_query = State()
    awaiting_job_metadata_input = State()
    awaiting_job_lyrics_input = State()
    awaiting_job_cover_input = State()


class AdminStates(StatesGroup):
    """FSM states during interactive administrator user onboarding/management."""

    awaiting_new_user_id = State()


class SongManagementStates(StatesGroup):
    """FSM states during interactive `/songs` search, tag/cover/lyrics editing, and dry-run confirmation."""

    awaiting_search_query = State()
    awaiting_metadata_input = State()
    awaiting_lyrics_input = State()
    awaiting_cover_input = State()
    awaiting_research_query = State()
    awaiting_import_payload = State()
    awaiting_dry_run_confirmation = State()
