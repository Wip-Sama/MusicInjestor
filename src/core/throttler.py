"""Asynchronous token-bucket API rate limiter and service throttler."""

import asyncio
import logging
import time
from types import TracebackType

logger = logging.getLogger(__name__)


class ServiceRateLimiter:
    """Token-bucket rate limiter for controlling request frequency to external services."""

    def __init__(self, requests_per_second: float, burst: int = 1) -> None:
        if requests_per_second <= 0:
            msg = f"requests_per_second must be positive, got {requests_per_second}"
            raise ValueError(msg)
        if burst < 1:
            msg = f"burst must be at least 1, got {burst}"
            raise ValueError(msg)

        self._rate = float(requests_per_second)
        self._capacity = float(burst)
        self._tokens = float(burst)
        self._last_time = time.monotonic()
        self._lock = asyncio.Lock()

    @property
    def rate(self) -> float:
        """Configured requests per second."""
        return self._rate

    @property
    def capacity(self) -> float:
        """Maximum burst token capacity."""
        return self._capacity

    async def acquire(self, tokens: float = 1.0) -> None:
        """Asynchronously wait until enough tokens are available, then consume them."""
        if tokens <= 0:
            return

        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_time
            self._last_time = now

            # Replenish tokens based on elapsed time
            self._tokens = min(self._capacity, self._tokens + (elapsed * self._rate))

            if self._tokens < tokens:
                needed = tokens - self._tokens
                wait_seconds = needed / self._rate
                logger.debug(
                    "Throttling rate limit: waiting %.3fs for %.2f token(s) (rate=%.1f/s)",
                    wait_seconds,
                    tokens,
                    self._rate,
                )
                await asyncio.sleep(wait_seconds)
                # After waiting, replenish up to the waited duration
                now_after = time.monotonic()
                elapsed_after = now_after - self._last_time
                self._last_time = now_after
                self._tokens = min(self._capacity, self._tokens + (elapsed_after * self._rate))

            self._tokens = max(0.0, self._tokens - tokens)

    async def __aenter__(self) -> ServiceRateLimiter:
        await self.acquire(1.0)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        pass


class ApiThrottler:
    """Registry of domain-specific rate limiters preventing intensive continuous API calls."""

    DEFAULT_LIMITS: dict[str, tuple[float, int]] = {
        # MusicBrainz strict rule: max 1 request per second
        "musicbrainz": (1.0, 1),
        # AcoustID REST API: ~3 requests per second
        "acoustid": (3.0, 2),
        # Genius API: ~4 requests per second
        "genius": (4.0, 3),
        # Last.fm API ToS: max 5 requests per second
        "lastfm": (5.0, 3),
        # LRCLIB API: ~3 requests per second
        "lrclib": (3.0, 2),
        # Cover Art Archive: ~2 requests per second
        "coverartarchive": (2.0, 2),
    }

    def __init__(self, default_rate: float = 2.0, default_burst: int = 2) -> None:
        self._default_rate = default_rate
        self._default_burst = default_burst
        self._limiters: dict[str, ServiceRateLimiter] = {}
        self._lock = asyncio.Lock()

        for service, (rate, burst) in self.DEFAULT_LIMITS.items():
            self._limiters[service] = ServiceRateLimiter(rate, burst)

    def configure_service(self, service: str, requests_per_second: float, burst: int = 1) -> None:
        """Register or update rate limit settings for a specific external service."""
        key = service.strip().lower()
        self._limiters[key] = ServiceRateLimiter(requests_per_second, burst)
        logger.info(
            "Configured API throttle for '%s': %.2f req/s (burst=%d)",
            key,
            requests_per_second,
            burst,
        )

    def get_limiter(self, service: str) -> ServiceRateLimiter:
        """Retrieve the rate limiter for a given service, creating a default one if unknown."""
        key = service.strip().lower()
        limiter = self._limiters.get(key)
        if limiter is None:
            limiter = ServiceRateLimiter(self._default_rate, self._default_burst)
            self._limiters[key] = limiter
        return limiter

    async def throttle(self, service: str, tokens: float = 1.0) -> None:
        """Wait for rate limit allocation on the specified service before firing a request."""
        limiter = self.get_limiter(service)
        await limiter.acquire(tokens)

    def acquire(self, service: str, tokens: float = 1.0) -> ServiceRateLimiter:
        """Return an async context manager limiter for the specified service."""
        return self.get_limiter(service)

    def reset(self) -> None:
        """Reset all rate limiters to initial states."""
        self._limiters.clear()
        for service, (rate, burst) in self.DEFAULT_LIMITS.items():
            self._limiters[service] = ServiceRateLimiter(rate, burst)


default_api_throttler: ApiThrottler = ApiThrottler()
