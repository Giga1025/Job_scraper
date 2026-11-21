"""Whether whole sources still work, and the daily "still running" email.

A company board that can't be read now and then is normal (thousands of them, read every hour),
so single boards aren't reported (your own entries are: job_monitor.broken_checks). What this
module watches is a whole source going quiet, which would otherwise look just like "no new jobs":

- Workday: no site read in WORKDAY_BAD_RUNS runs in a row that tried (Workday blocking the
  browser, say, or the runs running out of time before Workday's turn; not while you've paused it);
- a job-board platform (Greenhouse, Ashby, Lever...): at least BOARD_BAD_SHARE of its boards
  (BOARD_MIN or more) couldn't be read, or answered "not found" (an API that moved), in
  BOARD_BAD_CHECKS checks in a row;
- the company boards not checked at all for BOARDS_LATE_HOURS (or 3 times their every_hours);
- a new-grad list (a feed) with no new posting for FEED_QUIET_HOURS, or a list read only to find
  companies (find_companies_in) that couldn't be read for LIST_QUIET_HOURS.

Those warnings (alarms()) go at the top of every email while they last (not for a source you've
paused). A run can't report that runs stopped altogether, so once a day (summary_due: once per UTC
day, from a set hour) a short email says the monitor is still running: the runs, what was
checked and what was sent in the last day, and the same warnings. If it doesn't come, something
broke.

What it needs is kept per config in state.json (job_monitor.SOURCES_KEY): the last
RUNS_KEPT_HOURS of runs (a few numbers each), the emails sent, when each feed last added a
posting and each list was last read, and when the daily email was last sent."""
import html as html_lib
from collections import Counter
from datetime import datetime, timedelta

RUNS_KEPT_HOURS = 48
WORKDAY_BAD_RUNS = 3
BOARD_BAD_CHECKS = 3
BOARD_BAD_SHARE = 0.5
BOARD_MIN = 10
BOARDS_LATE_HOURS = 6
FEED_QUIET_HOURS = 72
LIST_QUIET_HOURS = 72
RUNS_EVERY_MINUTES = 30
SUMMARY_LATE_HOURS = 30     # the daily email goes at once if the last one is this old (runs that stopped, then resumed)...
DEFAULT_SUMMARY_HOUR = 14   # ...else once a UTC day, from this hour (8 AM US Central, 6 AM Pacific)

PLATFORM_NAMES = {"greenhouse": "Greenhouse", "ashby": "Ashby", "lever": "Lever", "workable": "Workable",
                  "smartrecruiters": "SmartRecruiters", "oracle": "Oracle", "workday": "Workday",
                  "jibe": "iCIMS (Jibe)", "rippling": "Rippling", "gem": "Gem"}


def _when(stamp) -> datetime | None:
    """A saved local time (naive, as the monitor writes them); None for anything else."""
    try:
        when = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return None
    return when.replace(tzinfo=None) if when.tzinfo else when


def _count(value) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _day(stamp) -> str:
    when = _when(stamp)
    return when.strftime("%b %d, %H:%M") if when else "a while"


def record_of(saved) -> dict:
    """A config's record from state.json, repaired where it isn't what this version writes."""
    saved = saved if isinstance(saved, dict) else {}

    def part(key, kind):
        return saved.get(key) if isinstance(saved.get(key), kind) else kind()

    def run_of(r: dict) -> dict:
        run = {"at": _when(r["at"]).isoformat(timespec="seconds"),
               **{k: _count(r.get(k)) for k in ("matches", "left_out", "entries", "failing")}}
        if isinstance(r.get("boards"), dict):
            run["boards"] = {str(p): {k: _count(c.get(k)) for k in ("ok", "unchanged", "missing", "failed")}
                             for p, c in r["boards"].items() if isinstance(c, dict)}
        if isinstance(r.get("workday"), dict):
            run["workday"] = {**{k: _count(r["workday"].get(k)) for k in ("sites", "read", "skipped", "missing")},
                              "why": str(r["workday"].get("why") or "")[:300]}
        return run

    return {"runs": [run_of(r) for r in part("runs", list) if isinstance(r, dict) and _when(r.get("at"))],
            "emails": [{"at": _when(e["at"]).isoformat(timespec="seconds"), **{k: _count(e.get(k)) for k in ("roles", "left_out", "parts")}}
                       for e in part("emails", list) if isinstance(e, dict) and _when(e.get("at"))],
            "feeds": {str(u): {"name": str(f.get("name") or u), **({"last_new": _when(f["last_new"]).isoformat(timespec="seconds")}
                                                                  if _when(f.get("last_new")) else {})}
                      for u, f in part("feeds", dict).items() if isinstance(f, dict)},
            "lists": {str(u): {"name": str(f.get("name") or u), **({"read": _when(f["read"]).isoformat(timespec="seconds")}
                                                                  if _when(f.get("read")) else {})}
                      for u, f in part("lists", dict).items() if isinstance(f, dict)},
            "summary_at": _when(saved["summary_at"]).isoformat(timespec="seconds") if _when(saved.get("summary_at")) else None,
            "summary_day": str(saved.get("summary_day")) if saved.get("summary_day") else None}


