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

Step 2 is tried again GUESS_AGAIN_DAYS later (also for a site with a board: the one a posting
named can be a side board, a referrals or campus board, and the guess the main one), step 3 while
a site has no board; a step whose requests went unanswered is tried again next run. A site can
have several boards (main and campus boards, or a careers platform several companies use, each
board named after the company whose posting or name found it): a job id on a site is asked about
unless a board found for that site already lists it.

What is remembered (meta["greenhouse_sites"], in state.json), per site: the boards found, the job
ids asked about (the newest ASKED_KEPT) and when guessing was last tried."""
import html
import logging
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from urllib.parse import parse_qsl, unquote, urlparse

import requests

from .common import HEADERS

log = logging.getLogger("job_monitor.greenhouse")

EMBED_URL = "https://boards.greenhouse.io/embed/job_app?token={}"
BOARD_API = "https://boards-api.greenhouse.io/v1/boards/{}/jobs"
BOARD_LINK = "https://job-boards.greenhouse.io/{}"  # what job_monitor.board_api_url reads a board's name from
ASKS_PER_SITE = 3          # job ids asked about per site per run (newest first)...
ASKED_KEPT = 1000          # ...each once: the highest this many are remembered per site (ids grow over time)
GUESS_AGAIN_DAYS = 7       # guesses (and, for a site with no board, its pages) tried again this long after
PAGES_PER_SITE = 2         # posting pages read per site for an embedded board name
SECONDS_PER_RUN = 240      # at most this long a run (the rest waits for the next): the first takes about a minute
GIVE_UP_AFTER = 10         # Greenhouse requests in a row with no answer (errors, timeouts, 403, 5xx): the rest wait a run
WORKERS = 4
TIMEOUT_SECONDS = 20

_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,79}$")
_FOR = re.compile(r"[?&]for=([^&#]+)")
_GH = r"(?:boards|job-boards)(?:\.[a-z]{2,3})?\.greenhouse\.io"
_EMBEDDED = re.compile(
    _GH + r"/embed/job_(?:board(?:/js)?|app)\?(?:[^\"'\s<>]*?&)?for=([A-Za-z0-9_-]+)"
    r"|" + _GH + r"/(?!embed\b)([A-Za-z0-9_-]+)"
    r"|boards-api(?:\.[a-z]{2,3})?\.greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)", re.IGNORECASE)
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
    """The Greenhouse job id in a link to a posting on a company's own site (?gh_jid=..., in any
    case, as job_monitor.job_keys reads it), else None (also for Greenhouse's own pages, whose board
    job_monitor.board_api_url already reads)."""
    parsed, site = _parse(link), site_of(link)
    if not parsed or not site or site.endswith("greenhouse.io"):
        return None
    try:
        ids = [v for k, v in parse_qsl(parsed.query) if k.lower() == "gh_jid"]
    except ValueError:
        return None
    return ids[0] if ids and ids[0].isdigit() and len(ids[0]) <= 20 else None


def domain_of(site: str) -> str:
    """A site's registered domain: careers.airbnb.com -> airbnb.com, jobs.example.co.uk -> example.co.uk."""
    labels = site.split(".")
    keep = 3 if len(labels) >= 3 and labels[-2] in _SECOND_LEVEL and len(labels[-1]) == 2 else 2
    return ".".join(labels[-keep:])


def _company_key(name) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(name or "").lower().replace("&", "and"))


def guesses(site: str, companies) -> list[tuple[str, str]]:
    """(board name, company it would be named after) to try for a site: from its address
    (careers.airbnb.com -> airbnb), only for a site the lists give one company (a careers platform
    several companies use isn't any one's), then from each company's name (Abnormal Security ->
    abnormalsecurity)."""
    companies = [c for c in companies if c]
    labels = [l for l in site.split(".")[:-1] if l not in _NOT_A_NAME and l not in _SECOND_LEVEL]
    pairs = []
    if labels and len({_company_key(c) for c in companies}) <= 1:
        owner = companies[0] if companies else ""
        pairs += [(labels[-1].replace("-", ""), owner), ("".join(labels).replace("-", ""), owner)]
    for company in companies:
        key = _company_key(company)
        pairs += [(key, company), (re.sub(r"(incorporated|inc|llc|ltd|corporation|corp|company|co|group|holdings)$", "", key), company)]
    out: list[tuple[str, str]] = []
    for name, company in pairs:
        if name and _NAME.match(name) and all(name != n for n, _ in out):
            out.append((name, company))
    return out[:8]


