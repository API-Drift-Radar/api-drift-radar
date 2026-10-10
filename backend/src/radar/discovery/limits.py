"""Shared limits for one sequential discovery run (including references)."""

from dataclasses import dataclass
import math
import time


@dataclass(frozen=True)
class FetchLimits:
    max_requests: int = 20
    connect_timeout: float = 3.0
    read_timeout: float = 5.0
    discovery_timeout: float = 30.0
    max_document_bytes: int = 5 * 1024 * 1024
    max_total_bytes: int = 32 * 1024 * 1024
    max_redirects: int = 3

    def __post_init__(self):
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

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise BudgetExceeded("deadline_exceeded")
        return remaining

    def claim_request(self):
        self.remaining()
        if self.requests_used >= self.limits.max_requests:
            raise BudgetExceeded("request_limit")
        self.requests_used += 1

    def record_bytes(self, count):
        self.bytes_used += count
        if self.bytes_used > self.limits.max_total_bytes:
            raise BudgetExceeded("total_size_limit")