def _prune(items: list[dict], now: datetime) -> list[dict]:
    cutoff = now - timedelta(hours=RUNS_KEPT_HOURS)
    return [i for i in items if (_when(i.get("at")) or now) >= cutoff]


def note_run(record: dict, now: datetime, boards: dict | None = None, workday: dict | None = None,
             matches: int = 0, left_out: int = 0, entries: int = 0, failing: int = 0):
    """Note this run: boards ({platform: {"ok", "unchanged", "missing", "failed"}} when the company
    boards were checked this run), workday (its run summary when Workday sites were read), how many
    matches and left-out postings it found, and how many of your entries it checked and how many
    aren't working."""
    run = {"at": now.isoformat(timespec="seconds"), "matches": int(matches), "left_out": int(left_out),
           "entries": int(entries), "failing": int(failing)}
    if boards is not None:
        run["boards"] = {p: {k: int(c.get(k, 0)) for k in ("ok", "unchanged", "missing", "failed")} for p, c in boards.items()}
    if workday:
        run["workday"] = {**{k: _count(workday.get(k)) for k in ("sites", "read", "skipped", "missing")},
                          "why": str(workday.get("why") or "")[:300]}  # (why its reading stopped, if it did)
    record["runs"] = _prune(record["runs"] + [run], now)


def note_feed(record: dict, url: str, name: str, added: bool, now: datetime, newest_seen=None):
    """A new-grad list (feed) was read this run; added: it had postings not seen before;
    newest_seen: the newest first_seen of its saved postings (another config sharing state.json
    may have recorded them first)."""
    feed = record["feeds"].setdefault(url, {})
    feed["name"] = name
    if added or not _when(feed.get("last_new")):
        feed["last_new"] = now.isoformat(timespec="seconds")
    elif _when(newest_seen) and _when(newest_seen) > _when(feed["last_new"]):
        feed["last_new"] = _when(newest_seen).isoformat(timespec="seconds")


def note_list(record: dict, url: str, name: str, read_at):
    """A list read only to find companies (find_companies_in), last read (answered) at read_at."""
    entry = record["lists"].setdefault(url, {})
    entry["name"] = name
    if _when(read_at):
        entry["read"] = str(read_at)


def note_email(record: dict, now: datetime, roles: int, left_out: int, parts: int):
    record["emails"] = _prune(record["emails"] + [{"at": now.isoformat(timespec="seconds"), "roles": int(roles),
                                                   "left_out": int(left_out), "parts": int(parts)}], now)


def forget_gone(record: dict, feed_urls, list_urls):
    """Drop the feeds and lists no longer in the config."""
    record["feeds"] = {u: f for u, f in record["feeds"].items() if u in feed_urls}
    record["lists"] = {u: f for u, f in record["lists"].items() if u in list_urls}


