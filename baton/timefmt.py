"""Times for people: ISO date, UTC offset and how far away, so a log line or a status
row can be read without guessing the zone ("2026-10-31 18:32 +03:30 (in 2h10m)")."""

from __future__ import annotations

from datetime import datetime, timedelta, tzinfo


def span(delta: timedelta) -> str:
    """'2h10m', '45m', '3d4h', 'under a minute'."""
    secs = int(abs(delta.total_seconds()))
    if secs < 60:
        return "under a minute"
    mins = secs // 60
    days, mins = divmod(mins, 1440)
    hours, mins = divmod(mins, 60)
    if days:
        return f"{days}d{hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h{mins:02d}m" if mins else f"{hours}h"
    return f"{mins}m"


def offset(dt: datetime) -> str:
    """+03:30 for an aware datetime."""
    off = dt.utcoffset() or timedelta(0)
    sign = "+" if off >= timedelta(0) else "-"
    total = abs(int(off.total_seconds())) // 60
    return f"{sign}{total // 60:02d}:{total % 60:02d}"


def iso(dt: datetime, tz: tzinfo) -> str:
    """'2026-10-31 18:32 +03:30' in the given zone."""
    local = dt.astimezone(tz)
    return f"{local:%Y-%m-%d %H:%M} {offset(local)}"


def when(dt: datetime | None, tz: tzinfo, now: datetime | None = None) -> str:
    """ISO time with the offset and, given `now`, how far away it is."""
    if dt is None:
        return "?"
    text = iso(dt, tz)
    if now is None:
        return text
    delta = dt - now
    if abs(delta.total_seconds()) < 60:
        return f"{text} (now)"
    return f"{text} (in {span(delta)})" if delta.total_seconds() > 0 else f"{text} ({span(delta)} ago)"


def until(dt: datetime | None, tz: tzinfo, now: datetime) -> str:
    """For a limit: 'available now' once the time has passed, else the time and how far away."""
    if dt is None or dt <= now:
        return "available now"
    return when(dt, tz, now)
