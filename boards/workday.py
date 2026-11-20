"""Workday career sites: links like <tenant>.wdN.myworkdayjobs.com/[<locale>/]<site>/job/... and
wdN.myworkdaysite.com/[<locale>/]recruiting/<tenant>/<site>/job/... A site is its host and site
path (site_of); about 37% of the postings the new-grad lists link to are on one. Both forms are
the same site (the lists link Chewy, Guidewire and Magna both ways), so the registry knows a site
by site_key, the same for both, and a posting is known by its tenant and job number (job_keys in
job_monitor.py), whichever form its link takes.

A site's jobs API (POST https://<host>/wday/cxs/<tenant>/<site>/jobs) lists its postings 20 at a
time, newest first, each with a rough date ("Posted Today", "Posted 3 Days Ago", "Posted 30+ Days
Ago"). Only the newest pages are read: until a page has a posting seen before, one older than
NEW_WITHIN_DAYS, the end of the list, or MAX_PAGES pages.

How it's asked (Reader): with a plain request first. Since Nov 16, 2026 Workday puts a bot check
(Cloudflare) in front of its sites that answers plain requests from GitHub's machines with "429"
(and a "Cf-Mitigated: challenge" header), while the search page opened in a browser loads as
usual. So once a plain request is refused (429, 403, or a page that isn't the job list), the rest
of the run reads Workday through one headless Chromium (boards/browser.py):
it opens the site's search page and takes the job list the page itself fetches; further pages
are asked from inside the page, the way the page asks. (Until then, a site whose plain request
fails another way, a server error or no answer, is tried in the browser too; if the browser
can't start, that site is skipped and plain requests go on.) Nothing tries to get past a check: a site
whose page answers with one or with a server error, or whose job list doesn't arrive in full
within PAGE_TIMEOUT_SECONDS (every wait in the browser has a limit, backed by a watchdog:
boards/browser.py), is skipped for the run, and after MAX_SKIPPED_IN_A_ROW skipped sites in a
row Workday isn't read again until the next run (a company site that already couldn't be read
at its last FAILED_TRIES_TO_WAIT tries doesn't count, unless it answers with a bot check: see
below). One request (a page, or a page of jobs) at a time, with a pause between them.

A company name that no longer exists on Workday (after an acquisition, say) gets a server error
(500) that sends the page on to Workday's "Workday is currently unavailable" page: the very page
Workday shows while it is down for maintenance. So such a site can't be told apart from one that
is down for a while, and it isn't counted as gone (a site name that doesn't exist under a real
company name is: 404). It is skipped like any site that couldn't be read, without waiting for the
time limit, and a company site that couldn't be read at its last FAILED_TRIES_TO_WAIT tries waits
at the back of the queue, and isn't tried again until a day after its last try (waiting(), order(),
read_sites). When its turn comes again, skipping it doesn't count toward the MAX_SKIPPED_IN_A_ROW
in a row (read_sites): sites that fail every time were all tried in the same run, so they come
round together, side by side at the front of the queue, and would otherwise stop the company
sites every day. A bot check still counts, whatever the site's history: it says Workday is
refusing the monitor, so if that lasts for weeks (until every site has failed its last tries),
each run still stops after MAX_SKIPPED_IN_A_ROW of them.

How long: GitHub stops a run at 30 minutes, so no Workday site (your entries or the company
sites) is started once the run is RUN_MINUTES old (a Reader is made at the start of the run).
A site started just before that takes a minute or two at most.

Which postings are new (job_monitor.check_target, with "_new_within_days"): a posting seen for the
first time is new only if Workday dates it within NEW_WITHIN_DAYS, so an older one that moves up
the list isn't. A site's first read records what it lists and emails only what went up in the last
catch_up_days days, like other boards. A site remembers (the link only) the RETENTION latest
postings it didn't email you and, besides those, the REPORTED_RETENTION latest it did, so a
busy site's new postings never push out what it emailed: a list linking one later doesn't email
it again.

Your own entries on a Workday site (read_entry; NVIDIA, Salesforce, Circle, Intel in
targets/big_tech.json) are read the same way, with the entry's filters (the part of its link
after "?"), whatever their "mode". Their links keep the form already recorded for each posting
(with or without the filters after "?"), so a posting isn't new again because its link changed.

The company sites in the registry (read_sites) come after every other target and after your
entries: one at a time for at most minutes_per_run per run (and never past RUN_MINUTES into the
run), sites never tried first (those a list linked most recently first), then those tried longest
ago. So each comes round every several hours: about every 8 with the ~1,340 sites the lists link
today (~640 from the new-grad lists, ~700 more from the internship list), about 80 read a run and
a run every 30 minutes. How many were read, skipped and left for the next run is logged and shown
on boards.md.
"""
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import requests