def alarms(record: dict, now: datetime, boards_checked_at=None, boards_on: bool = False, workday_on: bool = True,
           every_hours: float = 1) -> list[dict]:
    """The sources that look like they stopped: [{"name", "why", "since"}]. boards_on, workday_on:
    whether the company boards and their Workday sites are on now (a paused one isn't warned
    about); every_hours: how often the boards are checked."""
    out = []
    tried = [r for r in record["runs"] if isinstance(r.get("workday"), dict) and r["workday"].get("sites")]
    last = tried[-WORKDAY_BAD_RUNS:]
    if boards_on and workday_on and len(last) == WORKDAY_BAD_RUNS and all(not r["workday"].get("read") for r in last):
        stop = str(last[-1]["workday"].get("why") or "")
        since = f"in the last {WORKDAY_BAD_RUNS} runs, since {_day(last[0]['at'])}"
        if any(r["workday"].get("skipped") for r in last):
            why = f"no company Workday site could be read {since} (Workday may be refusing the browser)"
        elif all(r["workday"].get("missing") for r in last):
            why = f"every company Workday site tried answered \"not found\" {since} (Workday may have changed its addresses)"
        elif not stop or stop.startswith(("the run is", "time's up")):
            why = f"no company Workday site was read {since}: the runs ran out of time before Workday's turn"
        else:
            why = f"no company Workday site was read {since}: {stop}"
        out.append({"name": "Workday", "since": last[0]["at"], "why": why})
    checks = [r for r in record["runs"] if isinstance(r.get("boards"), dict)]
    for platform in sorted({p for r in checks for p in r["boards"]}) if boards_on else []:
        counts = [r["boards"].get(platform) for r in checks if isinstance(r["boards"].get(platform), dict)]
        last = counts[-BOARD_BAD_CHECKS:]
        if len(last) < BOARD_BAD_CHECKS:
            continue
        totals = [sum(c.get(k, 0) for k in ("ok", "unchanged", "missing", "failed")) for c in last]
        bad = [c.get("failed", 0) + c.get("missing", 0) for c in last]
        if all(t >= BOARD_MIN and b >= BOARD_BAD_SHARE * t for b, t in zip(bad, totals)):
            name = PLATFORM_NAMES.get(platform, platform)
            out.append({"name": name, "since": None,
                        "why": f"{bad[-1]} of its {totals[-1]} company boards couldn't be read or answered \"not found\", "
                               f"and as many in each of the last {BOARD_BAD_CHECKS} checks"})
    checked = _when(boards_checked_at)
    try:
        every = max(float(every_hours), 0)
    except (TypeError, ValueError):
        every = 1.0
    late = max(BOARDS_LATE_HOURS, 3 * every)
    if boards_on and checked is not None and now - checked >= timedelta(hours=late):
        out.append({"name": "Company job boards", "since": boards_checked_at,
                    "why": f"not checked since {_day(boards_checked_at)} (they're checked every {every:g} hour{'s' if every != 1 else ''})"})
    for url, feed in sorted(record["feeds"].items(), key=lambda kv: str(kv[1].get("name"))):
        since = _when(feed.get("last_new"))
        if since and now - since >= timedelta(hours=FEED_QUIET_HOURS):
            out.append({"name": feed.get("name") or url, "since": feed["last_new"],
                        "why": f"no new posting since {_day(feed['last_new'])} (the list may have stopped updating, "
                               f"or moved)"})
    for url, entry in sorted(record["lists"].items(), key=lambda kv: str(kv[1].get("name"))):
        read = _when(entry.get("read"))
        if read and now - read >= timedelta(hours=LIST_QUIET_HOURS):
            out.append({"name": entry.get("name") or url, "since": entry["read"],
                        "why": f"couldn't be read since {_day(entry['read'])} (it's used to find companies)"})
    return out


def summary_due(record: dict, now: datetime, hour, now_utc: datetime | None = None) -> bool:
    """Whether the daily email is due: once per UTC day (record["summary_day"]), from `hour` (UTC,
    0-23) on; or at once if the last one is SUMMARY_LATE_HOURS old (the runs stopped meanwhile).
    now: local time (as the record's times); now_utc: the UTC time (default: now)."""
    now_utc = now_utc or now
    last = _when(record.get("summary_at"))
    try:
        hour = int(hour) % 24
    except (TypeError, ValueError):
        hour = DEFAULT_SUMMARY_HOUR
    if last is not None and now - last >= timedelta(hours=SUMMARY_LATE_HOURS):
        return True
    if record.get("summary_day") == now_utc.date().isoformat():
        return False
    return now_utc.hour >= hour


