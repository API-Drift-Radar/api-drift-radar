"""Which discovery failures are worth trying again, and how long to wait between tries.

Only an `inaccessible` outcome is retried, and only when every source that could not be reached failed for a
reason that may clear by itself: a timeout, a dropped connection, a server error (5xx) or a rate limit (429).
A clean answer (404, 410), an invalid contract, a refused certificate or a 4xx other than 429 gives the same
result next time, so it is recorded at once.
"""

from dataclasses import dataclass
import math
import re

from radar.domain.discovery import DiscoveryOutcome, DiscoveryStatus

# Fetch failure codes (radar.discovery.fetch) that describe a passing network problem.
TRANSIENT_CODES = frozenset({"timeout", "connection_error", "incomplete_response"})
RATE_LIMITED_STATUS = 429
_HTTP_STATUS = re.compile(r"^http_error: HTTP status (\d{3})\.")


@dataclass(frozen=True)
class RetryPolicy:
    """How often and how patiently one check retries discovery.

    `max_attempts` counts the first try, so 3 means up to two retries. The wait before retry n (1, 2, ...) is
    `base_delay * 2 ** (n - 1)`, capped at `max_delay`; a rate-limited attempt waits `rate_limit_delay`
    instead. Each discovery attempt is already bounded by its own deadline (90 seconds by default), so a check
    takes at most about `max_attempts` deadlines plus the waits.
    """

    max_attempts: int = 3
    base_delay: float = 5.0
    max_delay: float = 60.0
    rate_limit_delay: float = 60.0

    def __post_init__(self):
        if type(self.max_attempts) is not int or self.max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer.")
        for name in ("base_delay", "max_delay", "rate_limit_delay"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be a finite number of seconds, zero or more.")

    def delay(self, retry_number: int, rate_limited: bool) -> float:
        if rate_limited:
            return self.rate_limit_delay
        return min(self.max_delay, self.base_delay * 2 ** (retry_number - 1))


NO_RETRY = RetryPolicy(max_attempts=1)


def _failure(attempt):
    """(code, http_status) of a discovery attempt that could not reach its source."""
    code, _, _ = (attempt.reason or "").partition(":")
    match = _HTTP_STATUS.match(attempt.reason or "")
    return code.strip(), int(match.group(1)) if match else None


def classify(outcome: DiscoveryOutcome) -> tuple[bool, bool]:
    """(retryable, rate_limited) for a discovery outcome."""
    if outcome.status is not DiscoveryStatus.INACCESSIBLE:
        return False, False
    failures = [_failure(a) for a in outcome.attempts if a.outcome == "inaccessible"]
    if not failures:
        return False, False
    rate_limited = False
    for code, status in failures:
        if code in TRANSIENT_CODES:
            continue
        if code == "http_error" and status is not None and (status >= 500 or status == RATE_LIMITED_STATUS):
            rate_limited = rate_limited or status == RATE_LIMITED_STATUS
            continue
        return False, False  # at least one source refused for a reason that will not clear by waiting
    return True, rate_limited
