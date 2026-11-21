"""Rippling job boards: links like ats.rippling.com/<board>/jobs/<id> (about 180 boards in the lists:
Rippling, Boom Supersonic, D-Wave, Root Insurance, Foundation Robotics, Riot Platforms...).

A board is read whole through the public JSON API its page uses (api_url), one row per job with
every place and its country (groupJobsByLocation). It gives no posting date, so a board's first
check only records what it lists (nothing is emailed as new), and from then on a job not seen
before is new. The board name is case-sensitive, kept as written."""
import re
from urllib.parse import urlparse

PAGE_SIZE = 1000  # the API's most
MAX_PAGES = 10    # a board with more isn't read (it couldn't be read whole: see job_monitor.fetch_rippling_jobs)

_LINK = re.compile(r"^/(?:[a-z]{2}(?:-[A-Za-z0-9]{2,3})?/)?(?:embed/)?([A-Za-z0-9_-]+)/jobs(?:/[0-9a-f-]{36})?(?:/apply)?/?$")
_API = re.compile(r"^/api/v2/board/([A-Za-z0-9_-]+)/jobs/?$")
HOST = "ats.rippling.com"


def _parse(link):
    try:
        return urlparse(str(link or "").strip())
    except ValueError:
        return None


def board_of(link) -> str | None:
    """The board a Rippling link (a posting's, the board's page, or its API URL) is on, else None."""
    parsed = _parse(link)
    if not parsed or parsed.netloc.lower() != HOST:
        return None
    match = _API.match(parsed.path) or _LINK.match(parsed.path)
    if not match or match.group(1).lower() in ("api", "embed"):
        return None
    return match.group(1)


def api_url(board: str, page: int = 0) -> str:
    return f"https://{HOST}/api/v2/board/{board}/jobs?page={page}&pageSize={PAGE_SIZE}&groupJobsByLocation=true"


def is_api_url(url) -> bool:
    parsed = _parse(url)
    return bool(parsed) and parsed.netloc.lower() == HOST and bool(_API.match(parsed.path))


def page_url(url) -> str:
    return f"https://{HOST}/{board_of(url)}/jobs"


def jobs_from(data) -> tuple[list[dict], int]:
    """(postings, number of pages) from one API answer."""
    items = data.get("items") if isinstance(data, dict) else None
    jobs = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or not str(item.get("name") or "").strip() or not item.get("url"):
            continue
        places = [p for p in item.get("locations") or [] if isinstance(p, dict)]
        jobs.append({"title": str(item["name"]).strip()[:200], "url": str(item["url"]).strip(),
                     "location": "; ".join(dict.fromkeys(str(p.get("name")).strip() for p in places if p.get("name"))),
                     "countries": list(dict.fromkeys(str(p.get("countryCode")).strip() for p in places if p.get("countryCode")))})
    try:
        pages = int(data.get("totalPages") or 1) if isinstance(data, dict) else 1
    except (TypeError, ValueError):
        pages = 1
    return jobs, pages
