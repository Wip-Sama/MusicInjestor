"""Unit tests for Telegram Mini App (TMA) cryptographic `initData` validation."""

import hashlib
import hmac
import json
import time
from urllib.parse import quote

import pytest

from src.bot.auth import TelegramInitDataValidationError, validate_telegram_init_data

TEST_BOT_TOKEN = "123456789:AAFakeTestBotTokenStringForTestingPurposes"


def _build_signed_init_data(
    params: dict[str, str],
    bot_token: str = TEST_BOT_TOKEN,
    include_hash: bool = True,
    hash_override: str | None = None,
) -> str:
    """Helper creating a cryptographically signed TMA `initData` query string."""
    sorted_pairs = sorted(params.items())
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted_pairs)

    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    calc_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()

    encoded_pairs = []
    for k, v in sorted_pairs:
        encoded_pairs.append(f"{quote(k, safe='')}={quote(v, safe='')}")

    if include_hash:
        final_hash = hash_override if hash_override is not None else calc_hash
        encoded_pairs.append(f"hash={final_hash}")

    return "&".join(encoded_pairs)


def test_validate_telegram_init_data_success() -> None:
    """Verify that a validly signed initData payload successfully parses user and metadata."""
    now = int(time.time())
    user_payload = {
        "id": 999123456,
        "first_name": "Test",
        "last_name": "User",
        "username": "testuser",
        "language_code": "en",
    }
    raw_params = {
        "auth_date": str(now),
        "query_id": "AAHdF6IQAAAAAN0XohC8x_test",
        "start_param": "track_42",
        "user": json.dumps(user_payload, separators=(",", ":")),
    }
    signed_init_data = _build_signed_init_data(raw_params)

    validated = validate_telegram_init_data(signed_init_data, bot_token=TEST_BOT_TOKEN)

    assert validated["auth_date"] == now
    assert validated["query_id"] == "AAHdF6IQAAAAAN0XohC8x_test"
    assert validated["start_param"] == "track_42"
    assert isinstance(validated["user"], dict)
    assert validated["user"]["id"] == 999123456
    assert validated["user"]["username"] == "testuser"
    assert "hash" in validated


def test_validate_telegram_init_data_ignores_third_party_signature() -> None:
    """Verify that optional third-party Ed25519 signature is ignored in Path A HMAC validation."""
    now = int(time.time())
    user_payload = {"id": 12345, "first_name": "Alice"}
    raw_params = {
        "auth_date": str(now),
        "user": json.dumps(user_payload, separators=(",", ":")),
    }
    signed_init_data = _build_signed_init_data(raw_params)
    signed_with_sig = signed_init_data + "&signature=some_base64_ed25519_signature"

    validated = validate_telegram_init_data(signed_with_sig, bot_token=TEST_BOT_TOKEN)
    assert validated["user"]["id"] == 12345


def test_validate_telegram_init_data_missing_payload() -> None:
    """Verify that empty initData raises TelegramInitDataValidationError."""
    with pytest.raises(TelegramInitDataValidationError, match="Missing or empty initData"):
        validate_telegram_init_data("", bot_token=TEST_BOT_TOKEN)


def test_validate_telegram_init_data_empty_token() -> None:
    """Verify that empty bot token raises TelegramInitDataValidationError."""
    with pytest.raises(TelegramInitDataValidationError, match="Bot token cannot be empty"):
        validate_telegram_init_data("auth_date=123&hash=abc", bot_token="")


def test_validate_telegram_init_data_missing_hash() -> None:
    """Verify that initData missing 'hash' raises TelegramInitDataValidationError."""
    raw_params = {"auth_date": str(int(time.time())), "user": '{"id":1}'}
    unsigned_init_data = _build_signed_init_data(raw_params, include_hash=False)

    with pytest.raises(TelegramInitDataValidationError, match="Missing cryptographic 'hash'"):
        validate_telegram_init_data(unsigned_init_data, bot_token=TEST_BOT_TOKEN)


def test_validate_telegram_init_data_missing_or_invalid_auth_date() -> None:
    """Verify that missing or malformed auth_date raises TelegramInitDataValidationError."""
    signed_bad_date = _build_signed_init_data({"auth_date": "not_a_number"})
    with pytest.raises(TelegramInitDataValidationError, match="Missing or non-integer 'auth_date'"):
        validate_telegram_init_data(signed_bad_date, bot_token=TEST_BOT_TOKEN)


def test_validate_telegram_init_data_expired_session() -> None:
    """Verify that expired auth_date is rejected when exceeding max_age_seconds."""
    expired_date = int(time.time()) - 100_000
    signed_expired = _build_signed_init_data({"auth_date": str(expired_date)})

    with pytest.raises(TelegramInitDataValidationError, match="Expired initData session"):
        validate_telegram_init_data(signed_expired, bot_token=TEST_BOT_TOKEN, max_age_seconds=86400)


def test_validate_telegram_init_data_future_clock_skew() -> None:
    """Verify that auth_date far in the future (>60s) is rejected."""
    future_date = int(time.time()) + 120
    signed_future = _build_signed_init_data({"auth_date": str(future_date)})

    with pytest.raises(TelegramInitDataValidationError, match="Future auth_date detected"):
        validate_telegram_init_data(signed_future, bot_token=TEST_BOT_TOKEN)


def test_validate_telegram_init_data_tampered_hash() -> None:
    """Verify that an altered cryptographic hash is rejected."""
    now = int(time.time())
    signed_tampered = _build_signed_init_data(
        {"auth_date": str(now), "user": '{"id":123}'},
        hash_override="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    )

    with pytest.raises(TelegramInitDataValidationError, match="Invalid initData HMAC-SHA256 signature"):
        validate_telegram_init_data(signed_tampered, bot_token=TEST_BOT_TOKEN)


def test_validate_telegram_init_data_tampered_payload() -> None:
    """Verify that modifying the payload after signing invalidates the signature."""
    now = int(time.time())
    signed = _build_signed_init_data({"auth_date": str(now), "user": '{"id":123}'})
    tampered = signed.replace("123", "999")

    with pytest.raises(TelegramInitDataValidationError, match="Invalid initData HMAC-SHA256 signature"):
        validate_telegram_init_data(tampered, bot_token=TEST_BOT_TOKEN)


def test_validate_telegram_init_data_malformed_user_json() -> None:
    """Verify that malformed JSON inside 'user' parameter raises error."""
    now = int(time.time())
    signed_bad_json = _build_signed_init_data({"auth_date": str(now), "user": "{malformed_json"})

    with pytest.raises(TelegramInitDataValidationError, match="Invalid JSON format for 'user'"):
        validate_telegram_init_data(signed_bad_json, bot_token=TEST_BOT_TOKEN)
