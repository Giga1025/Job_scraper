"""iCIMS career sites built on Jibe: <name>.jibeapply.com, or a company's own domain (careers.amd.com,
jobs.keysight.com, careers.rivian.com, careers.garmin.com, careers.jhuapl.edu, careers.viasat.com,
careers.docusign.com...; about 130 sites in the lists, linked like careers.amd.com/jobs/85806?icims=1).

Every Jibe site serves its postings through one public JSON API at its root (api_url): LIMIT a
page, newest first (sortBy=posted_date), with the title, the iCIMS job id, every place's country
and the posting date (read_site). Only the newest are read (a big company lists thousands): the
first page, and the next while the last posting on a page went up in the last DEEP_DAYS days (a
site that posts a hundred a day, like Schneider Electric's), or since a day before the site's last
good read of the company's sites (after an outage), up to MAX_PAGES. A site whose API ignores the
sort and lists oldest first (Bowman) has its last pages read, newest first; one whose API refuses
the sort (AVI-SPL answers 503) is read in its own order, up to MAX_PAGES pages. A site's saved
ETag is only used once a read has seen it list newest first (etag_ok).

So a posting missing from a read may still be open; it isn't forgotten for that
(job_monitor.FORGET_GONE_PLATFORMS). And a posting seen for the first time is new only if it went
up in the last NEW_WITHIN_DAYS days, or since the day before the site's last good read
(job_monitor.check_target, "_new_within_days"), so an older one that moves up into the newest (when
newer ones close) isn't emailed as new. When a posting went up is the later of its posting date
and the day it reached the site (a site moving to Jibe loads its open postings with their old
dates). A site remembers the RETENTION latest postings it didn't email you and, besides those, the
REPORTED_RETENTION latest it did.

A site is one iCIMS customer (its "client", read from the API): one company can have two sites
(careers.ice.com and ice.jibeapply.com), and a site's postings are also on the customer's classic
iCIMS portals (careers-amd.icims.com, global-external-amd.icims.com...), with the same job id. Some
sites name another domain as their postings' address (Schneider Electric's se.jibeapply.com names
careers.se.com; H-E-B's careers.heb.com names careers.hrblock.com, H&R Block's own site, where they
aren't found): the link emailed is always the site's own, which opens the posting, and another
domain is only noted (not one that is another Jibe site in the registry). So a posting is known by
(client, job id) on any of those addresses: learn() notes, from each read, which addresses belong to
which client, and job_key() uses that (an address not learned yet: by its host). job_monitor reads
one site per client (same_client: one that can be read)."""
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

from .common import posted_at

LIMIT = 100  # postings a page (the API's most), newest first
MAX_PAGES = 5
DEEP_DAYS = 3
NEW_WITHIN_DAYS = 7          # a posting seen for the first time but older than this isn't new
RETENTION = 2 * LIMIT        # postings not emailed remembered per site
REPORTED_RETENTION = 100     # and, besides those, the latest postings it emailed you

_JOB_PATH = re.compile(r"^/(?:[^/]+/)*jobs/(\d+)/?$")
_BOARD_PATH = re.compile(r"^/(?:[^/]+/)*jobs/?$")  # (the site's search page: careers.amd.com/careers-home/jobs)
_API_PATH = "/api/jobs"
_ICIMS_HOST = re.compile(r"^[a-z0-9-]+\.icims\.com$")
# Places a posting may name as its country that are in the US for the US filter (as on Workday).
_US_PLACES = {"united states", "united states of america", "usa", "us", "puerto rico", "guam", "american samoa",
              "united states virgin islands", "virgin islands, u.s.", "u.s. virgin islands", "northern mariana islands"}

