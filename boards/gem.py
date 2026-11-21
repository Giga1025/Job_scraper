"""Gem job boards: links like jobs.gem.com/<board>/<id> (Nuvo, Casap, 43North's portfolio companies,
Nominal, Rootly...; Gem boards are rare in the lists, so most are added under "companies").

A board is read whole through Gem's public job-board API (api_url): every open posting with its
title, link, first publication time and offices ("Philadelphia, United States", "United States -
Remote"). A shared board (43North) lists several companies' postings, titled "Company - Title".
The board name is case-sensitive, kept as written."""
import re
from urllib.parse import urlparse

from .common import posted_at

HOST = "jobs.gem.com"
_LINK = re.compile(r"^/([A-Za-z0-9_-]+)(?:/([A-Za-z0-9_=-]+))?(?:/application)?/?$")
_API = re.compile(r"^/job_board/v0/([A-Za-z0-9_-]+)/job_posts/?$")


def _parse(link):
    try:
        return urlparse(str(link or "").strip())
    except ValueError:
        return None


def board_of(link) -> str | None:
    """The board a Gem link (a posting's, the board's, or its API URL) is on, else None."""
    parsed = _parse(link)
    if not parsed:
        return None
    host = parsed.netloc.lower()
    match = _API.match(parsed.path) if host == "api.gem.com" else _LINK.match(parsed.path) if host == HOST else None
    if not match or match.group(1).lower() in ("api", "static"):
        return None
    return match.group(1)


def job_key(link) -> tuple | None:
    """("gem", board, posting id) for a posting's link, else None."""
    parsed = _parse(link)
    match = _LINK.match(parsed.path) if parsed and parsed.netloc.lower() == HOST else None
    return ("gem", match.group(1).lower(), match.group(2)) if match and match.group(2) else None


def api_url(board: str) -> str:
    return f"https://api.gem.com/job_board/v0/{board}/job_posts/"


def is_api_url(url) -> bool:
    parsed = _parse(url)
    return bool(parsed) and parsed.netloc.lower() == "api.gem.com" and bool(_API.match(parsed.path))


def page_url(url) -> str:
    return f"https://{HOST}/{board_of(url)}"


def _country(place: str) -> str | None:
    """The country in an office's name: "Toronto, Canada" -> Canada, "United States - Remote" ->
    United States; None for "Remote" or nothing."""
    place = place.strip()
    if place.lower().endswith(" - remote"):
        return place[: -len(" - remote")].strip() or None
    return place.rsplit(", ", 1)[1].strip() if ", " in place else None


def jobs_from(data) -> list[dict]:
    """The postings in an API answer (a list of postings)."""
    jobs = []
    for post in data if isinstance(data, list) else []:
        if not isinstance(post, dict) or not str(post.get("title") or "").strip() or not post.get("absolute_url"):
            continue
        offices = [o.get("location", {}).get("name") if isinstance(o.get("location"), dict) else o.get("name")
                   for o in post.get("offices") or [] if isinstance(o, dict)]
        places = [str(p).strip() for p in offices if p] or ([str(post["location"].get("name")).strip()]
                                                             if isinstance(post.get("location"), dict) and post["location"].get("name") else [])
        countries = [_country(p) for p in places]
        countries = [] if None in countries else countries  # ("Remote" names no country: the US filter reads the places then)
        job = {"title": str(post["title"]).strip()[:200], "url": str(post["absolute_url"]).strip(),
               "location": "; ".join(dict.fromkeys(places)), "countries": list(dict.fromkeys(countries))}
        if posted_at(post.get("first_published_at") or post.get("created_at")):
            job["posted"] = posted_at(post.get("first_published_at") or post.get("created_at"))
        jobs.append(job)
    return jobs
