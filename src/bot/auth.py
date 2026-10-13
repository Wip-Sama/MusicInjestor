"""Telegram Mini App (TMA) zero-trust cryptographic authentication validator (`HMAC-SHA256`)."""

import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import parse_qsl


class TelegramInitDataValidationError(Exception):
    """Raised when Telegram Mini App `initData` is missing, expired, or cryptographically invalid."""


def validate_telegram_init_data(
    init_data: str,
    bot_token: str,
    max_age_seconds: int = 86400,
    now_ts: int | None = None,
) -> dict[str, Any]:
    """Validate raw Telegram Mini App `initData` payload using first-party HMAC-SHA256.

    Adheres strictly to the Telegram Mini Apps specification:
    1. Exactly one URL-decode pass (`urllib.parse.parse_qsl`) on the raw query string.
    2. Extracts and pops `hash` (and ignores `signature` if present).
    3. Validates `auth_date` freshness against `max_age_seconds`.
    4. Lexicographically sorts remaining keys (`key=value`) joined by newline (`\\n`).
    5. Computes secret key via HMAC-SHA256(`WebAppData`, `bot_token`).
    6. Verifies hash in constant time via `hmac.compare_digest`.
    7. Deserializes `user` JSON payload into a dictionary without re-serializing.

    Returns the parsed parameter dictionary containing validated user information and metadata.
    """
    if not init_data or not init_data.strip():
        raise TelegramInitDataValidationError("Missing or empty initData payload.")

    if not bot_token or not bot_token.strip():
        raise TelegramInitDataValidationError("Bot token cannot be empty for initData validation.")

    # 1. Single URL-decode pass
    try:
        parsed_pairs = dict(parse_qsl(init_data.strip(), keep_blank_values=True))
    except Exception as exc:
        raise TelegramInitDataValidationError(f"Malformed initData query string: {exc}") from exc

    # 2. Extract and remove hash
    received_hash = parsed_pairs.pop("hash", None)
    if not received_hash:
        raise TelegramInitDataValidationError("Missing cryptographic 'hash' in initData.")

    signature = parsed_pairs.get("signature")

    # 3. Freshness check
    auth_date_raw = parsed_pairs.get("auth_date")
    if not auth_date_raw or not auth_date_raw.isdigit():
        raise TelegramInitDataValidationError("Missing or non-integer 'auth_date' parameter in initData.")

    auth_date = int(auth_date_raw)
    current_time = int(time.time()) if now_ts is None else now_ts
    if current_time < auth_date:
        # Allow slight clock skew up to 60 seconds into the future
        if (auth_date - current_time) > 60:
            raise TelegramInitDataValidationError("Future auth_date detected in initData (clock skew > 60s).")
    elif (current_time - auth_date) > max_age_seconds:
        raise TelegramInitDataValidationError(
            f"Expired initData session: auth_date age {current_time - auth_date}s exceeds {max_age_seconds}s."
        )

    # 4. Compute secret key = HMAC-SHA256(b"WebAppData", bot_token)
    secret_key = hmac.new(
        key=b"WebAppData",
        msg=bot_token.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()

    # 5. Build candidate data check strings:
    # Path A: without 'signature' (standard specification)
    pairs_without_sig = {k: v for k, v in parsed_pairs.items() if k != "signature"}
    data_check_string_without_sig = "\n".join(f"{k}={v}" for k, v in sorted(pairs_without_sig.items()))
    calculated_hash_without_sig = hmac.new(
        key=secret_key,
        msg=data_check_string_without_sig.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()

    # Path B: with 'signature' (if client or Telegram Bot API build included signature)
    is_valid = hmac.compare_digest(calculated_hash_without_sig, received_hash.lower())
    if not is_valid and signature is not None:
        data_check_string_with_sig = "\n".join(f"{k}={v}" for k, v in sorted(parsed_pairs.items()))
        calculated_hash_with_sig = hmac.new(
            key=secret_key,
            msg=data_check_string_with_sig.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).hexdigest()
        is_valid = hmac.compare_digest(calculated_hash_with_sig, received_hash.lower())

    if not is_valid:
        raise TelegramInitDataValidationError("Invalid initData HMAC-SHA256 signature.")

    # 6. Convert user JSON string into structured dict
    result: dict[str, Any] = dict(pairs_without_sig)
    result["hash"] = received_hash
    result["auth_date"] = auth_date
    if signature is not None:
        result["signature"] = signature

    if "user" in parsed_pairs:
        try:
            result["user"] = json.loads(parsed_pairs["user"])
        except Exception as exc:
            raise TelegramInitDataValidationError(f"Invalid JSON format for 'user' parameter: {exc}") from exc

    return result
