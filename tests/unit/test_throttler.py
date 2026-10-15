"""Unit tests for the async token-bucket rate limiter and central API throttler."""

import time

import pytest

from src.core.throttler import ApiThrottler, ServiceRateLimiter, default_api_throttler


@pytest.mark.anyio
async def test_service_rate_limiter_validation() -> None:
    """Verify invalid rate or burst values raise ValueError."""
    with pytest.raises(ValueError, match="requests_per_second must be positive"):
        ServiceRateLimiter(requests_per_second=0)

    with pytest.raises(ValueError, match="requests_per_second must be positive"):
        ServiceRateLimiter(requests_per_second=-2.5)

    with pytest.raises(ValueError, match="burst must be at least 1"):
        ServiceRateLimiter(requests_per_second=1.0, burst=0)


@pytest.mark.anyio
async def test_service_rate_limiter_acquire_zero_or_negative() -> None:
    """Acquiring 0 or negative tokens should return immediately without waiting."""
    limiter = ServiceRateLimiter(requests_per_second=1.0, burst=1)
    await limiter.acquire(0.0)
    await limiter.acquire(-1.0)
    assert limiter.rate == 1.0
    assert limiter.capacity == 1.0


@pytest.mark.anyio
async def test_service_rate_limiter_burst_and_replenish() -> None:
    """Verify bursting consumes available tokens and waiting replenishes tokens."""
    limiter = ServiceRateLimiter(requests_per_second=10.0, burst=2)
    # 2 tokens available immediately
    t0 = time.monotonic()
    await limiter.acquire(1.0)
    await limiter.acquire(1.0)
    assert time.monotonic() - t0 < 0.1

    # Next acquire will throttle for ~0.1s
    await limiter.acquire(1.0)
    assert time.monotonic() - t0 >= 0.08


@pytest.mark.anyio
async def test_service_rate_limiter_context_manager() -> None:
    """Verify rate limiter works as an async context manager."""
    limiter = ServiceRateLimiter(requests_per_second=20.0, burst=2)
    async with limiter:
        pass


@pytest.mark.anyio
async def test_api_throttler_defaults_and_configuration() -> None:
    """Verify preconfigured services and custom service configuration."""
    throttler = ApiThrottler(default_rate=5.0, default_burst=3)
    mb_limiter = throttler.get_limiter("musicbrainz")
    assert mb_limiter.rate == 1.0
    assert mb_limiter.capacity == 1.0

    lrclib_limiter = throttler.get_limiter("lrclib")
    assert lrclib_limiter.rate == 3.0

    # Custom service defaults
    custom = throttler.get_limiter("unknown_service")
    assert custom.rate == 5.0
    assert custom.capacity == 3.0

    # Configure custom service
    throttler.configure_service("custom_api", requests_per_second=8.0, burst=4)
    configured = throttler.get_limiter("custom_api")
    assert configured.rate == 8.0
    assert configured.capacity == 4.0


@pytest.mark.anyio
async def test_api_throttler_throttle_and_acquire() -> None:
    """Verify API throttler throttling calls and context manager acquisition."""
    throttler = ApiThrottler(default_rate=50.0, default_burst=5)
    await throttler.throttle("genius")
    async with throttler.acquire("lastfm"):
        pass


@pytest.mark.anyio
async def test_api_throttler_reset() -> None:
    """Verify reset restores default limiters."""
    throttler = ApiThrottler()
    throttler.configure_service("test_svc", 10.0, 5)
    assert "test_svc" in throttler._limiters
    throttler.reset()
    assert "test_svc" not in throttler._limiters
    assert "musicbrainz" in throttler._limiters


def test_default_api_throttler_instance() -> None:
    """Verify global singleton throttler is present."""
    assert isinstance(default_api_throttler, ApiThrottler)
