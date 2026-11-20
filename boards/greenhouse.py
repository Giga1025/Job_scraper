"""Greenhouse boards shown on companies' own careers sites: links like
https://careers.airbnb.com/positions/7859317?gh_jid=7859317 (Airbnb, AQR, Duolingo, Block, Akuna
Capital, Andreessen Horowitz... about 260 sites in the lists).

A company's Greenhouse board is read through Greenhouse's job-board API, by the board's name
(boards-api.greenhouse.io/v1/boards/<name>/jobs: job_monitor.board_api_url). A link to
job-boards.greenhouse.io/<name>/jobs/<id> names it; a link to the company's own site doesn't, only
the posting's Greenhouse job id (gh_jid), so those companies reached you only through the lists.
find_boards works the board's name out, each way checked:

1. from a posting: Greenhouse's embed address for a job (boards.greenhouse.io/embed/job_app?
   token=<job id>) answers with a redirect naming its board (for=<name>) while the posting is
   open (404 once it's closed). Each job id is asked about once;
2. failing that (a site whose postings in the lists are all closed, or not asked about yet),
   by guessing names made from the site's address and the company's name: a guess is taken only
   if that board's own postings link back to the same site (the company sets that link);
3. failing that, from a posting's page: the board name a careers page embeds
   (boards.greenhouse.io/embed/job_board?for=<name>...), taken if that board's postings link to
   the same site, or to Greenhouse's own pages and the name starts like a guess (a page can link
   other companies' boards: a fund's, its portfolio's).

Steps 2 and 3 are tried again GUESS_AGAIN_DAYS later while a site has no board. A site can have
several boards (a company's main and campus boards, or a careers platform several companies use):
a job id on a site is asked about unless a board found for that site already lists it.

What is remembered (meta["greenhouse_sites"], in state.json), per site: the boards found, the job
ids asked about (the newest ASKED_KEPT) and when guessing was last tried."""
import logging
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from urllib.parse import parse_qs, unquote, urlparse

import requests

from .common import HEADERS

log = logging.getLogger("job_monitor.greenhouse")

EMBED_URL = "https://boards.greenhouse.io/embed/job_app?token={}"
BOARD_API = "https://boards-api.greenhouse.io/v1/boards/{}/jobs"
BOARD_LINK = "https://job-boards.greenhouse.io/{}"  # what job_monitor.board_api_url reads a board's name from
ASKS_PER_SITE = 3          # job ids asked about per site per run (newest first)...
ASKED_KEPT = 200           # ...each once: the newest this many are remembered per site
GUESS_AGAIN_DAYS = 7       # a site with no board found: guesses and its page tried again this long after
PAGES_PER_SITE = 2         # posting pages read per site for an embedded board name
SECONDS_PER_RUN = 240      # at most this long a run (the rest waits for the next): the first takes ~3 minutes
WORKERS = 4
TIMEOUT_SECONDS = 20