def embedded_names(page: str) -> list[str]:
    """The Greenhouse board names a page refers to (an embedded board, a link to one), in order;
    also written HTML-escaped (&amp;) or JSON-escaped (\\/)."""
    out: list[str] = []
    for match in _EMBEDDED.finditer(html.unescape(page or "").replace("\\/", "/")):
        name = next(g for g in match.groups() if g).lower()
        if name not in out and name not in ("embed", "v1", "jobs", "job_app", "job_board"):
            out.append(name)
    return out


class _Asker:
    """The requests of one run: none after the deadline, none to Greenhouse once it answers "too
    many requests", and none of a kind (job lookups, board reads) once Greenhouse gives that kind
    no answer GIVE_UP_AFTER times in a row."""

    def __init__(self, get, deadline: float):
        self.get, self.deadline, self.stopped = get, deadline, False
        self.misses: Counter = Counter()

    def ok(self, kind: str | None = None) -> bool:
        return not self.stopped and time.monotonic() < self.deadline and self.misses[kind] < GIVE_UP_AFTER

    def request(self, url, greenhouse: str | None = "board", **kwargs):
        """The answer to a GET; None if none (an error, a timeout; from Greenhouse also 403 and
        5xx), or no time left. greenhouse: the kind of request to Greenhouse ("lookup", "board"),
        or None for another site."""
        if not self.ok(greenhouse):
            return None
        try:
            resp = self.get(url, headers=HEADERS, timeout=TIMEOUT_SECONDS, **kwargs)
        except Exception as exc:
            log.debug(f"  [greenhouse] {url}: {exc!r}")
            resp = None
        if not greenhouse:
            return resp
        if resp is not None and resp.status_code == 429:
            self.stopped = True
            log.info("  [greenhouse] Greenhouse answered \"too many requests\"; the rest waits for the next run.")
            return None
        if resp is None or resp.status_code == 403 or resp.status_code >= 500:
            self.misses[greenhouse] += 1
            if self.misses[greenhouse] == GIVE_UP_AFTER:
                log.info(f"  [greenhouse] No answer from Greenhouse to {GIVE_UP_AFTER} {greenhouse} requests in a row; "
                         f"the rest wait for the next run.")
            return None
        self.misses[greenhouse] = 0
        return resp

    def board_of(self, job: str) -> str | None | bool:
        """The board a job id is on: its name; False if Greenhouse doesn't know it (closed); None
        if it couldn't be asked (no answer, or no time left)."""
        resp = self.request(EMBED_URL.format(job), greenhouse="lookup", allow_redirects=False)
        if resp is None:
            return None
        if resp.status_code in (301, 302, 303, 307, 308):
            match = _FOR.search(str((resp.headers or {}).get("location") or (resp.headers or {}).get("Location") or ""))
            name = unquote(match.group(1)).strip().lower() if match else ""
            return name if _NAME.match(name) else False
        return False if resp.status_code in (404, 410) else None

    def sites_of_board(self, name: str) -> set[str] | None | bool:
        """The sites a board's postings link to (their absolute_url); False if there's no such
        board; None if Greenhouse gave no answer."""
        resp = self.request(BOARD_API.format(name))
        if resp is None:
            return None
        if resp.status_code in (404, 410):
            return False
        if resp.status_code != 200:
            return None
        try:
            jobs = resp.json().get("jobs") or []
        except (ValueError, AttributeError):
            return None
        return {site_of(j.get("absolute_url")) for j in jobs if isinstance(j, dict)} - {""}