from .browser import Browser
from .common import HEADERS, hours_since

log = logging.getLogger("job_monitor.workday")

PAGE_SIZE = 20                  # the most Workday lists per request
MAX_PAGES = 3                   # pages read per site at most
NEW_WITHIN_DAYS = 7             # a posting seen for the first time but older than this isn't new
RETENTION = 100                 # postings not emailed remembered per company site (at most 60 are read a visit)
REPORTED_RETENTION = 100        # and, besides those, the latest postings it emailed you
SECONDS_BETWEEN_REQUESTS = 2.0  # "workday_seconds_between_requests" on the boards entry
MINUTES_PER_RUN = 6.0           # "workday_minutes_per_run" on the boards entry (company sites only)
RUN_MINUTES = 20                # no Workday site is started once the run is this old (GitHub stops it at 30)
MAX_SKIPPED_IN_A_ROW = 3        # then Workday reading stops until the next run
FAILED_TRIES_TO_WAIT = 2        # a company site that couldn't be read this many tries in a row waits a day
PAGE_TIMEOUT_SECONDS = 15       # for a page to load and get its job list (it usually takes 2-4 s)
API_TIMEOUT_SECONDS = 20

_JOBS_HOST = re.compile(r"^([a-z0-9][a-z0-9-]*)\.wd\d+\.myworkdayjobs\.com$")
_SITE_HOST = re.compile(r"^wd\d+\.myworkdaysite\.com$")
_LOCALE = re.compile(r"[a-z]{2}-[a-z]{2}", re.IGNORECASE)
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_NOT_SITES = {"wday", "refreshfacet", "robots.txt"}  # Workday's own paths, not career sites
_NOT_FILTERS = {"redirect", "q", "start", "offset", "limit", "sort", "sort_by", "sortBy"}  # (search words, paging, order)
_JOBS_CALL = re.compile(r"/wday/cxs/[^/]+/[^/]+/jobs/?$", re.IGNORECASE)
_monotonic, _sleep = time.monotonic, time.sleep  # (tests replace these)


def _parts(link):
    """(lower-case host, the locale in the path or None, the path's other parts), or None."""
    try:
        parsed = urlparse(str(link or "").strip())
    except ValueError:
        return None
    parts = [p for p in parsed.path.split("/") if p]
    locale = parts.pop(0) if parts and _LOCALE.fullmatch(parts[0]) else None
    return parsed.netloc.lower(), locale, parts


def site_of(link) -> str | None:
    """The Workday site a link is on: "https://<tenant>.wdN.myworkdayjobs.com/<site>" or
    "https://wdN.myworkdaysite.com/recruiting/<tenant>/<site>" (no locale; the site's case kept).
    None for any other link."""
    split = _parts(link)
    if not split:
        return None
    host, _, parts = split
    jobs_host = _JOBS_HOST.match(host)
    if jobs_host and parts:
        tenant, site, path = jobs_host.group(1), parts[0], parts[0]
    elif _SITE_HOST.match(host) and len(parts) >= 3 and parts[0].lower() == "recruiting":
        tenant, site = parts[1], parts[2]
        path = f"recruiting/{tenant}/{site}"
    else:
        return None
    if not (_NAME.match(tenant) and _NAME.match(site)) or site.lower() in _NOT_SITES:
        return None
    return f"https://{host}/{path}"


