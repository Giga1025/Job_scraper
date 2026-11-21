"""iCIMS career sites built on Jibe: <name>.jibeapply.com, or a company's own domain (careers.amd.com,
jobs.keysight.com, careers.rivian.com, careers.garmin.com, careers.jhuapl.edu, careers.viasat.com,
careers.docusign.com...; about 130 sites in the lists, linked like careers.amd.com/jobs/85806?icims=1).

Every Jibe site serves its postings through one public JSON API at its root (api_url): the newest
LIMIT a page, newest first (sortBy=posted_date), with the title, the iCIMS job id, every place's
country and the posting date. Only the newest are read (a big company lists thousands): the first
page, and the next while the last posting on a page went up in the last DEEP_DAYS days (a site
that posts a hundred a day, like Schneider Electric's), up to MAX_PAGES. So a posting missing from
a read may still be open; it isn't forgotten for that (job_monitor.FORGET_GONE_PLATFORMS).

A site is one iCIMS customer (its "client", read from the API): one company can have two sites
(careers.ice.com and ice.jibeapply.com), and a site's postings are also on the customer's classic
iCIMS portals (careers-amd.icims.com, global-external-amd.icims.com...), with the same job id. So a
posting is known by (client, job id) on any of those addresses: learn() notes, from each read,
which addresses belong to which client, and job_key() uses that (an address not learned yet: by
its host). job_monitor reads one site per client."""
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

from .common import posted_at

LIMIT = 100  # postings a page (the API's most), newest first
MAX_PAGES = 5
DEEP_DAYS = 3

_JOB_PATH = re.compile(r"^/(?:[^/]+/)*jobs/(\d+)/?$")
_API_PATH = "/api/jobs"
_ICIMS_HOST = re.compile(r"^[a-z0-9-]+\.icims\.com$")

# host -> client, for every address learned (Jibe sites and the classic iCIMS portals behind them)
_CLIENTS: dict[str, str] = {}
_SITES: set[str] = set()  # the hosts of the Jibe sites in the registry (their links needn't say icims=1)


def _parse(link):
    try:
        return urlparse(str(link or "").strip())
    except ValueError:
        return None


def _host(parsed) -> str:
    return parsed.netloc.lower().split("@")[-1].split(":")[0] if parsed else ""


def site_of(link) -> str | None:
    """The host of a Jibe site a posting link (or the site's API URL) is on, else None: a
    *.jibeapply.com host, or a company domain whose link says icims=1, or one learned as Jibe."""
    parsed = _parse(link)
    host = _host(parsed)
    if not host or _ICIMS_HOST.match(host):
        return None
    path = parsed.path.rstrip("/") or "/"
    if path == _API_PATH:
        return host if "sortBy=posted_date" in parsed.query else None
    if not _JOB_PATH.match(path):
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


def read_further(jobs: list[dict], page_rows: int) -> bool:
    """Whether to read the next page after one with page_rows rows, whose postings (in order) end
    with jobs[-1]: when the page was full and its last posting went up in the last DEEP_DAYS days."""
    if page_rows < LIMIT or not jobs or not jobs[-1].get("posted"):
        return False
    try:
        when = datetime.fromisoformat(jobs[-1]["posted"])
    except ValueError:
        return False
    when = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).total_seconds() < DEEP_DAYS * 86400


def is_api_url(url) -> bool:
    parsed = _parse(url)
    return bool(parsed) and parsed.path.rstrip("/") == _API_PATH and "sortBy=posted_date" in parsed.query


def page_url(url) -> str:
    return f"https://{_host(_parse(url))}/jobs"