def _record(memory: dict, site: str) -> dict:
    """A site's record in memory, repaired if state.json holds something else."""
    record = memory.get(site) if isinstance(memory.get(site), dict) else {}
    boards, asked = record.get("boards"), record.get("asked")
    memory[site] = {**record,
                    "boards": {str(k): str(v) for k, v in boards.items()} if isinstance(boards, dict) else {},
                    "asked": [str(j) for j in asked if str(j).isdigit()] if isinstance(asked, list) else []}
    return memory[site]


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
    for site in by_site:
        _record(memory, site)
    asker = _Asker(get, time.monotonic() + seconds)
    found: list[str] = []

    def board_links_back(name: str, site: str, companies) -> bool | None:
        """Whether a board's postings link to the site (None: no answer)."""
        sites = asker.sites_of_board(name)
        if sites is None:
            return None
        return bool(sites) and any(domain_of(s) == domain_of(site) for s in sites)

    def one(site: str):
        record = memory[site]
        boards, asked = record["boards"], record["asked"]
        done = set(asked) | (set(listed(list(boards))) if boards else set())
        for job, company, _ in [p for p in by_site[site] if p[0] not in done][:ASKS_PER_SITE]:
            name = asker.board_of(job)
            if name is None:
                continue  # not asked (no answer or no time): asked again next run
            asked.append(job)
            if name and name not in boards:
                boards[name] = company or name
                found.append(name)
        record["asked"] = sorted(set(asked), key=int)[-ASKED_KEPT:]
        if str(record.get("guessed") or "") > _days_before(today, GUESS_AGAIN_DAYS):
            return
        companies = [c for c, _ in Counter(c for _, c, _ in by_site[site] if c).most_common()]
        unanswered, name, owner = False, None, ""
        for guess, company in guesses(site, companies):
            if guess in boards or not asker.ok("board"):
                unanswered |= guess not in boards
                continue
            linked = board_links_back(guess, site, companies)
            unanswered |= linked is None
            if linked:
                name, owner = guess, company
                break
        for _, company, link in by_site[site][:PAGES_PER_SITE] if not name and not boards else []:
            page = asker.request(link, greenhouse=None)
            if page is None or page.status_code >= 500:
                unanswered = True
                continue
            for embedded in embedded_names(page.text):  # (a closed posting's page too: often the site's usual page)
                sites = asker.sites_of_board(embedded) if embedded not in boards else False
                unanswered |= sites is None
                if sites and any(domain_of(s) == domain_of(site) or (s.endswith("greenhouse.io") and any(
                        embedded.startswith(g) for g, _ in guesses(site, companies))) for s in sites):
                    name, owner = embedded, company
                    break
            if name:
                break
        if name:
            boards[name] = owner or name
            found.append(name)
        if name or not unanswered:
            record["guessed"] = today  # (else tried again next run)

    sites = sorted(by_site, key=lambda s: (bool(memory[s]["boards"]), s))  # sites with no board first
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        list(pool.map(lambda s: _safely(one, s), sites))
    unknown = sum(1 for s in by_site if not memory[s]["boards"])
    log.info(f"  [greenhouse] Companies' own sites: {len(by_site)} with Greenhouse postings, {len(found)} board(s) found "
             f"this run{': ' + ', '.join(sorted(found)[:12]) + (' ...' if len(found) > 12 else '') if found else ''}; "
             f"{unknown} site(s) not matched yet{' (the rest next run)' if not (asker.ok('lookup') and asker.ok('board')) else ''}.")
    return [(BOARD_LINK.format(name), company) for site in by_site for name, company in memory[site]["boards"].items()]


def boards_by_domain(memory: dict) -> dict[str, set[str]]:
    """{registered domain: the board names found for its sites}, from meta["greenhouse_sites"]."""
    out: dict[str, set[str]] = {}
    for site, record in (memory or {}).items():
        if isinstance(record, dict) and isinstance(record.get("boards"), dict) and record["boards"]:
            out.setdefault(domain_of(site), set()).update(str(name) for name in record["boards"])
    return out


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