def tenant_of(site: str) -> str:
    """The company's name in Workday (the tenant) for a site from site_of."""
    host, _, parts = _parts(site)
    return parts[1] if _SITE_HOST.match(host) else host.split(".")[0]


def site_key(link) -> str:
    """One key per Workday site, whichever form of its address a link uses (the registry's key):
    "https://<tenant>.wdN.myworkdayjobs.com/<site>" in lower case, also for a link on
    wdN.myworkdaysite.com/recruiting/<tenant>/<site>. "" for a link that isn't on a Workday site."""
    site = site_of(link)
    if not site:
        return ""
    host, _, parts = _parts(site)
    pod = host.split(".")[0 if _SITE_HOST.match(host) else 1]  # (wdN)
    return f"https://{tenant_of(site)}.{pod}.myworkdayjobs.com/{parts[-1]}".lower()


def tenant_in(link) -> str | None:
    """The tenant (lower case) of a Workday link, whichever form it takes; None if it isn't one."""
    site = site_of(link)
    return tenant_of(site).lower() if site else None


def api_of(site: str, locale: str = "en-US") -> tuple[str, str]:
    """(the site's jobs API, the start of its postings' links) for a site from site_of."""
    host, _, parts = _parts(site)
    return f"https://{host}/wday/cxs/{tenant_of(site)}/{parts[-1]}/jobs", f"https://{host}/{locale}/{'/'.join(parts)}"


def posting_key(url) -> str:
    """A posting link without its locale, the part after "?" and case: the same posting whichever
    form its link takes."""
    split = _parts(url)
    if not split:
        return ""
    host, _, parts = split
    return f"{host}/{'/'.join(parts)}".lower()


def posted(text, now: datetime) -> datetime | None:
    """A Workday date ("Posted Today", "Posted Yesterday", "Posted 3 Days Ago", "Posted 30+ Days
    Ago") as a time that many days before now; None if there's none."""
    text = str(text or "").lower()
    if "today" in text:
        days = 0
    elif "yesterday" in text:
        days = 1
    else:
        match = re.search(r"(\d+)\+?\s*days?\s+ago", text)
        if not match:
            return None
        days = int(match.group(1))
    return now - timedelta(days=days)


def _text(value) -> str:
    return str(value).strip() if isinstance(value, (str, int, float)) and not isinstance(value, bool) else ""


def jobs_of(postings, prefix: str, now: datetime) -> list[dict]:
    """Postings ({"title", "url", "location"[, "posted"]}) from a page of the jobs API's "jobPostings"."""
    jobs = []
    for post in postings if isinstance(postings, list) else []:
        if not isinstance(post, dict) or not _text(post.get("title")) or not _text(post.get("externalPath")):
            continue
        job = {"title": _text(post["title"])[:200], "url": prefix + "/" + _text(post["externalPath"]).lstrip("/"),
               "location": _text(post.get("locationsText"))}
        when = posted(post.get("postedOn"), now)
        if when:
            job["posted"] = when.isoformat(timespec="seconds")
        jobs.append(job)
    return jobs


