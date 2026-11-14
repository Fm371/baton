"""Wall clock, kept behind a small interface so tests can fake it."""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone, tzinfo
from pathlib import Path


def local_zone() -> tzinfo:
    """The machine's own time zone as a real zone (it follows daylight saving time), found from
    $TZ, /etc/timezone or the /etc/localtime link. When none of those names a zone, the current
    fixed UTC offset is the fallback, which is wrong across a clock change."""
    from zoneinfo import ZoneInfo
    names = [os.environ.get("TZ", "").lstrip(":")]
    try:
        names.append(Path("/etc/timezone").read_text().strip())
    except OSError:
        pass
    try:
        link = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in link:
            names.append(link.split("zoneinfo/", 1)[1])
    except OSError:
        pass
    for name in names:
        if name:
            try:
                return ZoneInfo(name)
            except (KeyError, ValueError, OSError):
                continue
    return datetime.now().astimezone().tzinfo


STEP_S = 1.0      # how often a wait looks again at the clock and at should_stop(): Ctrl+C reacts within this


class Clock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)

    def sleep_until(self, when: datetime, should_stop=lambda: False) -> bool:
        """Sleep in short steps, re-reading the wall clock each time, so a
        suspended laptop wakes up at the right moment and a stop is seen within a second. Returns False if
        should_stop() asked to stop early."""
        while True:
            if should_stop():
                return False
            left = (when - self.now()).total_seconds()
            if left <= 0:
                return True
            self.sleep(min(left, STEP_S))
