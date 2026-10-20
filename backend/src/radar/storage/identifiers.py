"""Record identifiers and timestamps used throughout Radar storage.

Identifiers are a short kind prefix plus 32 lowercase hex characters from a
random UUID, e.g. ``chk_3f9a0c...``. They are assigned once, never reused,
and never renumbered, so other components and later features can keep
referring to a record indefinitely. The prefix tells a reader what kind of
record an identifier names; the random part reveals nothing about how many
records exist or in what order they were made.

Timestamps are stored as UTC ISO-8601 text with microseconds and an explicit
``+00:00`` offset, e.g. ``2026-10-12T22:56:02.123456+00:00``. Every stored
timestamp has the same length, so sorting the text sorts by time.
"""

import re
import uuid
from datetime import datetime, timezone

SOURCE = "src"
CHECK = "chk"
SNAPSHOT = "snap"
CHANGE = "chg"

KINDS = frozenset({SOURCE, CHECK, SNAPSHOT, CHANGE})

_ID_PATTERN = re.compile(r"(src|chk|snap|chg)_[0-9a-f]{32}")
_TIMESTAMP_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}\+00:00"
)


def new_id(kind):
    """Return a fresh identifier for a record of ``kind`` (e.g. ``CHECK``)."""
    if kind not in KINDS:
        raise ValueError(f"Unknown record kind: {kind!r}.")
    return f"{kind}_{uuid.uuid4().hex}"


def is_valid_id(value, kind=None):
    """True if ``value`` is a well-formed identifier, of ``kind`` if given.

    Lets callers reject malformed input (for example from a URL) before it
    reaches the database. Says nothing about whether the record exists.
    """
    if not isinstance(value, str):
        return False
    match = _ID_PATTERN.fullmatch(value)
    if match is None:
        return False
    return kind is None or match.group(1) == kind


def utc_now():
    """The current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def format_timestamp(value):
    """Convert an aware datetime to Radar's stored timestamp text.

    Naive datetimes are rejected: a time without a timezone is ambiguous,
    and guessing would silently misorder history.
    """
    if not isinstance(value, datetime):
        raise TypeError(f"Expected a datetime, got {type(value).__name__}.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timestamps must be timezone-aware.")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def parse_timestamp(text):
    """Read stored timestamp text back as an aware UTC datetime.

    Accepts only the exact stored format, so corrupted or hand-edited values
    are reported instead of being half-understood.
    """
    if not isinstance(text, str) or not _TIMESTAMP_PATTERN.fullmatch(text):
        raise ValueError(f"Not a stored Radar timestamp: {text!r}.")
    return datetime.fromisoformat(text)