def newest(first, more, prefix: str, seen=frozenset(), now: datetime | None = None, size: int | None = None) -> list[dict] | None:
    """The postings of a site's newest pages: first (the first page's answer), then more(offset)
    (None if it can't be read: what was read is kept), until a page has a posting seen before
    (seen: posting_key values), one older than NEW_WITHIN_DAYS, the end of the list or MAX_PAGES
    pages. None if the first page isn't a job list."""
    now = now or datetime.now(timezone.utc)
    size = size or PAGE_SIZE
    too_old = now - timedelta(days=NEW_WITHIN_DAYS)
    jobs, keys, data = [], set(), first
    total = first.get("total") if isinstance(first, dict) else None
    for page in range(MAX_PAGES):
        if page:
            data = more(page * size)
        postings = data.get("jobPostings") if isinstance(data, dict) else None
        if not isinstance(postings, list):
            if page == 0:
                return None
            break
        batch = jobs_of(postings, prefix, now)
        for job in batch:  # (one posting once, even if listed twice)
            if posting_key(job["url"]) not in keys:
                keys.add(posting_key(job["url"]))
                jobs.append(job)
        if len(postings) < size or (isinstance(total, int) and total > 0 and (page + 1) * size >= total) \
                or any(posting_key(j["url"]) in seen for j in batch) \
                or any(j.get("posted") and datetime.fromisoformat(j["posted"]) < too_old for j in batch):
            break
    return jobs


def search_of(link) -> tuple[str, dict, str, str]:
    """For your entry's link to a Workday search: (its filters as Workday's "appliedFacets",
    its search words, the start of its postings' links, the search page to open)."""
    host, locale, _ = _parts(link)
    site = site_of(link)
    query = parse_qs(urlparse(link).query)
    facets = {key: [v for v in values if v] for key, values in query.items() if key not in _NOT_FILTERS and any(values)}
    words = (query.get("q") or [""])[0]
    _, prefix = api_of(site, locale or "en-US")
    shown = "&".join(f"{k}={v}" for k, v in _query_pairs(link) if k not in _NOT_FILTERS or k == "q")
    return facets, words, prefix, prefix + (f"?{shown}" if shown else "")


def _query_pairs(link):
    query = urlparse(link).query
    return [tuple(p.split("=", 1)) if "=" in p else (p, "") for p in query.split("&") if p]


def _same_filters(a, b) -> bool:
    norm = lambda f: {k: sorted(map(str, v)) for k, v in (f or {}).items() if v} if isinstance(f, dict) else {}  # noqa: E731
    return norm(a) == norm(b)


