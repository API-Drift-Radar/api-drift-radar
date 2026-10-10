"""Shared limits for one sequential discovery run (navigation, configuration fetching, model-selected links
and reference capture all draw on the same budget)."""

from contextlib import contextmanager
from dataclasses import dataclass
import math
import time

# Why a fetch was refused because of the run's limits rather than because of the URL. One definition, used everywhere.
BUDGET_STOP_CODES = frozenset({"request_limit", "navigation_limit", "host_limit", "deadline_exceeded",
                               "total_size_limit"})


@dataclass(frozen=True)
class FetchLimits:
    max_requests: int = 40
    connect_timeout: float = 3.0
    read_timeout: float = 5.0
    discovery_timeout: float = 90.0
    max_document_bytes: int = 5 * 1024 * 1024
    max_total_bytes: int = 32 * 1024 * 1024
    max_redirects: int = 3
    max_hosts: int = 8  # distinct host:port pairs one run may contact
    # Requests held back for capturing the selected contract's references; navigation cannot spend them.
    # None means a quarter of max_requests.
    reference_reserve: int | None = None

    @classmethod
    def deep(cls, **changes):
        """A larger budget for a deliberate, deeper search (the research proposal: 60 requests, 120 seconds)."""
        return cls(**{"max_requests": 60, "discovery_timeout": 120.0, **changes})

    @property
    def reserve(self) -> int:
        return self.max_requests // 4 if self.reference_reserve is None else self.reference_reserve

    def __post_init__(self):
        if type(self.max_hosts) is not int or self.max_hosts < 1:
            raise ValueError("Invalid max_hosts.")
        if self.reference_reserve is not None and (type(self.reference_reserve) is not int
                                                   or not 0 <= self.reference_reserve < self.max_requests):
            raise ValueError("Invalid reference_reserve.")
        for name in ("max_requests", "max_document_bytes", "max_total_bytes", "max_redirects"):
            value = getattr(self, name)
            if type(value) is not int or value < (0 if name == "max_redirects" else 1):
                raise ValueError(f"Invalid {name}.")
        for name in ("connect_timeout", "read_timeout", "discovery_timeout"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"Invalid {name}.")


class BudgetExceeded(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class DiscoveryBudget:
    """Create once per discovery run; do not share across concurrent workers."""

    def __init__(self, limits=None):
        self.limits = limits or FetchLimits()
        self.deadline = time.monotonic() + self.limits.discovery_timeout
        self.requests_used = 0
        self.bytes_used = 0
        self.hosts = set()
        self._references = False

    @contextmanager
    def reference_phase(self):
        """Inside this block the requests reserved for reference capture may be spent."""
        previous, self._references = self._references, True
        try:
            yield
        finally:
            self._references = previous

    def navigation_remaining(self) -> int:
        """Requests navigation may still spend: the total minus what was used and the reference reserve."""
        return max(0, self.limits.max_requests - self.limits.reserve - self.requests_used)

    def navigation_exhausted(self) -> bool:
        return self.navigation_remaining() == 0

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise BudgetExceeded("deadline_exceeded")
        return remaining

    def claim_request(self, host=None):
        self.remaining()
        if self.requests_used >= self.limits.max_requests:
            raise BudgetExceeded("request_limit")
        if not self._references:
            if self.requests_used >= self.limits.max_requests - self.limits.reserve:
                raise BudgetExceeded("navigation_limit")
            if host is not None and host not in self.hosts and len(self.hosts) >= self.limits.max_hosts:
                raise BudgetExceeded("host_limit")
        if host is not None:
            self.hosts.add(host)
        self.requests_used += 1

    def record_bytes(self, count):
        self.bytes_used += count
        if self.bytes_used > self.limits.max_total_bytes:
            raise BudgetExceeded("total_size_limit")
