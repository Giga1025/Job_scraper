"""What the job-board readers share with job_monitor.py: request headers, time helpers and the log."""
import logging
from datetime import datetime, timezone

# job_monitor.py's log; a reader logs to a child of it ("job_monitor.workable"), so its messages
# are printed and saved like the rest, and count when the monitor collects an entry's problems.
log = logging.getLogger("job_monitor")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}
# Sent instead of a browser's name where the monitor reads a site's public job API.
MONITOR_USER_AGENT = "job-monitor/1.0 (personal job alerts; reads public job listings once an hour)"


def hours_since(stamp) -> float:
    """Hours since a local ISO time; infinite if there's none (never) or it's in the future (clock change)."""
    try:
        hours = (datetime.now() - datetime.fromisoformat(str(stamp))).total_seconds() / 3600
    except (TypeError, ValueError):
        return float("inf")  # never emailed
    return hours if hours >= 0 else float("inf")  # a stamp in the future (clock change): don't wait on it


def posted_at(value) -> str | None:
    """When a posting went up, as a UTC time ("2026-11-12T18:57:29+00:00"), from an ISO time
    (Greenhouse, Ashby) or milliseconds since 1970 (Lever); None if unknown."""
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            when = datetime.fromtimestamp(value / 1000, timezone.utc)
        elif isinstance(value, str) and value.strip():
            when = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        else:
            return None
    except (ValueError, OverflowError, OSError):
        return None
    when = when.replace(tzinfo=timezone.utc) if when.tzinfo is None else when.astimezone(timezone.utc)
    return when.isoformat(timespec="seconds")