class Reader:
    """Reads Workday sites for one run: plain requests until one is refused, then the browser
    (see the module's docstring). Make it at the start of the run: no site is started once
    run_minutes have passed since. Close it at the end of the run (close())."""

    def __init__(self, pause: float = SECONDS_BETWEEN_REQUESTS, browser=None, run_minutes: float = RUN_MINUTES):
        self.pause = pause
        self.browser = browser if browser is not None else Browser()
        self._last = None
        self.started, self.run_minutes = self.clock(), run_minutes
        self.api_refused = False   # a plain request was refused this run: the browser from then on
        self.in_a_row = 0          # sites skipped in a row
        self.stopped = None        # why Workday reading stopped for this run, once it has
        self._said_broken = False  # the browser couldn't start while plain requests work: said once

    @staticmethod
    def clock() -> float:
        return _monotonic()

    def _wait(self):
        """Pause until `pause` seconds after the last request to Workday."""
        if self._last is not None:
            _sleep(max(0.0, self._last + self.pause - self.clock()))
        self._last = self.clock()

    def read(self, site: str, seen=frozenset(), facets=None, words: str = "", prefix: str | None = None,
             page: str | None = None, name: str = "", quiet: bool = True, counts: bool = True) -> tuple[str, list | None]:
        """A site's newest postings (see newest()), with these filters: ("ok", postings),
        ("missing", None) (404), ("skipped", None) (a bot check, no answer in time, an error:
        tried again next run) or ("stopped", None) (not tried: Workday reading has stopped for
        this run, or the run is run_minutes old). quiet: problems are logged as debug, not as warnings.
        counts: False for a site already known to fail (failing()), whose skip doesn't count toward
        the MAX_SKIPPED_IN_A_ROW in a row unless it is a bot check."""
        if not self.stopped and self.clock() - self.started >= self.run_minutes * 60:
            self.stopped = f"the run is {self.run_minutes:g} minutes old, and GitHub stops a run at 30"
            log.info(f"  [workday] No more Workday sites this run: {self.stopped}.")
        if self.stopped:
            return "stopped", None
        say = log.debug if quiet else log.warning
        api, own_prefix = api_of(site)
        prefix = prefix or own_prefix
        body = {"appliedFacets": facets or {}, "limit": PAGE_SIZE, "offset": 0, "searchText": words or ""}
        now = datetime.now(timezone.utc)
        status, jobs, why = (None, None, "") if self.api_refused else self._by_api(api, body, prefix, seen, now)
        if status is None:
            status, jobs, why = self._by_browser(page or own_prefix, body, prefix, seen, now)
        if status == "refused":  # (a bot check: it counts toward the stop whatever the site's history)
            status, counts = "skipped", True
        if status == "skipped":
            say(f"  [workday] {name or site}: skipped this run ({why})."
                + ("" if counts else " (It couldn't be read at its last tries either, and this isn't a bot check: "
                                     "it doesn't count toward the stop.)"))
            if counts:
                self.in_a_row += 1
                if self.in_a_row >= MAX_SKIPPED_IN_A_ROW and not self.stopped:
                    self.stopped = f"{self.in_a_row} Workday sites in a row couldn't be read (last: {why})"
        else:
            self.in_a_row = 0
        if status == "missing":
            say(f"  [workday] {name or site}: the site is gone (404).")
        if self.stopped and status == "skipped":
            log.warning(f"  [workday] Workday reading stops for this run: {self.stopped}.")
        return status, jobs

    def _by_api(self, api, body, prefix, seen, now):
        """(status, postings, why) from plain requests; status None: use the browser."""
        def post(offset):
            self._wait()
            try:
                resp = requests.post(api, json={**body, "offset": offset}, timeout=API_TIMEOUT_SECONDS,
                                     headers={**HEADERS, "Accept": "application/json"})
            except Exception as exc:
                return "error", exc.__class__.__name__
            if resp.status_code == 404:
                return "missing", "404"
            refused = resp.status_code in (403, 429) or bool((resp.headers or {}).get("Cf-Mitigated"))
            if resp.status_code != 200 or refused:
                return ("refused" if refused else "error"), str(resp.status_code)
            try:
                return "ok", resp.json()
            except ValueError:
                return "refused", "a page that isn't the job list"

        status, first = post(0)
        if status == "missing":
            return "missing", None, "404"
        if status == "refused":
            self.api_refused = True
            log.info(f"  [workday] Workday refused a plain request ({first}); reading Workday through the browser "
                     f"for the rest of this run.")
            return None, None, ""
        if status != "ok":
            return None, None, ""  # (an error with this site: the browser tries it)

        def more(offset):
            status, data = post(offset)
            if status == "refused":
                self.api_refused = True
            return data if status == "ok" else None

        jobs = newest(first, more, prefix, seen, now)
        return (None, None, "") if jobs is None else ("ok", jobs, "")

    def _by_browser(self, page_url, body, prefix, seen, now):
        """(status, postings, why) from the site's search page in the browser; status "refused":
        skipped because of a bot check."""
        self._wait()
        started = self.clock()
        loaded = self.browser.open(page_url, lambda url, method: method == "POST" and bool(_JOBS_CALL.search(urlparse(url).path)),
                                   PAGE_TIMEOUT_SECONDS)
        try:
            if loaded.status == "missing":
                return "missing", None, "404"
            if loaded.status != "ok":
                broken = getattr(self.browser, "broken", None)
                if broken and self.api_refused:
                    self.stopped = broken  # (Workday can only be read through the browser now)
                elif broken and not self._said_broken:
                    self._said_broken = True
                    log.warning(f"  [workday] {broken}: a site whose plain request fails is skipped this run; "
                                f"the others are still read with plain requests.")
                why = {"challenge": f"Workday answered with a bot check or refusal ({loaded.detail})",
                       "unavailable": f"the site answered with a server error ({loaded.detail}): Workday may be down for "
                                      f"a while, or the company is no longer on Workday",
                       "timeout": f"its job list didn't arrive: timed out after {PAGE_TIMEOUT_SECONDS:g} s"
                                  + (f"; the page had gone to {loaded.went}" if getattr(loaded, "went", "") else "")
                       }.get(loaded.status, loaded.detail)
                return ("refused" if loaded.status == "challenge" else "skipped"), None, why
            sent = loaded.request_body if isinstance(loaded.request_body, dict) else {}
            ask = {**body, **{k: v for k, v in sent.items() if k not in ("offset",)}}
            size = ask.get("limit") if isinstance(ask.get("limit"), int) and 0 < ask["limit"] <= 100 else PAGE_SIZE
            first = loaded.data

            def more(offset):
                self._wait()
                status, data = self.browser.post_json(loaded, loaded.request_url, {**ask, "offset": offset, "limit": size},
                                                      API_TIMEOUT_SECONDS)
                return data if status == 200 else None

            if not _same_filters(sent.get("appliedFacets"), body["appliedFacets"]) or (sent.get("searchText") or "") != body["searchText"]:
                # The page didn't apply your entry's filters: its first page is asked with them.
                ask.update(appliedFacets=body["appliedFacets"], searchText=body["searchText"])
                first = more(0)
            jobs = newest(first, more, prefix, seen, now, size)
            if jobs is None:
                return "skipped", None, "the page's job list wasn't one"
            log.debug(f"  [workday] read through the browser in {self.clock() - started:.1f} s")
            return "ok", jobs, ""
        finally:
            self.browser.done(loaded)

    def close(self):
        self.browser.close()