def job_key(link) -> tuple | None:
    """("jibe", client or host, job id) for a posting on a Jibe site or a classic iCIMS portal
    learned to belong to one (careers-amd.icims.com/jobs/101944/job), else None."""
    parsed = _parse(link)
    host = _host(parsed)
    if not host:
        return None
    if _ICIMS_HOST.match(host):
        match = re.match(r"^/jobs/(\d+)(?:/|$)", parsed.path)
        return ("jibe", _CLIENTS[host], match.group(1)) if match and host in _CLIENTS else None
    if not site_of(link):
        return None
    match = _JOB_PATH.match(parsed.path.rstrip("/") or "/")
    return ("jibe", _CLIENTS.get(host, host), match.group(1)) if match else None


def client_of(api: str, jobs: list[dict]) -> str | None:
    """The client a site's read belongs to (its postings carry it)."""
    return next((j.get("client") for j in jobs or [] if j.get("client")), None)


def learn(known: dict, fetched: dict | None = None):
    """Note each Jibe site's client (and the classic portals its postings apply on) in the registry
    (known: {board key: info}), from this run's reads (fetched: {api url: (status, jobs, etag)}), and
    load every address learned so far for job_key()."""
    by_url = {info.get("url"): info for info in known.values() if isinstance(info, dict)}
    for url, (status, jobs, _) in (fetched or {}).items():
        info = by_url.get(url)
        if info is None or status != "ok" or not is_api_url(url):
            continue
        client = client_of(url, jobs)
        if client:
            info["client"] = client
            portals = sorted({h for j in jobs for h in j.get("apply_hosts") or ()} | set(info.get("portals") or ()))
            info["portals"] = portals[:20]
    _CLIENTS.clear()
    _SITES.clear()
    for info in known.values():
        if isinstance(info, dict) and is_api_url(info.get("url")):
            _SITES.add(_host(_parse(info["url"])))
            if info.get("client"):
                _CLIENTS[_host(_parse(info["url"]))] = str(info["client"])
                for portal in info.get("portals") or ():
                    _CLIENTS.setdefault(str(portal), str(info["client"]))


def same_client(known: dict) -> set[str]:
    """Registry keys of Jibe sites whose client another site already has (the same company under
    two addresses): read one site per client, the first by key."""
    seen, skip = set(), set()
    for key, info in sorted(known.items()):
        client = info.get("client") if isinstance(info, dict) and is_api_url(info.get("url")) else None
        if client:
            if client in seen:
                skip.add(key)
            seen.add(client)
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


def jobs_from(data, host: str) -> list[dict]:
    """The postings in an API answer: title, link (the site's own page), places and their
    countries (the main one and every other), when it went up, and the site's client and the
    classic iCIMS portals it applies on (for learn())."""
    items = data.get("jobs") if isinstance(data, dict) else None
    jobs = []
    for item in items if isinstance(items, list) else []:
        job = item.get("data") if isinstance(item, dict) else None
        if not isinstance(job, dict) or not str(job.get("title") or "").strip():
            continue
        number = str(job.get("req_id") or job.get("slug") or "").strip()
        meta = job.get("meta_data") if isinstance(job.get("meta_data"), dict) else {}
        url = str(meta.get("canonical_url") or "").strip() or (
            f"https://{host}/jobs/{number}?lang={job.get('language') or 'en-us'}" if number.isdigit() else "")
        if not url:
            continue
        others = [o for o in job.get("additional_locations") or [] if isinstance(o, dict)]
        countries = [str(c).strip() for c in [job.get("country_code") or job.get("country")]
                     + [o.get("country_code") or o.get("country") for o in others] if c and str(c).strip()]
        apply_host = _host(_parse(job.get("apply_url")))
        out = {"title": str(job["title"]).strip()[:200], "url": url,
               "location": str(job.get("full_location") or job.get("short_location") or "").strip()[:300],
               "countries": list(dict.fromkeys(countries)),
               "client": str(job.get("client_code") or meta.get("client_code") or "").strip() or None,
               "apply_hosts": [apply_host] if _ICIMS_HOST.match(apply_host) else []}
        if _posted(job.get("posted_date")):
            out["posted"] = _posted(job.get("posted_date"))
        jobs.append(out)
    return jobs