_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,79}$")
_FOR = re.compile(r"[?&]for=([^&#]+)")
_EMBEDDED = re.compile(
    r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board(?:/js)?\?(?:[^\"'\s<>]*?&)?for=|embed/job_app\?(?:[^\"'\s<>]*?&)?for=)([A-Za-z0-9_-]+)"
    r"|(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?!embed\b)([A-Za-z0-9_-]+)"
    r"|boards-api(?:\.eu)?\.greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)", re.IGNORECASE)
# Parts of a careers site's address that aren't the company's name.
_NOT_A_NAME = {"www", "careers", "career", "jobs", "job", "about", "app", "apply", "boards", "work", "join", "en",
               "us", "global", "corporate", "corp", "company", "go", "hire", "talent", "team", "life", "people"}
_SECOND_LEVEL = {"co", "com", "org", "net", "ac", "gov", "edu"}


def _parse(link):
    try:
        return urlparse(str(link or "").strip())
    except ValueError:  # e.g. "https://[x"
        return None


def site_of(link) -> str:
    """The site a link is on: its host, lower-case, without "www."."""
    parsed = _parse(link)
    return parsed.netloc.lower().split("@")[-1].split(":")[0].removeprefix("www.") if parsed else ""


def job_id(link) -> str | None:
    """The Greenhouse job id in a link to a posting on a company's own site (?gh_jid=...), else
    None (also for Greenhouse's own pages, whose board job_monitor.board_api_url already reads)."""
    parsed, site = _parse(link), site_of(link)
    if not parsed or not site or site.endswith("greenhouse.io"):
        return None
    try:
        ids = parse_qs(parsed.query).get("gh_jid") or []
    except ValueError:
        return None
    return ids[0] if ids and ids[0].isdigit() and len(ids[0]) <= 20 else None


def _domain(site: str) -> str:
    """A site's registered domain: careers.airbnb.com -> airbnb.com, jobs.example.co.uk -> example.co.uk."""
    labels = site.split(".")
    keep = 3 if len(labels) >= 3 and labels[-2] in _SECOND_LEVEL and len(labels[-1]) == 2 else 2
    return ".".join(labels[-keep:])


def _company_key(name) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(name or "").lower().replace("&", "and"))


def guesses(site: str, companies) -> list[str]:
    """Board names to try for a site: from its address (careers.airbnb.com -> airbnb), then from
    the companies' names in the lists (Abnormal Security -> abnormalsecurity)."""
    labels = [l for l in site.split(".")[:-1] if l not in _NOT_A_NAME and l not in _SECOND_LEVEL]
    names = [labels[-1].replace("-", ""), "".join(labels).replace("-", "")] if labels else []
    for company in companies:
        key = _company_key(company)
        names.append(key)
        trimmed = re.sub(r"(incorporated|inc|llc|ltd|corporation|corp|company|co|group|holdings)$", "", key)
        names.append(trimmed)
    out: list[str] = []
    for name in names:
        if name and _NAME.match(name) and name not in out:
            out.append(name)
    return out[:6]


def embedded_names(html: str) -> list[str]:
    """The Greenhouse board names a page refers to (an embedded board, a link to one), in order."""
    out: list[str] = []
    for match in _EMBEDDED.finditer(html or ""):
        name = next(g for g in match.groups() if g).lower()
        if name not in out and name not in ("embed", "v1", "jobs", "job_app", "job_board"):
            out.append(name)
    return out


class _Asker:
    """Greenhouse requests for one run: stops at the deadline, and for the rest of the run once
    Greenhouse answers "too many requests"."""

    def __init__(self, get, deadline: float):
        self.get, self.deadline, self.stopped = get, deadline, False

    def ok(self) -> bool:
        return not self.stopped and time.monotonic() < self.deadline

    def request(self, url, greenhouse: bool = True, **kwargs):
        """The answer to a GET (None if none, or no time left). greenhouse: a request to
        Greenhouse, whose "too many requests" stops the rest of the run's."""
        if not self.ok():
            return None
        try:
            resp = self.get(url, headers=HEADERS, timeout=TIMEOUT_SECONDS, **kwargs)
        except Exception as exc:
            log.debug(f"  [greenhouse] {url}: {exc!r}")
            return None
        if resp.status_code == 429 and greenhouse:
            self.stopped = True
            log.info("  [greenhouse] Greenhouse answered \"too many requests\"; the rest waits for the next run.")
            return None
        return resp

    def board_of(self, job: str) -> str | None | bool:
        """The board a job id is on: its name; False if Greenhouse doesn't know it (closed); None
        if it couldn't be asked (no answer, or no time left)."""
        resp = self.request(EMBED_URL.format(job), allow_redirects=False)
        if resp is None:
            return None
        if resp.status_code in (301, 302, 303, 307, 308):
            match = _FOR.search(str((resp.headers or {}).get("location") or (resp.headers or {}).get("Location") or ""))
            name = unquote(match.group(1)).strip().lower() if match else ""
            return name if _NAME.match(name) else False
        return False if resp.status_code in (404, 410) else None

    def sites_of_board(self, name: str) -> set[str] | None:
        """The sites a board's postings link to (their absolute_url); None if there's no such board."""
        resp = self.request(BOARD_API.format(name))
        if resp is None or resp.status_code != 200:
            return None
        try:
            jobs = resp.json().get("jobs") or []
        except (ValueError, AttributeError):
            return None
        return {site_of(j.get("absolute_url")) for j in jobs if isinstance(j, dict)} - {""}


def find_boards(links, memory: dict, today: str | None = None, listed=None, get=None,
                seconds: float = SECONDS_PER_RUN) -> list[tuple[str, str]]:
    """Work out the Greenhouse boards behind these (link, company) pairs' company-site postings
    (job_id), as described above. memory is meta["greenhouse_sites"] and is updated. listed(names)
    gives the job ids these boards (those found for a site) list now (those aren't asked about).

    Returns (board link, company) pairs for job_monitor.grow_board_registry: every board known for
    the sites in links (each run, so a board that left the registry comes back)."""
    today = today or date.today().isoformat()
    get = get or requests.get
    listed = listed or (lambda names: set())
    by_site: dict[str, list[tuple[str, str, str]]] = {}  # site -> (job id, company, link), in the order given
    for link, company in links:
        job = job_id(link)
        if job:
            jobs = by_site.setdefault(site_of(link), [])
            if all(job != j for j, _, _ in jobs):
                jobs.append((job, str(company or "").strip(), str(link).strip()))
    asker = _Asker(get, time.monotonic() + seconds)
    found: list[str] = []

    def one(site: str):
        record = memory.setdefault(site, {})
        boards: dict = record.setdefault("boards", {})
        asked: list = record.setdefault("asked", [])
        done = set(asked) | set(listed(list(boards)) if boards else ())
        for job, company, _ in [p for p in by_site[site] if p[0] not in done][:ASKS_PER_SITE]:
            name = asker.board_of(job)
            if name is None:
                continue  # not asked (no answer or no time): asked again next run
            asked.append(job)
            if name and name not in boards:
                boards[name] = company or name
                found.append(name)
        del asked[:-ASKED_KEPT]
        if boards or str(record.get("guessed") or "") > _days_before(today, GUESS_AGAIN_DAYS):
            return
        companies = [c for c, _ in Counter(c for _, c, _ in by_site[site] if c).most_common()]
        name = next((g for g in guesses(site, companies)
                     if asker.ok() and any(_domain(s) == _domain(site) for s in asker.sites_of_board(g) or ())), None)
        for _, _, link in by_site[site][:PAGES_PER_SITE] if not name else []:
            page = asker.request(link, greenhouse=False)
            for embedded in embedded_names(page.text if page is not None and page.status_code == 200 else ""):
                sites = asker.sites_of_board(embedded)
                if sites and any(_domain(s) == _domain(site) or (s.endswith("greenhouse.io") and any(
                        embedded.startswith(g) for g in guesses(site, companies))) for s in sites):
                    name = embedded
                    break
            if name:
                break
        if not name and not asker.ok():
            return  # (out of time, or stopped: guessed again next run)
        record["guessed"] = today
        if name:
            boards[name] = companies[0] if companies else name
            found.append(name)

    sites = sorted(by_site, key=lambda s: (bool((memory.get(s) or {}).get("boards")), s))  # sites with no board first
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        list(pool.map(lambda s: _safely(one, s), sites))
    unknown = sum(1 for s in by_site if not (memory.get(s) or {}).get("boards"))
    log.info(f"  [greenhouse] Companies' own sites: {len(by_site)} with Greenhouse postings, {len(found)} board(s) found "
             f"this run{': ' + ', '.join(sorted(found)[:12]) + (' ...' if len(found) > 12 else '') if found else ''}; "
             f"{unknown} site(s) not matched yet{' (out of time: the rest next run)' if not asker.ok() else ''}.")
    return [(BOARD_LINK.format(name), company) for site in by_site
            for name, company in ((memory.get(site) or {}).get("boards") or {}).items()]


def _safely(fn, site):
    try:
        fn(site)
    except Exception:
        log.exception(f"  [greenhouse] Could not work out the board of {site}")


def _days_before(today: str, days: int) -> str:
    try:
        return date.fromordinal(date.fromisoformat(today).toordinal() - days).isoformat()
    except ValueError:
        return ""