def read_entry(reader: Reader, link: str, recorded, name: str = "") -> list[dict] | None:
    """Your entry's postings (a link to a Workday search, with its filters), or None if it can't
    be read this run. A posting already recorded (recorded: the entry's saved postings) keeps the
    link it was recorded with."""
    facets, words, prefix, page = search_of(link)
    links = {}
    for job in recorded if isinstance(recorded, list) else []:
        if isinstance(job, dict) and isinstance(job.get("url"), str):
            links.setdefault(posting_key(job["url"]), job["url"])
    status, jobs = reader.read(site_of(link), set(links), facets, words, prefix, page, name, quiet=False)
    if status == "stopped":
        log.warning(f"  [workday] Not read this run: {reader.stopped}.")
    if status != "ok":
        return None
    log.info(f"  [workday] {name or link}: {len(jobs)} postings on its newest page(s)")
    return [{**job, "url": links.get(posting_key(job["url"]), job["url"])} for job in jobs]


def newest_links(rows) -> dict[str, str]:
    """{site_key: the newest date} from (link, date first listed) pairs."""
    out: dict[str, str] = {}
    for link, date in rows:
        key = site_key(link) if link and "myworkday" in str(link) else ""
        if key and date and str(date) > out.get(key, ""):
            out[key] = str(date)
    return out


def failing(info) -> bool:
    """A company site (its registry info) that couldn't be read at its last FAILED_TRIES_TO_WAIT
    tries or more (see note())."""
    tries = info.get("failed_tries") if isinstance(info, dict) else None
    return isinstance(tries, int) and tries >= FAILED_TRIES_TO_WAIT


def waiting(info) -> bool:
    """A company site (its registry info) that is failing() and was last tried less than a day
    ago: it isn't tried again before then (read_sites)."""
    return failing(info) and hours_since(info.get("tried_at")) < 24


