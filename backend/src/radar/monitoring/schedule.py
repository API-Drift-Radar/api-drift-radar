"""When sources are due, and what to do when Radar starts.

A source is scheduled when its configuration has `interval_minutes` (a positive number) and is not `paused`;
without an interval it is only checked manually. Pausing keeps the interval, so resuming restores the schedule;
a paused source can still be checked manually. It is due when it has never been checked, or when its latest check started at least one
interval ago. The interval is measured from the latest check of any trigger, so a manual check also resets it.

Missed runs: Radar does not catch up slot by slot. A source that missed several runs while Radar was down is
due once at startup, and its next run is one interval after that check.

This module only decides; `radar.monitoring.runner` runs the due checks.
"""

from datetime import datetime, timedelta
import math

from radar.storage.records import RUNNING

INTERVAL_KEY = "interval_minutes"
PAUSED_KEY = "paused"


def interval_of(source) -> timedelta | None:
    """The source's check interval, or None if it is paused, checked only manually, or the setting is unusable."""
    if source.config.get(PAUSED_KEY) is True:
        return None
    value = source.config.get(INTERVAL_KEY)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        return None
    return timedelta(minutes=value)


def next_due(store, source) -> datetime | None:
    """When the source is next due (possibly in the past), or None if it is not scheduled or a check is running."""
    interval = interval_of(source)
    if interval is None:
        return None
    latest = store.list_checks(source.id, limit=1)
    if not latest:
        return source.created_at  # never checked: due since it was added
    if latest[0].status == RUNNING:
        return None
    return latest[0].started_at + interval


def due_sources(store, now: datetime):
    """Scheduled sources due at `now` (a timezone-aware UTC time), oldest due first."""
    due = []
    for source in store.list_sources():
        when = next_due(store, source)
        if when is not None and when <= now:
            due.append((when, source))
    return [source for _, source in sorted(due, key=lambda pair: pair[0])]


def close_interrupted(store):
    """Close checks left running by a previous Radar process as `interrupted`. Call once when the runner starts,
    before any check runs. Returns the closed checks. Queued requests are kept: the runner starts them next.

    Only call this while holding the runner lock (`radar.monitoring.runner`): another live runner's checks would
    otherwise be closed too.
    """
    closed = []
    for check in store.list_running_checks():
        closed.append(store.fail_check(check.id, stage="interrupted", reason="restarted",
                                       error="Radar stopped before this check finished."))
    return closed