# host -> client (or the site's host, if its client isn't known), for every address learned: Jibe
# sites, the classic iCIMS portals behind them, and the other domains they name for their postings
_CLIENTS: dict[str, str] = {}
_SITES: set[str] = set()  # the hosts of the Jibe sites in the registry (their links needn't say icims=1)
_ALIASES: set[str] = set()  # other domains a site names for its postings (keys only: not sites to read)
_READ_AT: dict[str, datetime] = {}  # host -> the last good read of any of its client's sites
_ORDER: dict[str, str] = {}  # host -> the order this run's read found ("newest first", "oldest first"; learn() notes it)


def _parse(link):
    try:
        return urlparse(str(link or "").strip())
    except ValueError:
        return None


def _host(parsed) -> str:
    return parsed.netloc.lower().split("@")[-1].split(":")[0] if parsed else ""


def _when(value) -> datetime | None:
    try:
        when = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def site_of(link) -> str | None:
    """The host of a Jibe site a posting link (or its search page's, or the site's API URL) is on,
    else None: a *.jibeapply.com host, or a company domain whose link says icims=1 (a link to a
    company's own domain without it can't be told from any other site's), or one known as Jibe."""
    parsed = _parse(link)
    host = _host(parsed)
    if not host or _ICIMS_HOST.match(host):
        return None
    path = parsed.path.rstrip("/") or "/"
    if path == _API_PATH:
        return host if "sortBy=posted_date" in parsed.query else None
    if not _JOB_PATH.match(path) and not _BOARD_PATH.match(path):
        return None
    try:
        query = {k.lower(): v for k, v in parse_qs(parsed.query).items()}
    except ValueError:
        query = {}
    if host.endswith(".jibeapply.com") or query.get("icims") == ["1"] or host in _SITES:
        return host
    return None


def api_url(host: str, page: int = 1) -> str:
    """A page of a site's postings, newest first (page 1: the newest LIMIT)."""
    return f"https://{host}{_API_PATH}?page={page}&limit={LIMIT}&sortBy=posted_date&descending=true&internal=false"


def unsorted_url(host: str, page: int = 1) -> str:
    """A page of a site's postings in the site's own order."""
    return f"https://{host}{_API_PATH}?page={page}&limit={LIMIT}&internal=false"


def read_further(jobs: list[dict], page_rows: int, since: datetime | None = None) -> bool:
    """Whether to read the next page after one with page_rows rows, whose postings (in order) end
    with jobs[-1]: when the page was full and its last posting went up since `since` (by default
    DEEP_DAYS days ago)."""
    if page_rows < LIMIT or not jobs:
        return False
    when = _when(jobs[-1].get("listed"))
    since = since or datetime.now(timezone.utc) - timedelta(days=DEEP_DAYS)
    return bool(when) and when >= since


def _usable(data) -> bool:
    return isinstance(data, dict) and isinstance(data.get("jobs"), list)


def _oldest_first(jobs: list[dict]) -> bool:
    dated = [w for w in (_when(j.get("listed")) for j in jobs) if w]
    return len(dated) >= 2 and dated[0] < dated[-1]


def read_site(host: str, get_json) -> list[dict] | None:
    """A site's newest postings (see the module's notes), or None if it couldn't be read (or, when
    get_json says so with last_status 304, it hasn't changed). get_json's last_status, when it has
    one, is the HTTP status of its last request (None: no answer)."""
    data = get_json(api_url(host))
    status = getattr(get_json, "last_status", None)
    jobs = jobs_from(data, host) if _usable(data) else None
    if jobs is None:
        # (an HTTP error other than "not found" or a throttle: the sort may be refused, as AVI-SPL's 503; no
        # answer at all, a timeout, isn't asked again; nor is "unchanged", a 304)
        return _own_order(host, get_json) if isinstance(status, int) and status not in (304, 403, 404, 429) else None
    since = datetime.now(timezone.utc) - timedelta(days=DEEP_DAYS)
    if host in _READ_AT:
        since = min(since, _READ_AT[host] - timedelta(days=1))  # (after an outage: back to the last good read)
    if _oldest_first(jobs):
        _ORDER[host] = "oldest first"
        return jobs + _newest_from_the_end(host, get_json, data, since)
    _ORDER[host] = "newest first"
    page = 1
    while page < MAX_PAGES and read_further(jobs, len(data["jobs"]), since):
        page += 1
        data = get_json(api_url(host, page))
        if not _usable(data):
            break  # (what was read stands: a Jibe site's postings aren't forgotten for missing from a read)
        jobs += jobs_from(data, host)
    return jobs