def order(sites: list[dict], known: dict, linked: dict) -> list[dict]:
    """The registry's sites (board targets) in reading order: never tried first, those a list
    linked most recently first (linked: {board key: date}), then those added most recently;
    then the others, tried longest ago first. A site that couldn't be read at its last
    FAILED_TRIES_TO_WAIT tries comes last until a day after its last try (waiting())."""
    info = lambda s: known.get(s.get("_board_key")) or {}  # noqa: E731
    never = [s for s in sites if not info(s).get("tried_at")]
    never.sort(key=lambda s: (str(linked.get(s.get("_board_key")) or ""), str(info(s).get("added") or "")), reverse=True)
    tried = sorted((s for s in sites if info(s).get("tried_at")), key=lambda s: (waiting(info(s)), str(info(s)["tried_at"])))
    return never + tried


def note(known: dict, sites: list[dict], results: dict, now: str):
    """Note in the registry (known: {board key: info}) each site read_sites tried: "tried_at" (for
    the order), "read_at" when it was read, and "failed_tries": how many tries in a row couldn't
    read it (none once it is read, or gone: 404)."""
    for site in sites:
        info = known.get(site.get("_board_key"))
        if site["url"] not in results or not isinstance(info, dict):
            continue
        status = results[site["url"]][0]
        info["tried_at"] = now
        if status == "ok":
            info["read_at"] = now
        if status == "failed":
            tries = info.get("failed_tries")
            info["failed_tries"] = (tries if isinstance(tries, int) and tries > 0 else 0) + 1
        else:
            info.pop("failed_tries", None)


def read_sites(reader: Reader, sites: list[dict], seen: dict, minutes: float, known: dict | None = None) -> tuple[dict, dict]:
    """Read the registry's sites (board targets, in this order) one at a time until minutes have
    passed, the run is the reader's run_minutes old, or Workday reading stops; seen: {url:
    posting_key values already recorded}; known: the registry ({board key: info}). A site still
    waiting its day there (waiting()) isn't tried; skipping one already known to fail (failing())
    whose day is up doesn't count toward the MAX_SKIPPED_IN_A_ROW in a row unless it is a bot
    check. Returns ({url: (status, postings, None)} for the sites tried: "ok", "missing" (404) or
    "failed" (skipped: read next time), and a summary: {"sites", "read", "skipped", "missing",
    "left", "why" (why some were left), "minutes"})."""
    known = known or {}
    started = reader.clock()
    results: dict[str, tuple] = {}
    why, waits = "", 0
    for site in sites:
        if waiting(known.get(site.get("_board_key"))):
            waits += 1  # (not tried until a day after its last try)
            continue
        if reader.stopped:
            why = reader.stopped
        elif reader.clock() - started >= minutes * 60:
            why = f"time's up: {minutes:g} min a run"
        if why:
            break
        status, jobs = reader.read(site["url"], seen.get(site["url"], frozenset()), name=site.get("name") or "",
                                   counts=not failing(known.get(site.get("_board_key"))))
        if status == "stopped":  # (not tried: the run is too old to start another site)
            why = reader.stopped
            break
        results[site["url"]] = ("ok", jobs, None) if status == "ok" else ("missing", None, None) if status == "missing" \
            else ("failed", None, None)
    if waits and not why:
        why = f"{waits} couldn't be read at {'its' if waits == 1 else 'their'} last tries and wait{'s' if waits == 1 else ''} a day"
    counts = [sum(1 for r in results.values() if r[0] == s) for s in ("ok", "failed", "missing")]
    summary = {"sites": len(sites), "read": counts[0], "skipped": counts[1], "missing": counts[2],
               "left": len(sites) - len(results), "why": why if len(results) < len(sites) else "",
               "minutes": round((reader.clock() - started) / 60, 1)}
    log.info(f"  [workday] {summary['read']} of {len(sites)} company Workday sites read in {summary['minutes']:g} min"
             + (f", {summary['skipped']} couldn't be read (tried again later)" if summary["skipped"] else "")
             + (f", {summary['missing']} gone (404)" if summary["missing"] else "")
             + (f"; {summary['left']} wait for a later run ({summary['why']})" if summary["left"] else "") + ".")
    return results, summary
