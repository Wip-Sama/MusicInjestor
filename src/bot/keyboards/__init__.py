"""Telegram keyboard builders for persistent navigation, verification, onboarding, queue, and admin flows."""

from src.bot.keyboards.admin import (
    BTN_MAIN_ADMIN,
    BTN_MAIN_INFO,
    BTN_MAIN_QUEUE,
    BTN_MAIN_SONGS,
    BTN_MAIN_SSO,
    RESERVED_MENU_BUTTONS,
    build_access_request_approval_keyboard,
    build_admin_dashboard_keyboard,
    build_guest_onboarding_keyboard,
    build_main_menu_keyboard,
    build_oidc_link_keyboard,
    build_queued_job_keyboard,
    build_user_detail_keyboard,
    build_user_list_keyboard,
)
from src.bot.keyboards.songs import (
    build_dry_run_confirmation_keyboard,
    build_input_cancel_keyboard,
    build_metadata_field_keyboard,
    build_rescan_confirmation_keyboard,
    build_research_mode_keyboard,
    build_song_action_keyboard,
    build_songs_search_menu_keyboard,
)
from src.bot.keyboards.verification import build_verification_keyboard

__all__ = [
    "BTN_MAIN_ADMIN",
    "BTN_MAIN_INFO",
    "BTN_MAIN_QUEUE",
    "BTN_MAIN_SONGS",
    "BTN_MAIN_SSO",
    "RESERVED_MENU_BUTTONS",
    "build_access_request_approval_keyboard",
    "build_admin_dashboard_keyboard",
    "build_dry_run_confirmation_keyboard",
    "build_guest_onboarding_keyboard",
    "build_input_cancel_keyboard",
    "build_main_menu_keyboard",
    "build_metadata_field_keyboard",
    "build_oidc_link_keyboard",
    "build_queued_job_keyboard",
    "build_rescan_confirmation_keyboard",
    "build_research_mode_keyboard",
    "build_song_action_keyboard",
    "build_songs_search_menu_keyboard",
    "build_user_detail_keyboard",
    "build_user_list_keyboard",
    "build_verification_keyboard",
]