def _newest_from_the_end(host: str, get_json, first: dict, since: datetime) -> list[dict]:
    """For a site listing oldest first (Bowman): its last pages, newest last, back to `since`, up
    to MAX_PAGES - 1 of them (page 1, the oldest, is read already)."""
    try:
        last = -(-int(first.get("totalCount") or 0) // LIMIT)
    except (TypeError, ValueError):
        last = 0
    jobs: list[dict] = []
    for page in list(range(last, 1, -1))[:MAX_PAGES - 1]:
        data = get_json(api_url(host, page))
        if not _usable(data):
            break
        found = jobs_from(data, host)
        jobs += found
        oldest = _when(found[0].get("listed")) if found else None
        if not oldest or oldest < since:
            break
    return jobs


def _own_order(host: str, get_json) -> list[dict] | None:
    """A site whose API refuses the sort (AVI-SPL), read in its own order: up to MAX_PAGES pages."""
    jobs, page, data = None, 0, {"jobs": [None] * LIMIT}
    while page < MAX_PAGES and len(data["jobs"]) >= LIMIT:
        page += 1
        data = get_json(unsorted_url(host, page))
        if not _usable(data):
            break
        jobs = (jobs or []) + jobs_from(data, host)
    return jobs


def is_api_url(url) -> bool:
    parsed = _parse(url)
    return bool(parsed) and parsed.path.rstrip("/") == _API_PATH and "sortBy=posted_date" in parsed.query


def page_url(url) -> str:
    return f"https://{_host(_parse(url))}/jobs"


def job_key(link) -> tuple | None:
    """("jibe", client or host, job id) for a posting on a Jibe site, a classic iCIMS portal or
    another domain learned to belong to one (careers-amd.icims.com/jobs/101944/job), else None."""
    if "/jobs/" not in str(link or ""):  # (most links: quickly)
        return None
    parsed = _parse(link)
    host = _host(parsed)
    if not host:
        return None
    if _ICIMS_HOST.match(host):
        match = re.match(r"^/jobs/(\d+)(?:/|$)", parsed.path)
        return ("jibe", _CLIENTS[host], match.group(1)) if match and host in _CLIENTS else None
    if host not in _ALIASES and not site_of(link):
        return None
    match = _JOB_PATH.match(parsed.path.rstrip("/") or "/")
    return ("jibe", _CLIENTS.get(host, host), match.group(1)) if match else None


def client_of(api: str, jobs: list[dict]) -> str | None:
    """The client a site's read belongs to (its postings carry it)."""
    return next((j.get("client") for j in jobs or [] if j.get("client")), None)


def _strings(values) -> list[str]:
    return [str(v) for v in values if isinstance(v, str) and v] if isinstance(values, list) else []


def learn(known: dict, fetched: dict | None = None):
    """Note in the registry (known: {board key: info}), from this run's reads (fetched: {api url:
    (status, jobs, etag)}), each Jibe site's client, the classic portals its postings apply on, the
    other domains it names for them, whether its last read worked ("last_read", for same_client)
    and when its last good read was ("read_at"), and the order its API lists in ("order");
    and load all of it for job_key() and read_site()."""
    now = datetime.now(timezone.utc)
    by_url = {info.get("url"): info for info in known.values() if isinstance(info, dict) and isinstance(info.get("url"), str)}
    sites = [info for info in known.values() if isinstance(info, dict) and is_api_url(info.get("url"))]
    site_hosts = {_host(_parse(info["url"])) for info in sites}
    for url, (status, jobs, _) in (fetched or {}).items():
        info = by_url.get(url)
        if info is None or not is_api_url(url):
            continue
        info["last_read"] = "ok" if status in ("ok", "unchanged") else "failed"
        if status in ("ok", "unchanged"):
            info["read_at"] = now.isoformat(timespec="seconds")
        if status != "ok":
            continue
        host = _host(_parse(url))
        if host in _ORDER:
            info["order"] = _ORDER[host]
        info.pop("unsorted", None)
        client = client_of(url, jobs)
        if client:
            info["client"] = client
        portals = {h for j in jobs for h in j.get("apply_hosts") or ()} | set(_strings(info.get("portals")))
        aliases = {h for j in jobs for h in j.get("named_hosts") or ()} | set(_strings(info.get("aliases")))
        aliases -= site_hosts  # (another site's own address is that site's: H-E-B names H&R Block's)
        if portals:
            info["portals"] = sorted(portals)[:20]
        if aliases:
            info["aliases"] = sorted(aliases)[:10]
        else:
            info.pop("aliases", None)
    _ORDER.clear()
    _CLIENTS.clear()
    _SITES.clear()
    _ALIASES.clear()
    _READ_AT.clear()
    _READ_AT.update(last_reads(known))
    _SITES.update(site_hosts)
    for info in sites:
        host = _host(_parse(info["url"]))
        who = str(info["client"]) if info.get("client") else host
        if info.get("client"):
            _CLIENTS[host] = who
        for portal in _strings(info.get("portals")):
            _CLIENTS.setdefault(portal, who)
        for alias in addresses(info, site_hosts) - {host} - set(_strings(info.get("portals"))):
            _ALIASES.add(alias)
            _CLIENTS.setdefault(alias, who)


def addresses(info: dict, site_hosts) -> set[str]:
    """Every address a site's postings are on: its own host, its classic portals and the other
    domains it names (not one that is another site in the registry: site_hosts)."""
    host = _host(_parse(info.get("url")))
    return {host} | set(_strings(info.get("portals"))) | (set(_strings(info.get("aliases"))) - set(site_hosts))


def last_reads(known: dict) -> dict[str, datetime]:
    """host -> the latest good read ("read_at") of the site or of another site of its client: a
    company's second address, skipped for weeks while the first was read, hasn't been down."""
    sites = [i for i in known.values() if isinstance(i, dict) and is_api_url(i.get("url"))]
    latest: dict[str, datetime] = {}
    for info in sites:
        when, who = _when(info.get("read_at")), str(info.get("client") or _host(_parse(info["url"])))
        if when and (who not in latest or when > latest[who]):
            latest[who] = when
    return {_host(_parse(i["url"])): latest[w] for i in sites
            for w in [str(i.get("client") or _host(_parse(i["url"])))] if w in latest}


def etag_ok(info) -> bool:
    """Whether a site's saved ETag may be sent: only once a read has seen it list newest first
    ("order"). A site listing oldest first (Bowman) keeps the same first page while new postings
    go up at the end, so its ETag would answer "unchanged" for ever; one whose API refuses the sort
    (AVI-SPL) is read in full every time."""
    return isinstance(info, dict) and info.get("order") == "newest first"


def same_client(known: dict, skipped=frozenset(), missing=frozenset()) -> set[str]:
    """Registry keys of the Jibe sites not to read because another site has their client (the same
    company under two addresses): one site per client is read, the first by key of those that can
    be (not already skipped for another reason, nor "not found" at its last try: missing, URLs),
    one whose last read worked first; if none can be, none is skipped for this."""
    by_client: dict[str, list[str]] = {}
    for key, info in sorted(known.items()):
        client = info.get("client") if isinstance(info, dict) and is_api_url(info.get("url")) else None
        if client:
            by_client.setdefault(str(client), []).append(key)
    skip = set()
    for keys in by_client.values():
        live = [k for k in keys if k not in skipped and known[k]["url"] not in missing]
        if len(keys) > 1 and live:
            keep = min(live, key=lambda k: known[k].get("last_read") == "failed")  # (the first, unless its last read failed)
            skip |= set(keys) - {keep}
    return skip


def _posted(value) -> str | None:
    """posted_date: ISO ("2026-11-20T19:07:00+0000"), or "November 20, 2026" (Schneider Electric)."""
    when = posted_at(value)
    if when or not isinstance(value, str):
        return when
    try:
        return datetime.strptime(value.strip(), "%B %d, %Y").replace(tzinfo=timezone.utc).isoformat()
    except ValueError:
        return None


def _country(place: dict) -> str | None:
    """A place's country by its name (its code can be wrong: Viasat codes US places "FM", "PR" or
    "ZA"); a US territory counts as the US."""
    name = str(place.get("country") or "").strip()
    return "United States" if name.lower() in _US_PLACES else name or None


def _place_text(text: str, place: dict) -> str:
    """A place's text for the US filter: with its location_name too when it doesn't name its country
    (AVI-SPL's "Farnborough" is "United Kingdom" there)."""
    text, name = text.strip(), str(place.get("location_name") or "").strip()
    if not place.get("country") and name and name.lower() not in text.lower():
        text = f"{text}, {name}" if text else name
    return text


def jobs_from(data, host: str) -> list[dict]:
    """The postings in an API answer: title, link (the site's own page), places and their
    countries (the main one and every other: none if a place doesn't name its country, so the US
    filter reads the places), when it went up ("posted": the later of its posting date and the day
    it reached the site; "listed": the posting date, the API's order), and the site's client, the
    classic iCIMS portals it applies on and any other domain it names as the posting's address
    (for learn())."""
    items = data.get("jobs") if isinstance(data, dict) else None
    jobs = []
    for item in items if isinstance(items, list) else []:
        job = item.get("data") if isinstance(item, dict) else None
        if not isinstance(job, dict) or not str(job.get("title") or "").strip():
            continue
        number = str(job.get("req_id") or job.get("slug") or "").strip()
        meta = job.get("meta_data") if isinstance(job.get("meta_data"), dict) else {}
        canonical = str(meta.get("canonical_url") or "").strip()
        named = _host(_parse(canonical)) if canonical else ""
        # (the site's own link: one on another domain may not open the posting, like H-E-B's on careers.hrblock.com)
        url = canonical if canonical and (named == host or not number.isdigit()) else (
            f"https://{host}/jobs/{number}?lang={job.get('language') or 'en-us'}" if number.isdigit() else "")
        if not url:
            continue
        others = [o for o in job.get("additional_locations") or [] if isinstance(o, dict)]
        countries = [_country(p) for p in [job] + others]
        places = [_place_text(str(job.get("full_location") or job.get("short_location") or ""), job)] + [
            _place_text(", ".join(str(o[k]).strip() for k in ("city", "state", "country") if o.get(k)), o) for o in others]
        apply_host = _host(_parse(job.get("apply_url")))
        named_path = (_parse(canonical).path.rstrip("/") or "/") if named else ""
        out = {"title": str(job["title"]).strip()[:200], "url": url,
               "location": "; ".join(dict.fromkeys(p for p in places if p))[:300],
               "countries": [] if None in countries else list(dict.fromkeys(countries)),
               "client": str(job.get("client_code") or meta.get("client_code") or "").strip() or None,
               "apply_hosts": [apply_host] if _ICIMS_HOST.match(apply_host) else [],
               "named_hosts": [named] if named and named != host and _JOB_PATH.match(named_path) else []}
        listed = _posted(job.get("posted_date"))
        shown = [w for w in (_when(listed), _when(posted_at(job.get("create_date")))) if w]
        if listed:
            out["listed"] = listed
        if shown:
            out["posted"] = max(shown).isoformat()
        jobs.append(out)
    return jobs