def summary(record: dict, now: datetime, facts: dict, found_alarms: list[dict], problems: list[str],
            now_utc: datetime | None = None) -> tuple[str, str, str]:
    """The daily email: (subject, plain text, HTML). facts: {"boards": {platform: count checked},
    "workday_sites", "workday_read_day", "entries", "pending", "boards_checked_at"}; problems: lines about
    your entries that aren't working."""
    now_utc = now_utc or now
    day = now - timedelta(hours=24)
    runs = [r for r in record["runs"] if (_when(r["at"]) or now) >= day]
    times = sorted(_when(r["at"]) for r in runs)
    gap = max(((b - a) for a, b in zip(times, times[1:])), default=timedelta(0))
    emails = [e for e in record["emails"] if (_when(e["at"]) or now) >= day]
    sent = sum(e.get("roles", 0) for e in emails)
    found = sum(r.get("matches", 0) for r in runs)
    boards = facts.get("boards") or {}
    first = min((_when(r["at"]) for r in record["runs"]), default=None)
    if first is not None and first > day + timedelta(minutes=RUNS_EVERY_MINUTES):
        # (the record is younger than a day: since the monitor started noting runs)
        span = f"since the first run noted ({_day(first.isoformat())})"
        expected = int((now - first) / timedelta(minutes=RUNS_EVERY_MINUTES)) + 1
    else:
        span, expected = "in the last 24 hours", 24 * 60 // RUNS_EVERY_MINUTES
    lines = [f"Runs {span}: {len(runs)} (about {expected} expected, one every {RUNS_EVERY_MINUTES} minutes)"
             + (f"; the longest gap between two was {gap.seconds // 3600 + gap.days * 24} h {gap.seconds % 3600 // 60} min"
                if gap >= timedelta(minutes=90) else "") + "."]
    if boards:
        listing = ", ".join(f"{PLATFORM_NAMES.get(p, p)} {n:,}" for p, n in sorted(boards.items(), key=lambda kv: -kv[1]))
        lines.append(f"Company job boards checked every hour: {sum(boards.values()):,} ({listing}); "
                     f"last check {_day(facts.get('boards_checked_at'))}.")
    if facts.get("workday_sites"):
        lines.append(f"Company Workday sites: {facts.get('workday_read_day', 0):,} of {facts['workday_sites']:,} read "
                     f"in the last 24 hours.")
    lines.append(f"Your own entries: {facts.get('entries', 0)} checked each run"
                 + (f", {len(problems)} not working (below)." if problems else ", all working."))
    lines.append(f"New matching roles found {span}: {found}; emailed: {sent} in {len(emails)} email(s)"
                 + (f"; waiting for the next email: {facts['pending']}" if facts.get("pending") else "") + ".")
    if found_alarms:
        status = f"{len(found_alarms)} source(s) may have stopped working"
    elif problems:
        status = f"running; {len(problems)} of your entries aren't working"
    else:
        status = "running fine"
    subject = f"[Job Monitor] Daily check: {status} ({sent} role{'s' if sent != 1 else ''} emailed in the last day)"
    warn = [f"{a['name']}: {a['why']}" for a in found_alarms]
    plain = "\n".join(["=" * 60, f"  JOB MONITOR DAILY CHECK — {now_utc.strftime('%Y-%m-%d %H:%M')} UTC", "=" * 60, ""]
                      + [f"- {line}" for line in lines] + [""]
                      + ([f"⚠ Sources that may have stopped working:"] + [f"    ! {w}" for w in warn] + [""] if warn
                         else ["All sources look normal.", ""])
                      + ([f"⚠ Your entries that aren't working:"] + [f"    ! {p}" for p in problems] + [""] if problems else [])
                      + ["This email comes once a day while the monitor runs. If a day passes without one, the runs "
                         "have stopped: see \"When a whole source stops\" in the README.", "", "Sent by job_monitor.py"])
    esc = html_lib.escape
    box = lambda title, items: (f'<div style="background:#fff4e5;border-left:4px solid #f29900;padding:8px 12px;margin:16px 0">'
                                f'<b>⚠ {esc(title)}</b><ul style="margin:6px 0">'
                                + "".join(f'<li style="margin-bottom:4px">{esc(i)}</li>' for i in items) + "</ul></div>")
    body = "<ul>" + "".join(f'<li style="margin-bottom:6px">{esc(line)}</li>' for line in lines) + "</ul>"
    body += box("Sources that may have stopped working", warn) if warn else '<p style="color:#188038"><b>All sources look normal.</b></p>'
    body += box("Your entries that aren't working", problems) if problems else ""
    html = (f'<div style="font-family:system-ui,sans-serif;max-width:600px;margin:auto;padding:20px">'
            f'<h2 style="border-bottom:2px solid #1a73e8;padding-bottom:8px">Daily check: {esc(status)}</h2>'
            f'<p style="color:#555">{esc(now_utc.strftime("%B %d, %Y at %I:%M %p"))} UTC</p>{body}'
            f'<p style="color:#999;font-size:12px;margin-top:32px">This email comes once a day while the monitor runs. '
            f'If a day passes without one, the runs have stopped: see "When a whole source stops" in the README.</p>'
            f'<p style="color:#999;font-size:12px">Sent by job_monitor.py</p></div>')
    return subject, plain, html


def board_counts(fetched: dict, platform_of) -> dict:
    """{platform: {"ok", "unchanged", "missing", "failed"}} from a board check's results."""
    out: dict[str, Counter] = {}
    for url, (status, _, _) in fetched.items():
        out.setdefault(platform_of(url), Counter())[status if status in ("ok", "unchanged", "missing") else "failed"] += 1
    return {p: dict(c) for p, c in out.items()}
