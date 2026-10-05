#!/usr/bin/env python3
"""
Job Page Monitor (v2 — with browser support)
=============================================
Monitors company career pages for new job postings and sends email alerts.
Supports both static HTML pages and JavaScript-rendered pages (like Microsoft).

Usage:
    1. First run creates config.json — edit it with your targets
    2. Run: python job_monitor.py
    3. Schedule with cron for periodic checks

Requirements:
    pip install requests beautifulsoup4 lxml playwright
    playwright install chromium
"""

import copy
import json
import hashlib
import re
import unicodedata
import os
import sys
import argparse
import smtplib
import stat
import ssl
import logging
import tempfile
import time
import html as html_lib
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin
from urllib.parse import urlparse, urlunparse, parse_qs

try:
    import fcntl  # POSIX only; on Windows saves to state.json are not locked
except ImportError:
    fcntl = None

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("Missing dependencies. Run:")
    print("  pip install requests beautifulsoup4 lxml playwright")
    print("  playwright install chromium")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
CONFIG_ALL_PATH = BASE_DIR / "config_all.json"
STATE_PATH = BASE_DIR / "state.json"
LOG_PATH = BASE_DIR / "monitor.log"

# Seen jobs remembered per target. Large enough that a job is never forgotten
# while it is still listed, so it can't be re-alerted after dropping off and
# coming back; small enough that state.json doesn't grow without bound.
STATE_RETENTION_PER_TARGET = 1000

SMTP_TIMEOUT_SECONDS = 30

# Reserved state.json keys (target keys are URLs, so they never start with "_").
PENDING_LEFT_OUT_KEY = "_left_out_pending"  # left-out postings not yet listed in an email
LAST_EMAIL_KEY = "_last_email_at"           # when an email last went out (ISO time)

# A state.json temp file older than this was left by a monitor killed mid-save.
STALE_TEMP_FILE_SECONDS = 600

_IS_WINDOWS = os.name == "nt"

# Read once at startup: os.umask() can only be read by briefly changing it.
_UMASK = os.umask(0)
os.umask(_UMASK)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default config (written on first run)
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "_instructions": (
        "Edit this file with your target career pages and email settings. "
        "Set mode to 'browser' for JavaScript-heavy pages (like Microsoft, Google, Meta). "
        "Set mode to 'html' for simple static pages. "
        "Set mode to 'api' and provide api_url for sites with known JSON APIs."
    ),
    "email": {
        "enabled": False,
        "smtp_server": "smtp.gmail.com",
        "smtp_port": 587,
        "sender_email": "you@gmail.com",
        "sender_password": "your-app-password",
        "recipient_email": "you@gmail.com",
    },
    "keyword_filters": [],
    "targets": [
        {
            "name": "Microsoft — US Remote Entry Level",
            "url": "https://apply.careers.microsoft.com/careers?start=0&location=United+States&sort_by=timestamp&filter_include_remote=1&filter_seniority=Entry%20Level",
            "mode": "browser",
            "wait_for": "a[href*='/careers/job/']",
            "link_selector": "a[href*='/careers/job/']",
        },
        {
            "name": "Example Static Site",
            "url": "https://example.com/careers",
            "mode": "html",
            "link_selector": "",
        },
    ],
}


# ===================================================================
# Core helpers
# ===================================================================
def load_json(path: Path, default=None):
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def _file_mode(path: Path) -> int:
    """Permissions to give a rewritten file: its current ones, or the umask default for a new file."""
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        return 0o666 & ~_UMASK


def _replace_file(src: str, dst: Path):
    for attempt in range(5):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            # On Windows the rename fails while another process (a second
            # monitor, antivirus) briefly has the file open, so retry.
            if not _IS_WINDOWS or attempt == 4:
                raise
            time.sleep(0.2 * (attempt + 1))


def save_json(path: Path, data):
    # Write a temp file and rename it into place, so a crash or a full disk
    # mid-write can never leave a truncated file behind. Resolve symlinks so
    # the link's target is updated rather than the link being replaced.
    path = Path(os.path.realpath(path))
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_path, _file_mode(path))  # mkstemp creates it as 0600
        _replace_file(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def ensure_config(config_override: str | None = None):
    override = config_override or os.environ.get("JOB_MONITOR_CONFIG", "").strip()
    if override:
        active_config_path = Path(override)
        if not active_config_path.is_absolute():
            active_config_path = BASE_DIR / active_config_path
        if not active_config_path.exists():
            if os.path.realpath(active_config_path) == os.path.realpath(CONFIG_PATH):
                # Asked for config.json and there isn't one yet: write the starter.
                save_json(CONFIG_PATH, DEFAULT_CONFIG)
                log.info(f"Created default config at {CONFIG_PATH}")
                log.info("Edit it with your targets and re-run.")
                sys.exit(0)
            # Any other missing path is an error. (A typo used to overwrite config.json.)
            log.error(f"Config file not found: {active_config_path}")
            sys.exit(2)
    else:
        active_config_path = CONFIG_ALL_PATH if CONFIG_ALL_PATH.exists() else CONFIG_PATH
        if not active_config_path.exists():
            save_json(CONFIG_PATH, DEFAULT_CONFIG)
            log.info(f"Created default config at {CONFIG_PATH}")
            log.info("Edit it with your targets and re-run.")
            sys.exit(0)
        if active_config_path == CONFIG_ALL_PATH and CONFIG_PATH.exists():
            log.warning(
                f"Using {CONFIG_ALL_PATH.name}; {CONFIG_PATH.name} is ignored. "
                f"To use it, run with --config {CONFIG_PATH.name}."
            )
    log.info(f"Using config file: {active_config_path.name}")
    return load_json(active_config_path)


def _read_state() -> dict | None:
    """state.json's contents: {} if it doesn't exist, None if it can't be parsed."""
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return state if isinstance(state, dict) else None


def load_state() -> dict:
    if not STATE_PATH.exists():
        log.warning(
            f"No {STATE_PATH.name} yet, so every target records a baseline this run. If you see "
            f"this on every run, {STATE_PATH.name} isn't being kept and nothing will ever be alerted."
        )
    state = _read_state()
    if state is None:
        # Only save_state() moves the bad file aside, while holding the lock,
        # so a reader can never move a good file another monitor just wrote.
        log.error(
            f"{STATE_PATH.name} is unreadable; treating it as empty, so every target records "
            f"a fresh baseline this run and postings since the last good run won't be alerted. "
            f"It will be set aside on the next save."
        )
        return {}
    return state


@contextmanager
def _state_lock():
    """Exclusive lock so monitors running at the same time don't interleave saves."""
    if fcntl is None:
        yield
        return
    lock_path = STATE_PATH.with_name(STATE_PATH.name + ".lock")
    lock_file = None
    try:
        lock_file = open(lock_path, "a")
        fcntl.flock(lock_file, fcntl.LOCK_EX)
    except OSError as exc:
        # E.g. Lustre without flock, NFS without lockd, or a lock file we can't open.
        log.warning(f"Could not lock {lock_path.name} ({exc}); saving without a lock.")
        if lock_file is not None:
            lock_file.close()
            lock_file = None
    try:
        yield
    finally:
        if lock_file is not None:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
            lock_file.close()


def _remove_stale_temp_files():
    """Delete temp files left by a monitor that was killed mid-save."""
    real_path = Path(os.path.realpath(STATE_PATH))
    for leftover in real_path.parent.glob(f"{real_path.name}.*.tmp"):
        try:
            if time.time() - leftover.stat().st_mtime > STALE_TEMP_FILE_SECONDS:
                leftover.unlink()
        except OSError:
            pass


def save_state(
    updates: dict,
    baselines: dict | None = None,
    pending_add: list[dict] | None = None,
    pending_shown: set[str] | None = None,
    last_email: str | None = None,
):
    """Write these targets' entries to state.json, keeping all other targets as they are on disk.

    Baselines are only written for targets still missing from the file: if another monitor
    recorded the target first, its earlier listing is kept, so nothing posted in between
    is silently absorbed.

    The pool of left-out postings waiting to be emailed is updated item by item (add new
    ones, drop the ones an email just listed), so monitors sharing the file don't lose
    each other's entries."""
    with _state_lock():
        _remove_stale_temp_files()
        state = _read_state()
        if state is None:
            # Don't let one bad file break every future run: keep it for inspection and start over.
            backup = STATE_PATH.with_name(f"{STATE_PATH.name}.corrupt-{datetime.now():%Y%m%d-%H%M%S-%f}")
            try:
                STATE_PATH.replace(backup)
                log.error(f"Moved unreadable {STATE_PATH.name} to {backup.name}.")
            except OSError as exc:
                log.error(f"Could not move unreadable {STATE_PATH.name} aside ({exc}); overwriting it.")
            state = {}
        state.update(updates)
        for url, jobs in (baselines or {}).items():
            state.setdefault(url, jobs)
        if pending_add or pending_shown:
            shown = pending_shown or set()
            pool = [p for p in state.get(PENDING_LEFT_OUT_KEY) or [] if compute_job_id(p) not in shown]
            in_pool = {compute_job_id(p) for p in pool}
            for item in pending_add or []:
                if compute_job_id(item) not in in_pool:
                    pool.append(item)
                    in_pool.add(compute_job_id(item))
            if pool:
                state[PENDING_LEFT_OUT_KEY] = pool
            else:
                state.pop(PENDING_LEFT_OUT_KEY, None)
        if last_email:
            state[LAST_EMAIL_KEY] = last_email
        save_json(STATE_PATH, state)


# ===================================================================
# Fetching — static HTML
# ===================================================================
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}


def fetch_html(url: str, timeout: int = 30) -> str | None:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout)
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as exc:
        log.error(f"  [html] Failed to fetch {url}: {exc}")
        return None


def fetch_microsoft_jobs_via_api(careers_url: str, timeout: int = 30) -> list[dict] | None:
    """
    Fetch Microsoft jobs directly from their PCSX search API.
    Returns None on request/parsing failure, or a list (possibly empty) on success.
    """
    parsed = urlparse(careers_url)
    if "apply.careers.microsoft.com" not in parsed.netloc.lower() or parsed.path != "/careers":
        return None

    raw_qs = parse_qs(parsed.query)
    params: dict[str, str] = {
        "domain": "microsoft.com",
        "query": raw_qs.get("query", [""])[0],
        "start": raw_qs.get("start", ["0"])[0],
        "sort_by": raw_qs.get("sort_by", ["timestamp"])[0],
    }

    if raw_qs.get("location"):
        params["location"] = raw_qs["location"][0]

    # Carry all filter_* values through from the careers URL.
    for key, values in raw_qs.items():
        if key.startswith("filter_") and values:
            params[key] = values[0]

    # Current Microsoft API expects "Entry" rather than "Entry Level".
    if params.get("filter_seniority", "").strip().lower() == "entry level":
        params["filter_seniority"] = "Entry"

    api_url = "https://apply.careers.microsoft.com/api/pcsx/search"
    try:
        resp = requests.get(api_url, params=params, headers=HEADERS, timeout=timeout)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        log.warning(f"  [ms-api] Failed to query Microsoft API: {exc}")
        return None

    positions = payload.get("data", {}).get("positions", [])
    if not isinstance(positions, list):
        return None

    jobs: list[dict] = []
    seen: set[str] = set()
    for pos in positions:
        if not isinstance(pos, dict):
            continue
        title = _to_text(pos.get("name"))
        purl = _to_text(pos.get("positionUrl"))
        jid = _to_text(pos.get("displayJobId") or pos.get("id") or pos.get("atsJobId"))
        full_url = _normalize_job_url(careers_url, purl, jid)
        if not title or not full_url or full_url in seen:
            continue
        seen.add(full_url)
        jobs.append({"title": title[:200], "url": full_url})

    return jobs


def _default_eightfold_domain_from_host(host: str) -> str:
    host_lc = (host or "").lower()
    if host_lc == "apply.careers.microsoft.com":
        return "microsoft.com"
    if host_lc.endswith(".eightfold.ai"):
        return host_lc.split(".", 1)[0] + ".com"
    return ""


def fetch_eightfold_jobs_via_api(careers_url: str, timeout: int = 30) -> list[dict] | None:
    """
    Fetch jobs from Eightfold-hosted career sites via /api/pcsx/search.
    Supports microsoft apply portal and *.eightfold.ai tenants.
    """
    parsed = urlparse(careers_url)
    host = parsed.netloc.lower()
    if host != "apply.careers.microsoft.com" and not host.endswith(".eightfold.ai"):
        return None

    raw_qs = parse_qs(parsed.query)
    params: dict[str, str] = {
        "domain": raw_qs.get("domain", [_default_eightfold_domain_from_host(host)])[0],
        "query": raw_qs.get("query", [""])[0],
        "start": raw_qs.get("start", ["0"])[0],
        "sort_by": raw_qs.get("sort_by", ["timestamp"])[0],
    }

    for key, values in raw_qs.items():
        if not values:
            continue
        if key.startswith("filter_") or key in {
            "location",
            "location_country",
            "location_city",
            "distance",
            "hl",
            "lang",
            "sort_by",
            "query",
            "start",
            "domain",
        }:
            params[key] = values[0]

    if params.get("filter_seniority", "").strip().lower() == "entry level":
        params["filter_seniority"] = "Entry"

    api_url = f"{parsed.scheme or 'https'}://{host}/api/pcsx/search"
    try:
        resp = requests.get(api_url, params=params, headers=HEADERS, timeout=timeout)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        log.warning(f"  [eightfold-api] Failed to query Eightfold API: {exc}")
        return None

    data = payload.get("data", {}) if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return None
    positions = data.get("positions", [])
    if not isinstance(positions, list):
        return None
    first_page_len = len(positions)
    positions = [p for p in positions if isinstance(p, dict)]
    if params["sort_by"] == "timestamp" and _posted_dates_only(positions):
        positions = _eightfold_newest_days(api_url, params, positions, first_page_len, data.get("count"), timeout)

    jobs: list[dict] = []
    seen: set[str] = set()
    for pos in positions:
        title = _to_text(pos.get("name"))
        purl = _to_text(pos.get("positionUrl"))
        jid = _to_text(pos.get("displayJobId") or pos.get("id") or pos.get("atsJobId"))
        full_url = _normalize_job_url(careers_url, purl, jid)
        if not title or not full_url or full_url in seen:
            continue
        seen.add(full_url)
        jobs.append({"title": title[:200], "url": full_url})

    return jobs


EIGHTFOLD_MAX_POSITIONS = 100


def _posted_dates_only(positions: list[dict]) -> bool:
    """True when postedTs holds only a date (midnight UTC), as on Morgan Stanley's and PayPal's sites."""
    stamps = [p.get("postedTs") for p in positions]
    return bool(stamps) and all(isinstance(t, int) and t > 0 and t % 86400 == 0 for t in stamps)


def _eightfold_newest_days(api_url: str, params: dict, positions: list[dict], first_page_len: int,
                           total, timeout: int) -> list[dict]:
    """Sorted by "Latest", postings of one day tie when the site stores only the date, and
    Eightfold returns tied postings in an order that changes from minute to minute, at most
    10 per page, so the first page is a different slice each time. Read on until the two
    newest posting days are complete (a third day has begun, or the list ended) and return
    those two days in a fixed order: newest day first, then by id."""
    def days():
        return sorted({p.get("postedTs") or 0 for p in positions}, reverse=True)

    total = int(total) if str(total).isdigit() else 0
    start = str(params.get("start") or 0)
    offset = (int(start) if start.isdigit() else 0) + first_page_len
    while len(days()) < 3 and len(positions) < EIGHTFOLD_MAX_POSITIONS and offset < total:
        try:
            resp = requests.get(api_url, params={**params, "start": str(offset)}, headers=HEADERS, timeout=timeout)
            resp.raise_for_status()
            page = resp.json()["data"]["positions"]
        except Exception as exc:
            log.warning(f"  [eightfold-api] Failed to read more of the newest postings: {exc}")
            break
        if not isinstance(page, list) or not page:
            break
        offset += len(page)
        positions = positions + [p for p in page if isinstance(p, dict)]
    newest = days()
    if len(newest) >= 3:  # the third day is only partly read, and which part varies
        positions = [p for p in positions if (p.get("postedTs") or 0) >= newest[1]]
    return sorted(positions, key=lambda p: (p.get("postedTs") or 0, _to_text(p.get("id")).zfill(20)), reverse=True)


def _looks_like_locale(segment: str) -> bool:
    return bool(re.fullmatch(r"[a-z]{2}-[A-Z]{2}", segment or ""))


def fetch_workday_jobs_via_api(careers_url: str, timeout: int = 30) -> list[dict] | None:
    """
    Fetch jobs for Workday-hosted career sites (myworkdayjobs.com).
    Returns None if URL is not Workday or if request/parsing fails.
    """
    parsed = urlparse(careers_url)
    host = parsed.netloc.lower()
    match = re.match(r"^([^.]+)\.wd\d+\.myworkdayjobs\.com$", host)
    if not match:
        return None

    tenant = match.group(1)
    segments = [s for s in parsed.path.split("/") if s]
    if not segments:
        return None

    if _looks_like_locale(segments[0]):
        locale = segments[0]
        site = segments[1] if len(segments) > 1 else ""
    else:
        locale = "en-US"
        site = segments[0]

    if not site:
        return None

    api_url = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    raw_qs = parse_qs(parsed.query)
    payload: dict = {
        "limit": 20,
        "offset": 0,
        "searchText": raw_qs.get("q", [""])[0],
    }

    # Map URL filters into Workday facets.
    # Example: locationHierarchy1=...&workerSubType=...
    applied_facets: dict[str, list[str]] = {}
    skip_params = {"redirect", "q", "start", "offset", "limit", "sort", "sort_by", "sortBy"}
    for key, values in raw_qs.items():
        if key in skip_params or not values:
            continue
        applied_facets[key] = [v for v in values if v]
    if applied_facets:
        payload["appliedFacets"] = applied_facets

    try:
        resp = requests.post(api_url, json=payload, headers=HEADERS, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.warning(f"  [wd-api] Failed to query Workday API: {exc}")
        return None

    postings = data.get("jobPostings", [])
    if not isinstance(postings, list):
        return None

    jobs: list[dict] = []
    seen: set[str] = set()
    prefix = f"https://{host}/{locale}/{site}"
    for post in postings:
        if not isinstance(post, dict):
            continue
        title = _to_text(post.get("title"))
        external_path = _to_text(post.get("externalPath"))
        if not title or not external_path:
            continue
        full_url = urljoin(prefix + "/", external_path.lstrip("/"))
        if full_url in seen:
            continue
        seen.add(full_url)
        jobs.append({"title": title[:200], "url": full_url})

    return jobs


def fetch_phenom_jobs_from_page(careers_url: str, timeout: int = 30) -> list[dict] | None:
    """
    Extract jobs from Phenom-hosted pages that embed eagerLoadRefineSearch
    in the `phApp.ddo` JSON object.
    """
    try:
        resp = requests.get(careers_url, headers=HEADERS, timeout=timeout)
        resp.raise_for_status()
        html = resp.text
    except Exception:
        return None

    match = re.search(
        r"var\s+phApp\s*=\s*phApp\s*\|\|\s*(\{.*?\});\s*phApp\.ddo\s*=\s*(\{.*?\});",
        html,
        re.S,
    )
    if not match:
        return None

    try:
        ddo = json.loads(match.group(2))
    except Exception:
        return None

    raw_jobs = ddo.get("eagerLoadRefineSearch", {}).get("data", {}).get("jobs", [])
    if not isinstance(raw_jobs, list):
        return None

    jobs: list[dict] = []
    seen: set[str] = set()
    for item in raw_jobs:
        if not isinstance(item, dict):
            continue
        title = _to_text(item.get("title") or item.get("jobTitle") or item.get("name"))
        url = _to_text(item.get("jobUrl") or item.get("applyUrl") or item.get("url"))
        if not title or not url:
            continue
        # Prefer job detail link over direct apply endpoint when possible.
        if url.endswith("/apply"):
            url = url[:-6]
        full_url = _normalize_job_url(careers_url, url, "")
        if not full_url or full_url in seen:
            continue
        seen.add(full_url)
        jobs.append({"title": title[:200], "url": full_url})

    return jobs


GOOGLE_JOB_PATH_RE = re.compile(
    r"^/about/careers/applications/jobs/results/\d{6,}-[a-z0-9-]+$",
    re.IGNORECASE,
)


def _looks_like_google_job_url(full_url: str) -> bool:
    parsed = urlparse(full_url)
    if "google.com" not in parsed.netloc.lower():
        return False
    return bool(GOOGLE_JOB_PATH_RE.match(parsed.path))


def _normalize_google_job_href(careers_url: str, href: str) -> str:
    raw = (href or "").strip()
    if not raw:
        return ""
    if raw.startswith(("http://", "https://")):
        return raw

    cleaned = raw.lstrip("./")
    if cleaned.startswith("jobs/results/"):
        return urljoin(careers_url, f"/about/careers/applications/{cleaned}")
    return urljoin(careers_url, raw)


def fetch_google_jobs_from_page(careers_url: str, timeout: int = 30) -> list[dict] | None:
    """
    Extract Google job links from the careers search results page HTML.
    Returns None when URL is not a supported Google careers results page.
    """
    parsed = urlparse(careers_url)
    host = parsed.netloc.lower()
    if "google.com" not in host or "/about/careers/applications/jobs/results" not in parsed.path:
        return None

    html = fetch_html(careers_url, timeout=timeout)
    if not html:
        return None

    soup = BeautifulSoup(html, "lxml")
    jobs: list[dict] = []
    seen: set[str] = set()

    for a_tag in soup.find_all("a", href=True):
        href = (a_tag.get("href") or "").strip()
        if not href:
            continue

        full_url = _normalize_google_job_href(careers_url, href)
        if not _looks_like_google_job_url(full_url):
            continue
        if full_url in seen:
            continue

        aria = _to_text(a_tag.get("aria-label"))
        title = ""
        if aria.lower().startswith("learn more about "):
            title = aria[len("learn more about ") :].strip()
        elif aria:
            title = aria

        if not title:
            text = a_tag.get_text(" ", strip=True)
            if text and text.lower() != "learn more":
                title = text

        if not title:
            slug_match = re.search(r"/results/\d{6,}-([a-z0-9-]+)$", urlparse(full_url).path, re.IGNORECASE)
            if slug_match:
                title = slug_match.group(1).replace("-", " ").strip().title()

        if not title:
            continue

        seen.add(full_url)
        jobs.append({"title": title[:200], "url": full_url})

    return jobs


# ===================================================================
# Fetching — job-board APIs (Greenhouse, Ashby, Lever, Oracle HCM)
# ===================================================================
# A target whose URL is one of these public job-board APIs is read with a
# dedicated reader: clean titles, location data for the US filter, and no
# browser. Readers return None when the request fails; the target is then
# skipped for this run (no fallback that could return differently-shaped
# URLs and re-alert old jobs).
def _get_json(url: str, params=None, timeout: int = 60):
    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        log.warning(f"  [api] Failed to fetch {url}: {exc}")
        return None


def _query_values(query: dict, *names: str) -> list[str]:
    return [v for name in names for v in query.get(name, []) if v]


def fetch_greenhouse_api_jobs(url: str) -> list[dict] | None:
    """https://boards-api.greenhouse.io/v1/boards/<board>/jobs[?departments[]=<id or name>&offices[]=...]"""
    parsed = urlparse(url)
    match = re.match(r"^/v1/boards/([^/]+)/jobs/?$", parsed.path)
    if not match:
        log.warning(f"  [greenhouse] Unrecognised board URL: {url}")
        return None
    query = parse_qs(parsed.query)
    want_departments = {v.lower() for v in _query_values(query, "departments[]", "departments", "department")}
    want_offices = {v.lower() for v in _query_values(query, "offices[]", "offices", "office")}
    params = {"content": "true"} if (want_departments or want_offices) else None
    data = _get_json(f"https://boards-api.greenhouse.io/v1/boards/{match.group(1)}/jobs", params)
    postings = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(postings, list):
        return None

    def matches(post, key, wanted):
        if not wanted:
            return True
        return any(str(d.get("id")).lower() in wanted or str(d.get("name", "")).lower() in wanted
                   for d in post.get(key) or [] if isinstance(d, dict))

    jobs = []
    for post in postings:
        if not isinstance(post, dict) or not post.get("title") or not post.get("absolute_url"):
            continue
        if not matches(post, "departments", want_departments) or not matches(post, "offices", want_offices):
            continue
        jobs.append({
            "title": _to_text(post["title"])[:200],
            "url": _to_text(post["absolute_url"]),
            "location": _to_text((post.get("location") or {}).get("name")),
        })
    return _dedupe_jobs(jobs)


def fetch_ashby_api_jobs(url: str) -> list[dict] | None:
    """https://api.ashbyhq.com/posting-api/job-board/<board>[?department=<name>&team=<name>]"""
    parsed = urlparse(url)
    match = re.match(r"^/posting-api/job-board/([^/]+)/?$", parsed.path)
    if not match:
        log.warning(f"  [ashby] Unrecognised board URL: {url}")
        return None
    query = parse_qs(parsed.query)
    want_departments = {v.lower() for v in _query_values(query, "department")}
    want_teams = {v.lower() for v in _query_values(query, "team")}
    data = _get_json(f"https://api.ashbyhq.com/posting-api/job-board/{match.group(1)}")
    postings = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(postings, list):
        return None
    jobs = []
    for post in postings:
        if not isinstance(post, dict) or not post.get("title") or not post.get("jobUrl"):
            continue
        if post.get("isListed") is False:
            continue
        if want_departments and str(post.get("department", "")).lower() not in want_departments:
            continue
        if want_teams and str(post.get("team", "")).lower() not in want_teams:
            continue
        places = [post.get("location")] + [
            loc.get("location") for loc in post.get("secondaryLocations") or [] if isinstance(loc, dict)
        ]
        countries = [
            _to_text(((loc.get("address") or {}).get("postalAddress") or {}).get("addressCountry"))
            for loc in [post] + [l for l in post.get("secondaryLocations") or [] if isinstance(l, dict)]
        ]
        jobs.append({
            "title": _to_text(post["title"])[:200],
            "url": _to_text(post["jobUrl"]),
            "location": "; ".join(_to_text(p) for p in places if _to_text(p)),
            # Only trust countries when every location has one; otherwise the text decides.
            "countries": countries if all(countries) else [],
        })
    return _dedupe_jobs(jobs)


def fetch_lever_api_jobs(url: str) -> list[dict] | None:
    """https://api.lever.co/v0/postings/<company>[?location=...&department=...&team=...&commitment=...]"""
    parsed = urlparse(url)
    match = re.match(r"^/v0/postings/([^/]+)/?$", parsed.path)
    if not match:
        log.warning(f"  [lever] Unrecognised postings URL: {url}")
        return None
    params = [(k, v) for k, vs in parse_qs(parsed.query).items() if k != "mode" for v in vs]
    params.append(("mode", "json"))
    data = _get_json(f"https://api.lever.co/v0/postings/{match.group(1)}", params)
    if not isinstance(data, list):
        return None
    jobs = []
    for post in data:
        if not isinstance(post, dict) or not post.get("text") or not post.get("hostedUrl"):
            continue
        categories = post.get("categories") or {}
        places = categories.get("allLocations") or [categories.get("location")]
        jobs.append({
            "title": _to_text(post["text"])[:200],
            "url": _to_text(post["hostedUrl"]),
            "location": "; ".join(_to_text(p) for p in places if _to_text(p)),
            "countries": [_to_text(post.get("country"))] if _to_text(post.get("country")) else [],
        })
    return _dedupe_jobs(jobs)


def fetch_oracle_hcm_jobs(url: str) -> list[dict] | None:
    """https://<host>.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitions?...finder=findReqs;siteNumber=<site>,..."""
    parsed = urlparse(url)
    site = re.search(r"siteNumber=([^,;&]+)", parsed.query)
    if not site:
        log.warning(f"  [oracle] No siteNumber in the finder of {url}")
        return None
    data = _get_json(url)
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return None
    jobs = []
    for item in items:
        for req in (item.get("requisitionList") or []) if isinstance(item, dict) else []:
            if not isinstance(req, dict) or not req.get("Title") or not req.get("Id"):
                continue
            jobs.append({
                "title": _to_text(req["Title"])[:200],
                "url": f"https://{parsed.netloc}/hcmUI/CandidateExperience/en/sites/{site.group(1)}/job/{_to_text(req['Id'])}",
                "location": _to_text(req.get("PrimaryLocation")),
                "countries": [_to_text(req.get("PrimaryLocationCountry"))] if req.get("PrimaryLocationCountry") else [],
            })
    return _dedupe_jobs(jobs)


_TESLA_STATE_PATH = "/cua-api/apps/careers/state"
_TESLA_HOSTS = ("www.tesla.com", "tesla.com", "www.tesla.cn")


def is_tesla_careers_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.netloc.lower() in _TESLA_HOSTS and (
        parsed.path.rstrip("/") in ("/careers/search", _TESLA_STATE_PATH)
    )


def fetch_tesla_jobs(url: str) -> list[dict] | None:
    """Every Tesla listing comes from one JSON document behind the careers search page
    (https://www.tesla.com/careers/search/?country=US). The page's country (or site) and
    type parameters are applied here. Tesla's bot protection blocks some networks; that is
    logged and the caller falls back to the page itself."""
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    want_site = (_query_values(query, "site", "country") or [""])[0].upper()  # none: every country
    want_types = {t.lower() for t in _query_values(query, "type")}
    ignored = sorted(set(query) - {"site", "country", "type", "region", "sort"})
    if ignored:
        log.info(f"  [tesla] Only country and type are applied; ignoring {', '.join(ignored)}")
    try:
        resp = requests.get(
            f"https://{parsed.netloc}{_TESLA_STATE_PATH}",
            headers={**HEADERS, "Accept": "application/json", "Referer": f"https://{parsed.netloc}/careers/search/"},
            timeout=60,
        )
        if resp.status_code != 200 or "json" not in resp.headers.get("content-type", ""):
            log.warning(
                f"  [tesla] Tesla's jobs data refused the request (HTTP {resp.status_code}); its bot "
                f"protection blocks some networks."
            )
            return None
        state = resp.json()
    except Exception as exc:
        log.warning(f"  [tesla] Failed to fetch {url}: {exc}")
        return None
    if not isinstance(state, dict) or not isinstance(state.get("listings"), list) or not state["listings"]:
        log.warning("  [tesla] Tesla's jobs data has no listings")  # it always has thousands
        return None
    try:
        return _tesla_jobs_from_state(state, parsed.netloc, want_site, want_types)
    except Exception as exc:
        log.warning(f"  [tesla] Unexpected layout in Tesla's jobs data: {exc!r}")
        return None


def _tesla_jobs_from_state(state: dict, host: str, want_site: str, want_types: set[str]) -> list[dict]:
    def leaf_ids(node, out):
        if isinstance(node, dict):
            for key, value in node.items():
                if key not in ("id", "name"):
                    leaf_ids(value, out)
        elif isinstance(node, list):
            for value in node:
                if isinstance(value, (str, int)):
                    out.add(str(value))
                else:
                    leaf_ids(value, out)

    site_of_location = {}
    for region in state.get("geo") or []:
        for site in (region.get("sites") or []) if isinstance(region, dict) else []:
            ids = set()
            leaf_ids(site, ids)
            for loc_id in ids:
                site_of_location[loc_id] = site.get("id")
    lookup = state.get("lookup") or {}
    locations, types = lookup.get("locations") or {}, lookup.get("types") or {}

    jobs = []
    for row in state["listings"]:
        if not isinstance(row, dict):
            continue
        job_id, title = _to_text(row.get("id")), _to_text(row.get("t"))
        site = site_of_location.get(_to_text(row.get("l")))
        if not job_id or not title or (want_site and site and site != want_site):
            continue  # a location missing from geo is kept; its text decides (us_only)
        type_id = _to_text(row.get("y"))
        if want_types and not want_types & {type_id, str(types.get(type_id, "")).lower()}:
            continue
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")  # as in Tesla's own job links
        jobs.append({
            "title": title[:200],
            "url": f"https://{host}/careers/search/job/{slug + '-' if slug else ''}{job_id}",
            "location": _to_text(locations.get(_to_text(row.get("l")))),
            "countries": [site] if site else [],
        })
    return _dedupe_jobs(jobs)


# Sitemap mode: some careers sites (e.g. Citadel) block everything except their
# sitemap. Each job page URL becomes a job; its title and region come from the slug.
_SLUG_WORDS = {
    "c": "C++", "ai": "AI", "ml": "ML", "phd": "PhD", "hpc": "HPC", "sre": "SRE", "ui": "UI", "ux": "UX",
    "api": "API", "gpu": "GPU", "fpga": "FPGA", "asic": "ASIC", "it": "IT", "qa": "QA", "llm": "LLM",
    "etf": "ETF", "fx": "FX", "cto": "CTO", "us": "US", "uk": "UK", "emea": "EMEA", "apac": "APAC",
    "hr": "HR", "bs": "BS", "ms": "MS", "sdet": "SDET", "dmm": "DMM", "cpp": "C++", "ios": "iOS",
}
_SLUG_REGIONS = {
    "europe": "Europe", "asia": "Asia", "emea": "EMEA", "apac": "APAC", "australia": "Australia",
    "uk": "UK", "london": "London", "singapore": "Singapore", "canada": "Canada", "india": "India",
    "hong-kong": "Hong Kong", "dublin": "Dublin", "paris": "Paris", "sydney": "Sydney", "tokyo": "Tokyo",
}


def _job_from_slug_url(url: str) -> dict:
    """Title and region from a job page's slug, e.g. .../quantitative-trader-intern-us-new-york/."""
    tokens = [t for t in urlparse(url).path.rstrip("/").rsplit("/", 1)[-1].lower().split("-") if t]
    if len(tokens) > 1 and re.fullmatch(r"\d", tokens[-1]):
        tokens = tokens[:-1]  # WordPress suffix for duplicate slugs ("...-engineer-2")
    location, countries = "", []
    for i in range(len(tokens) - 1, max(len(tokens) - 5, 0), -1):  # "...-us" or "...-us-new-york"
        city = " ".join(tokens[i + 1:])
        if tokens[i] == "us" and (not city or city in _US_CITIES | {"new york"}):
            location, countries, tokens = (f"{city.title()}, US" if city else "US"), ["US"], tokens[:i]
            break
    else:
        n = next((n for n in (2, 1) if len(tokens) > n and "-".join(tokens[-n:]) in _SLUG_REGIONS), 0)
        if n:
            location, tokens = _SLUG_REGIONS["-".join(tokens[-n:])], tokens[:-n]
            # "...-new-york-london" or "...-us-europe": also in the US, so keep the US part
            # in the location (a location naming any US place is never skipped).
            for k in (3, 2, 1):
                place = " ".join(tokens[-k:])
                if len(tokens) > k and (place == "us" or place in _US_CITIES | {"new york"}):
                    location, tokens = f"{'US' if place == 'us' else place.title()} / {location}", tokens[:-k]
                    break
    title = " ".join(_SLUG_WORDS.get(t, t.capitalize()) for t in tokens if t) or url
    return {"title": title[:200], "url": url, "location": location, "countries": countries}


def fetch_sitemap_jobs(url: str, url_contains: str) -> list[dict] | None:
    """Jobs from a sitemap: every <loc> whose URL contains url_contains (the target's link_selector)."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except Exception as exc:
        log.warning(f"  [sitemap] Failed to read {url}: {exc}")
        return None
    if root.tag.rsplit("}", 1)[-1] != "urlset":
        # e.g. a sitemap index (a list of sitemaps) or a block page that happens to be XML.
        log.warning(f"  [sitemap] {url} is not a sitemap of pages (<{root.tag.rsplit('}', 1)[-1]}>)")
        return None
    locs = [el.text.strip() for el in root.iter() if el.tag.rsplit("}", 1)[-1] == "loc" and el.text]
    return _dedupe_jobs([_job_from_slug_url(u) for u in locs if not url_contains or url_contains in u])


def fetch_talentbrew_jobs(url: str) -> list[dict] | None:
    """TalentBrew (Radancy) careers sites such as jobs.intuit.com. The search page sorts by
    relevance and ignores sort parameters, but the results endpoint it calls,
    <site>/search-jobs/results?...&SearchResultsModuleName=Search+Results&SortCriteria=1&SortDirection=1,
    sorts by date posted, newest first. It answers JSON whose "results" is the list's HTML."""
    data = _get_json(url)
    if not isinstance(data, dict) or not isinstance(data.get("results"), str):
        return None
    soup = BeautifulSoup(data["results"], "lxml")
    if soup.select_one("#search-results") is None:
        log.warning("  [talentbrew] No result list in the response (is SearchResultsModuleName in the URL?)")
        return None
    jobs = []
    for link in soup.select("#search-results-list a[href*='/job/']"):
        heading = link.find(["h2", "h3"])
        location = link.select_one(".job-location")
        jobs.append({
            "title": (heading or link).get_text(" ", strip=True)[:200],
            "url": urljoin(url, link["href"]),
            "location": location.get_text(" ", strip=True) if location else "",
        })
    return _dedupe_jobs(jobs)


def job_board_api_reader(url: str):
    """(tag, reader) when the URL is a job-board API with a dedicated reader, else None."""
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host == "boards-api.greenhouse.io":
        return "greenhouse", fetch_greenhouse_api_jobs
    if host == "api.ashbyhq.com":
        return "ashby", fetch_ashby_api_jobs
    if host in ("api.lever.co", "api.eu.lever.co"):
        return "lever", fetch_lever_api_jobs
    if host.endswith(".oraclecloud.com") and "/hcmRestApi/resources/" in parsed.path:
        return "oracle", fetch_oracle_hcm_jobs
    if parsed.path.rstrip("/").lower() == "/search-jobs/results":
        return "talentbrew", fetch_talentbrew_jobs
    return None


# ===================================================================
# US-only filter (for jobs whose location is known)
# ===================================================================
_US_STATES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut", "delaware",
    "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky",
    "louisiana", "maine", "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey", "new mexico",
    "new york", "north carolina", "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania",
    "rhode island", "south carolina", "south dakota", "tennessee", "texas", "utah", "vermont",
    "virginia", "washington", "west virginia", "wisconsin", "wyoming", "district of columbia",
}
_US_STATE_CODES = (
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY "
    "NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC"
).split()
_US_CITIES = {
    "san francisco", "seattle", "austin", "boston", "chicago", "los angeles", "palo alto",
    "mountain view", "sunnyvale", "menlo park", "redmond", "bellevue", "denver", "atlanta", "miami",
    "pittsburgh", "philadelphia", "dallas", "houston", "san jose", "san diego", "salt lake city",
    "raleigh", "durham", "charlotte", "nashville", "portland", "phoenix", "detroit", "minneapolis",
    "santa clara", "san mateo", "cupertino", "irvine", "brooklyn", "manhattan", "jersey city",
    "arlington", "reston", "herndon", "washington, d.c.", "washington d.c.", "nyc", "sf bay area",
    "bay area", "silicon valley", "memphis", "frisco", "plano", "columbus", "boulder", "ann arbor",
}
_US_WORDS = re.compile(
    r"\bunited states\b|\bu\.s\.a?\.?(?!\w)|\busa\b|\bamericas?\b|"
    r"\b(?:" + "|".join(re.escape(c) for c in sorted(_US_STATES | _US_CITIES, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)
_US_CODE = re.compile(r"(?<![A-Za-z])(?:US|" + "|".join(_US_STATE_CODES) + r")(?![A-Za-z])")
_NON_US_WORDS = re.compile(
    r"\b(?:" + "|".join(sorted([
        "canada", "toronto", "vancouver", "montreal", "ontario", "british columbia", "quebec",
        "united kingdom", "uk", "england", "scotland", "london", "manchester", "edinburgh", "ireland",
        "dublin", "germany", "berlin", "munich", "hamburg", "frankfurt", "france", "paris",
        "netherlands", "amsterdam", "spain", "madrid", "barcelona", "portugal", "lisbon", "italy",
        "milan", "poland", "warsaw", "krakow", "romania", "bucharest", "switzerland", "zurich",
        "geneva", "sweden", "stockholm", "denmark", "copenhagen", "norway", "oslo", "finland",
        "helsinki", "israel", "tel aviv", "india", "bangalore", "bengaluru", "hyderabad", "pune",
        "mumbai", "delhi", "gurgaon", "gurugram", "chennai", "noida", "singapore", "japan", "tokyo",
        "korea", "seoul", "china", "shanghai", "beijing", "shenzhen", "hong kong", "taiwan", "taipei",
        "australia", "sydney", "melbourne", "brazil", "são paulo", "sao paulo", "mexico", "mexico city",
        "argentina", "buenos aires", "chile", "colombia", "bogota", "uae", "dubai", "abu dhabi",
        "philippines", "manila", "vietnam", "malaysia", "kuala lumpur", "indonesia", "jakarta",
        "thailand", "bangkok", "south africa", "nigeria", "lagos", "kenya", "nairobi", "egypt",
        "turkey", "istanbul", "greece", "athens", "czech", "prague", "hungary", "budapest", "austria",
        "vienna", "belgium", "brussels", "luxembourg", "estonia", "tallinn", "lithuania", "latvia",
        "ukraine", "kyiv", "serbia", "belgrade", "bulgaria", "sofia", "croatia", "slovenia",
        "slovakia", "cyprus", "malta", "new zealand", "auckland", "saudi arabia", "riyadh", "qatar",
        "doha", "bahrain", "kuwait", "pakistan", "bangladesh", "sri lanka", "emea", "apac", "latam",
        "europe", "asia", "costa rica", "peru", "uruguay", "guatemala",
    ], key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)
_US_COUNTRY_NAMES = {"us", "usa", "united states", "united states of america"}


def is_outside_us(job: dict) -> bool:
    """True only when the job's location data clearly places it outside the US.
    Unknown, empty or ambiguous locations ("Remote") count as possibly US."""
    countries = [c.strip().lower() for c in job.get("countries") or [] if str(c).strip()]
    if countries:
        return not any(c in _US_COUNTRY_NAMES for c in countries)
    location = str(job.get("location") or "").strip()
    if not location:
        return False
    parts = [p.strip() for p in re.split(r";|\||\bor\b|/", location, flags=re.IGNORECASE) if p.strip()]
    for part in parts:
        if _US_WORDS.search(part) or _US_CODE.search(part) or not _NON_US_WORDS.search(part):
            return False  # this part is (or may be) in the US
    return True


# ===================================================================
# Fetching — browser (Playwright) for JS-rendered pages
# ===================================================================
def fetch_browser(url: str, wait_for: str = "", wait_seconds: int = 8) -> tuple[str | None, list[str]]:
    """
    Load a page in a headless Chromium browser, wait for JS to render,
    and return the fully-rendered HTML plus captured JSON payloads.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.error(
            "  [browser] Playwright not installed. Run:\n"
            "    pip install playwright && playwright install chromium"
        )
        return None, []

    payloads: list[str] = []

    def capture_response(response):
        # Some careers sites load jobs from JSON APIs instead of anchor tags.
        if len(payloads) >= 80:
            return
        try:
            ctype = (response.headers or {}).get("content-type", "").lower()
            url_lc = response.url.lower()
            likely_json = "json" in ctype or "/api/" in url_lc or "careerhub" in url_lc
            if not likely_json:
                return
            body = response.text()
            if not body:
                return
            body_lc = body.lower()
            if any(token in body_lc for token in ("position", "job", "hiring_title", "display_job_id", "requisition")):
                payloads.append(body)
        except Exception:
            return

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=HEADERS["User-Agent"],
                viewport={"width": 1280, "height": 900},
                # Pin the locale: with LANG unset Chromium reports "en-US@posix",
                # which some careers sites (Oracle HCM, e.g. JPMorgan) choke on.
                locale="en-US",
            )
            page = context.new_page()
            page.on("response", capture_response)

            log.info(f"  [browser] Loading page...")
            page.goto(url, wait_until="domcontentloaded", timeout=60000)

            # Wait for job listing elements to appear
            if wait_for:
                try:
                    log.info(f"  [browser] Waiting for selector: {wait_for}")
                    page.wait_for_selector(wait_for, timeout=15000)
                except Exception:
                    log.warning(f"  [browser] Selector '{wait_for}' not found within timeout, continuing anyway")

            # Extra settle time for lazy-loaded content
            time.sleep(wait_seconds)

            # Scroll down to trigger any lazy loading
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(2)

            html = page.content()
            browser.close()
            return html, payloads
    except Exception as exc:
        log.error(f"  [browser] Failed: {exc}")
        return None, []


def _dict_get_any(d: dict, keys: list[str]):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def _to_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float)):
        return str(value).strip()
    return ""


def _normalize_job_url(base_url: str, raw_url: str, raw_id: str) -> str:
    url = _to_text(raw_url)
    if url:
        if url.startswith(("http://", "https://")):
            return url
        return urljoin(base_url, url)

    rid = _to_text(raw_id)
    if rid:
        # Best-effort route for Microsoft/Eightfold style position detail pages.
        return urljoin(base_url, f"/careers/jobs/{rid}")
    return ""


def _extract_jobs_from_object(node, base_url: str, out: list[dict], seen: set[str]):
    if isinstance(node, dict):
        title = _to_text(
            _dict_get_any(
                node,
                [
                    "title",
                    "jobTitle",
                    "job_title",
                    "hiring_title",
                    "position_title",
                    "positionTitle",
                    "name",
                ],
            )
        )
        raw_url = _to_text(
            _dict_get_any(
                node,
                [
                    "url",
                    "jobUrl",
                    "job_url",
                    "applyUrl",
                    "apply_url",
                    "positionUrl",
                    "position_url",
                    "absolute_url",
                    "canonical_url",
                    "detail_url",
                ],
            )
        )
        raw_id = _to_text(
            _dict_get_any(
                node,
                [
                    "id",
                    "jobId",
                    "job_id",
                    "positionId",
                    "position_id",
                    "display_job_id",
                    "requisitionId",
                    "requisition_id",
                    "pid",
                    "uuid",
                ],
            )
        )

        key_blob = " ".join(str(k).lower() for k in node.keys())
        looks_jobish = any(token in key_blob for token in ("job", "position", "hiring", "requisition", "ats"))

        if title and (raw_url or raw_id) and looks_jobish:
            full_url = _normalize_job_url(base_url, raw_url, raw_id)
            if full_url and full_url not in seen and "careerhub/explore/jobs" not in full_url:
                seen.add(full_url)
                out.append({"title": title[:200], "url": full_url})

        for value in node.values():
            _extract_jobs_from_object(value, base_url, out, seen)
        return

    if isinstance(node, list):
        for item in node:
            _extract_jobs_from_object(item, base_url, out, seen)


def extract_jobs_from_json_payloads(payloads: list[str], base_url: str) -> list[dict]:
    jobs: list[dict] = []
    seen: set[str] = set()
    for payload in payloads:
        if not payload:
            continue
        text = payload.strip()
        if not text.startswith(("{", "[")):
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        _extract_jobs_from_object(data, base_url, jobs, seen)
    return jobs


def extract_jobs_from_embedded_json(html: str, base_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    blobs: list[str] = []

    for code in soup.find_all("code"):
        text = code.get_text(strip=True)
        if not text:
            continue
        if code.get("id", "").lower().endswith("data") or "position" in text.lower():
            blobs.append(html_lib.unescape(text))

    for script in soup.find_all("script", attrs={"type": "application/json"}):
        text = script.get_text(strip=True)
        if text:
            blobs.append(html_lib.unescape(text))

    return extract_jobs_from_json_payloads(blobs, base_url)


# ===================================================================
# Extraction
# ===================================================================
JOB_PATTERNS = re.compile(
    r"(job|jobs|position|opening|role|vacanc|posting|hire|recruit|talent|requisition|req)",
    re.IGNORECASE,
)

# Generic CTA link texts that carry no job-title information — fall back to URL slug
_GENERIC_LINK_TITLES = frozenset({
    "see role", "apply", "apply now", "apply here", "view job", "view role",
    "learn more", "click here", "read more", "view", "open role", "explore",
    "view open positions", "explore open roles", "explore opportunities",
})

# URL path fragments that signal nav/UI links rather than job listings
_NAV_URL_RE = re.compile(
    r"/(saved[_-]jobs|login|sign-?in|logout|register|clear|reset)(/|$|\?)",
    re.IGNORECASE,
)


def _title_from_url_slug(url: str) -> str:
    """Derive a readable title from the last non-empty path segment of a URL."""
    path = urlparse(url).path.rstrip("/")
    segment = path.rsplit("/", 1)[-1] if "/" in path else path
    if not segment or segment.startswith("?"):
        return ""
    return segment.replace("-", " ").replace("_", " ").title()

MS_JOB_URL_PATTERNS = [
    re.compile(r"/careers/(job|jobs)/", re.IGNORECASE),
    re.compile(r"/v2/global/en/job/", re.IGNORECASE),
    re.compile(r"[?&](jobid|reqid|requisition|positionid|pid)=", re.IGNORECASE),
]


_LOGIN_URL_RE = re.compile(r"/(login|sign-?in|register)(\?|/|$)", re.IGNORECASE)


def filter_target_noise_jobs(source_url: str, jobs: list[dict]) -> list[dict]:
    """Drop obvious non-job links for known noisy career pages."""
    # Always strip login/apply-redirect URLs (e.g. iCIMS "Apply Now" buttons)
    jobs = [j for j in jobs if not _LOGIN_URL_RE.search(j.get("url", ""))]

    if "apply.careers.microsoft.com/careers" not in source_url:
        return jobs

    filtered: list[dict] = []
    seen: set[str] = set()
    for job in jobs:
        job_url = (job.get("url") or "").strip()
        if not job_url or job_url in seen:
            continue
        if any(p.search(job_url) for p in MS_JOB_URL_PATTERNS):
            seen.add(job_url)
            filtered.append(job)
    return filtered


def extract_jobs_from_html(html: str, url: str, link_selector: str = "") -> list[dict]:
    """
    Extract job links from rendered HTML.
    If link_selector is given, use it directly.
    Otherwise, use heuristics to find job-like links.
    """
    soup = BeautifulSoup(html, "lxml")
    jobs: list[dict] = []
    seen: set[str] = set()

    if link_selector:
        # Direct CSS selector mode — grab all matching links
        elements = soup.select(link_selector)
        for el in elements:
            # Could be an <a> tag or a container with an <a> inside
            if el.name == "a":
                a_tag = el
            else:
                a_tag = el.find("a", href=True)

            if not a_tag or not a_tag.get("href"):
                continue

            href = a_tag["href"].strip()
            full_url = urljoin(url, href)

            # Get text: prefer the element's full text over just the <a> text
            text = el.get_text(" ", strip=True) or a_tag.get_text(" ", strip=True)

            if not text or full_url in seen or href.startswith("#"):
                continue
            if _NAV_URL_RE.search(full_url):
                continue
            if text.lower() in _GENERIC_LINK_TITLES:
                text = _title_from_url_slug(full_url) or text

            seen.add(full_url)
            jobs.append({"title": text[:200], "url": full_url})
    else:
        # Heuristic mode — scan all links for job-like patterns
        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"].strip()
            full_url = urljoin(url, href)
            text = a_tag.get_text(" ", strip=True)

            if not text or full_url in seen or href.startswith("#"):
                continue
            if _NAV_URL_RE.search(full_url):
                continue

            if JOB_PATTERNS.search(href) or JOB_PATTERNS.search(text):
                if text.lower() in _GENERIC_LINK_TITLES:
                    text = _title_from_url_slug(full_url) or text
                seen.add(full_url)
                jobs.append({"title": text[:200], "url": full_url})

    return jobs


# ===================================================================
# Diffing
# ===================================================================
def _job_identity_url(url: str) -> str:
    """Normalize a job URL for comparison: ignore case, utm_* tracking params and a trailing slash."""
    parsed = urlparse(url.strip().lower())
    query = "&".join(p for p in parsed.query.split("&") if p and not p.startswith("utm_"))
    path = parsed.path.rstrip("/")
    if parsed.netloc in _TESLA_HOSTS:
        # Tesla job links put the title before the id; a title edit keeps the id.
        path = re.sub(r"^(.*/careers/search/job/)(?:.*-)?(\d+)$", r"\1\2", path)
    return urlunparse(parsed._replace(path=path, query=query))


def compute_job_id(job: dict) -> str:
    # Identify a job by its URL only: titles scraped from job cards often include
    # changing text ("Posted 2 days ago") that would make the same job look new.
    raw = _job_identity_url(job["url"])
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def diff_jobs(old_jobs: list[dict], new_jobs: list[dict]) -> list[dict]:
    old_ids = {compute_job_id(j) for j in old_jobs}
    return [j for j in new_jobs if compute_job_id(j) not in old_ids]


def merge_jobs_new_first(old_jobs: list[dict], current_jobs: list[dict], max_jobs: int) -> list[dict]:
    """Keep new jobs first, then previously known jobs, with de-duplication and optional cap."""
    old_ids = {compute_job_id(j) for j in old_jobs}
    current_ids = {compute_job_id(j) for j in current_jobs}

    merged: list[dict] = []
    merged_ids: set[str] = set()

    # Prepend newly discovered items from the current scrape.
    for job in current_jobs:
        jid = compute_job_id(job)
        if jid not in old_ids and jid not in merged_ids:
            merged.append(job)
            merged_ids.add(jid)

    # Then keep already-known items in their existing state order.
    for job in old_jobs:
        jid = compute_job_id(job)
        if jid in current_ids and jid not in merged_ids:
            merged.append(job)
            merged_ids.add(jid)

    # Finally include carried-over items that may no longer appear this run.
    for job in old_jobs:
        jid = compute_job_id(job)
        if jid not in merged_ids:
            merged.append(job)
            merged_ids.add(jid)

    if max_jobs > 0:
        # Never forget a job that is still listed, or it would be reported again next run.
        return merged[:max(max_jobs, len(current_ids))]
    return merged


# ===================================================================
# Keyword filtering
# ===================================================================
def _keyword_pattern(keyword: str) -> re.Pattern:
    """Match the keyword anywhere in a title, as typed, or as a word that a tag in the
    page split with a space ("Infra<wbr>structure" reaches the title as "Infra structure")."""
    alternatives = [re.escape(keyword)]
    letters = "".join(keyword.split())
    if letters:
        split_word = r"\s*".join(re.escape(c) for c in letters)
        end = r"(?!\w)" if keyword[-1].isspace() else ""
        alternatives.append(rf"(?<!\w){split_word}{end}")
    return re.compile("|".join(alternatives), re.IGNORECASE)


def filter_by_keywords(jobs: list[dict], keywords: list[str]) -> list[dict]:
    if not keywords:
        return jobs
    patterns = [_keyword_pattern(kw) for kw in keywords]
    return [j for j in jobs if any(p.search(j["title"]) for p in patterns)]


# ===================================================================
# Role filtering (exclusion-based)
# ===================================================================
# The role filter keeps every title unless it clearly says the role is senior,
# an internship, pure frontend, non-engineering or non-software engineering.
# Unusual titles ("Member of Technical Staff", "Forward Deployed Engineer")
# therefore pass. The word lists live in the config under "role_filter".
def _phrase_pattern(phrases) -> re.Pattern | None:
    """Whole-word, case-insensitive match of any phrase; a space in a phrase also matches
    a hyphen, slash, "&" or nothing ("front end" matches "front-end" and "frontend")."""
    parts = [r"[\s\-/&]*".join(re.escape(w) for w in str(p).split()) for p in phrases or [] if str(p).split()]
    if not parts:
        return None
    return re.compile(r"(?<!\w)(?:" + "|".join(parts) + r")(?!\w)", re.IGNORECASE)


def compile_role_filter(role_filter: dict | None) -> dict | None:
    if not isinstance(role_filter, dict):
        return None
    return {key: _phrase_pattern(value) for key, value in role_filter.items() if isinstance(value, list)}


_TITLE_TAIL_RE = re.compile(r" : | • | ⋅ | · |, United States\b")
_DASHES = str.maketrans({c: "-" for c in "\u2010\u2011\u2012\u2013\u2014\u2015\u2212"})


def _title_core(title: str) -> str:
    """The job title without text some sites append after it (a teaser sentence,
    team, location or salary), which could otherwise trip the role filter."""
    title = unicodedata.normalize("NFKC", title).translate(_DASHES)
    title = re.sub(r"^\s*icon\s+", "", title, flags=re.IGNORECASE)
    return _TITLE_TAIL_RE.split(title, 1)[0]


def role_exclusion_reason(title: str, patterns: dict | None) -> str | None:
    """Why the role filter leaves this title out, or None if it's kept."""
    if not patterns:
        return None
    title = _title_core(title)

    def hit(key, text=title):
        match = patterns.get(key) and patterns[key].search(text)
        return match.group(0) if match else None

    senior_text = patterns["senior_ignore"].sub(" ", title) if patterns.get("senior_ignore") else title
    if (word := hit("exclude_senior", senior_text)):
        return f"senior ({word})"
    if (word := hit("exclude_internships")) and not hit("internship_ok_if"):
        return f"internship ({word})"
    if (word := hit("exclude_frontend")) and not hit("frontend_ok_if"):
        return f"frontend ({word})"
    if (word := hit("exclude_non_engineering")) and not hit("engineering_signals"):
        return f"not engineering ({word})"
    if (word := hit("exclude_other_disciplines")) and not hit("software_signals"):
        return f"not software ({word})"
    return None


def split_new_jobs(jobs: list[dict], keywords: list[str], role_patterns: dict | None) -> tuple[list[dict], list[dict]]:
    """Split new jobs into (matches, left out). Left-out jobs are copies carrying a "reason"."""
    keyword_ok = {id(j) for j in filter_by_keywords(jobs, keywords)}
    matches, left_out = [], []
    for job in jobs:
        reason = "no keyword match" if id(job) not in keyword_ok else role_exclusion_reason(job["title"], role_patterns)
        if reason:
            left_out.append({**job, "reason": reason})
        else:
            matches.append(job)
    return matches, left_out



# ===================================================================
# Notifications
# ===================================================================
LEFT_OUT_HEADING = "Left out by your filters (listed so a misnamed role isn't missed)"


def _location_suffix(job: dict) -> str:
    return f" ({job['location']})" if job.get("location") else ""


def format_plain_report(all_new: dict[str, list[dict]], left_out: dict[str, list[dict]] | None = None) -> str:
    lines = [
        "=" * 60,
        f"  JOB MONITOR ALERT — {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "=" * 60,
        "",
    ]
    for company, jobs in all_new.items():
        lines.append(f"- {company}  ({len(jobs)} new)")
        for j in jobs:
            lines.append(f"    * {j['title']}{_location_suffix(j)}")
            lines.append(f"      {j['url']}")
        lines.append("")
    if left_out:
        lines.append(f"{LEFT_OUT_HEADING}: {sum(len(v) for v in left_out.values())}")
        for company, jobs in left_out.items():
            lines.append(f"- {company}")
            for j in jobs:
                lines.append(f"    · {j['title']}{_location_suffix(j)}  [{j['reason']}]")
                lines.append(f"      {j['url']}")
        lines.append("")
    lines.append("Sent by job_monitor.py")
    return "\n".join(lines)


def format_html_report(all_new: dict[str, list[dict]], left_out: dict[str, list[dict]] | None = None) -> str:
    esc = html_lib.escape
    rows = ""
    for company, jobs in all_new.items():
        rows += f'<h3 style="color:#1a73e8;margin-top:24px">{esc(company)} ({len(jobs)} new)</h3><ul>'
        for j in jobs:
            rows += (
                f'<li style="margin-bottom:8px">'
                f'<a href="{esc(j["url"])}" style="color:#1a73e8;text-decoration:none;font-weight:600">'
                f'{esc(j["title"])}</a>{esc(_location_suffix(j))}</li>'
            )
        rows += "</ul>"
    if left_out:
        total = sum(len(v) for v in left_out.values())
        rows += f'<h4 style="color:#777;margin-top:32px">{esc(LEFT_OUT_HEADING)}: {total}</h4>'
        for company, jobs in left_out.items():
            rows += f'<p style="color:#777;font-size:13px;margin:12px 0 4px">{esc(company)}</p><ul style="margin-top:0">'
            for j in jobs:
                rows += (
                    f'<li style="font-size:13px;color:#777"><a href="{esc(j["url"])}" style="color:#777">'
                    f'{esc(j["title"])}</a>{esc(_location_suffix(j))} <span style="color:#aaa">[{esc(j["reason"])}]</span></li>'
                )
            rows += "</ul>"
    heading = "New Job Postings Found" if all_new else "Postings left out by your filters"

    return f"""
    <div style="font-family:system-ui,sans-serif;max-width:600px;margin:auto;padding:20px">
      <h2 style="border-bottom:2px solid #1a73e8;padding-bottom:8px">{heading}</h2>
      <p style="color:#555">Detected on {datetime.now().strftime('%B %d, %Y at %I:%M %p')}</p>
      {rows}
      <p style="color:#999;font-size:12px;margin-top:32px">Sent by job_monitor.py</p>
    </div>
    """


def send_email(config: dict, all_new: dict[str, list[dict]], left_out: dict[str, list[dict]] | None = None) -> bool:
    """Email the report. Returns False if email is enabled but sending failed."""
    try:
        email_cfg = config.get("email") or {}
        if not email_cfg.get("enabled"):
            log.info("Email disabled — printing report to console only.")
            return True

        sender_email = email_cfg.get("sender_email") or os.environ.get("SENDER_EMAIL", "")
        sender_password = email_cfg.get("sender_password") or os.environ.get("SENDER_PASSWORD", "")

        recipients = email_cfg.get("recipient_email") or ""
        if isinstance(recipients, list):
            recipients = ", ".join(str(r).strip() for r in recipients if str(r).strip())
        if not recipients.strip():
            recipients = os.environ.get("RECIPIENT_EMAIL", "")
        if not recipients.strip():
            log.error(
                "Failed to send email: no recipient. Set recipient_email in the config "
                "or the RECIPIENT_EMAIL environment variable."
            )
            return False

        total = sum(len(v) for v in all_new.values())
        left_out_total = sum(len(v) for v in (left_out or {}).values())
        if total:
            subject = f"[Job Monitor] {total} new job posting{'s' if total != 1 else ''} found"
            if left_out_total:
                subject += f" (+{left_out_total} left out by filters)"
        else:
            subject = f"[Job Monitor] Digest: {left_out_total} posting{'s' if left_out_total != 1 else ''} left out by your filters"
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = sender_email
        msg["To"] = recipients

        # utf-8 makes MIMEText encode the parts, so long lines are wrapped safely in transit.
        msg.attach(MIMEText(format_plain_report(all_new, left_out), "plain", "utf-8"))
        msg.attach(MIMEText(format_html_report(all_new, left_out), "html", "utf-8"))

        # Verify the server's certificate so the password can't be sent to an impostor.
        context = ssl.create_default_context()
        port = int(email_cfg["smtp_port"])
        if port == 465:
            with smtplib.SMTP_SSL(
                email_cfg["smtp_server"], port, context=context, timeout=SMTP_TIMEOUT_SECONDS
            ) as server:
                server.login(sender_email, sender_password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(email_cfg["smtp_server"], port, timeout=SMTP_TIMEOUT_SECONDS) as server:
                server.ehlo()
                server.starttls(context=context)
                server.login(sender_email, sender_password)
                server.send_message(msg)
        log.info("Email sent successfully.")
        return True
    except Exception as exc:
        log.error(f"Failed to send email: {exc}")
        return False


def print_console_safe(text: str):
    """Print text without crashing on non-UTF8 Windows consoles (e.g., cp1252)."""
    try:
        print(text)
        return
    except UnicodeEncodeError:
        pass

    stdout = getattr(sys, "stdout", None)
    if stdout and hasattr(stdout, "buffer"):
        enc = getattr(stdout, "encoding", None) or "utf-8"
        payload = text if text.endswith("\n") else text + "\n"
        stdout.buffer.write(payload.encode(enc, errors="replace"))
        stdout.flush()
    else:
        # Final fallback for unusual environments.
        print(text.encode("ascii", errors="replace").decode("ascii"))


# ===================================================================
# Main
# ===================================================================
def fetch_target_jobs(url: str, mode: str, link_selector: str, wait_for: str) -> list[dict] | None:
    """Return every job currently listed for a target, or None if the page could not be fetched."""
    reader = job_board_api_reader(url)
    if reader:
        tag, read = reader
        jobs = read(url)
        if jobs is None:
            log.error(f"  [{tag}] Could not read the job board, skipping.")
            return None
        log.info(f"  [{tag}] Found {len(jobs)} jobs from the job board API")
        return jobs
    if mode == "sitemap":
        jobs = fetch_sitemap_jobs(url, link_selector)
        if jobs is None:
            log.error("  [sitemap] Could not read the sitemap, skipping.")
            return None
        log.info(f"  [sitemap] Found {len(jobs)} job pages in the sitemap")
        return jobs
    if is_tesla_careers_url(url):
        jobs = fetch_tesla_jobs(url)
        if jobs is not None:
            log.info(f"  [tesla] Found {len(jobs)} jobs from Tesla's jobs data")
            return jobs
        if urlparse(url).path.rstrip("/") == _TESLA_STATE_PATH:
            log.error("  [tesla] Could not read Tesla's jobs data, skipping.")
            return None
        log.info("  [tesla] Falling back to the careers page")

    google_jobs = fetch_google_jobs_from_page(url)
    if google_jobs is not None:
        log.info(f"  [google] Found {len(google_jobs)} job links from results page")
        return google_jobs

    # Prefer structured sources (official APIs, embedded data) over rendering the page.
    if mode in ("browser", "api", "eightfold"):
        for tag, fetcher, source in (
            ("eightfold-api", fetch_eightfold_jobs_via_api, "API"),
            ("phenom", fetch_phenom_jobs_from_page, "embedded data"),
            ("wd-api", fetch_workday_jobs_via_api, "API"),
            ("ms-api", fetch_microsoft_jobs_via_api, "API"),
        ):
            jobs = fetcher(url)
            if jobs is not None:
                log.info(f"  [{tag}] Found {len(jobs)} job links from {source}")
                return jobs

    # --- Fetch page content ---
    html = None
    browser_payloads: list[str] = []
    if mode in ("browser", "eightfold"):
        html, browser_payloads = fetch_browser(url, wait_for=wait_for)
    else:
        html = fetch_html(url)

    if html is None:
        log.error(f"  Could not fetch page, skipping.")
        return None

    # --- Extract jobs ---
    # In browser mode without an explicit selector, prefer structured data
    # before loose anchor heuristics to avoid nav/footer false positives.
    current_jobs: list[dict] = []

    if mode in ("browser", "eightfold") and not link_selector:
        if browser_payloads:
            payload_jobs = extract_jobs_from_json_payloads(browser_payloads, url)
            if payload_jobs:
                log.info(f"  [browser] Fallback extracted {len(payload_jobs)} jobs from API payloads")
                current_jobs = payload_jobs

        if not current_jobs:
            embedded_jobs = extract_jobs_from_embedded_json(html, url)
            if embedded_jobs:
                log.info(f"  [browser] Fallback extracted {len(embedded_jobs)} jobs from embedded JSON")
                current_jobs = embedded_jobs

        if not current_jobs:
            current_jobs = extract_jobs_from_html(html, url, link_selector)
    else:
        current_jobs = extract_jobs_from_html(html, url, link_selector)

        if not current_jobs and browser_payloads:
            payload_jobs = extract_jobs_from_json_payloads(browser_payloads, url)
            if payload_jobs:
                log.info(f"  [browser] Fallback extracted {len(payload_jobs)} jobs from API payloads")
                current_jobs = payload_jobs

        if not current_jobs:
            embedded_jobs = extract_jobs_from_embedded_json(html, url)
            if embedded_jobs:
                log.info(f"  [browser] Fallback extracted {len(embedded_jobs)} jobs from embedded JSON")
                current_jobs = embedded_jobs

    filtered_jobs = filter_target_noise_jobs(url, current_jobs)
    if len(filtered_jobs) != len(current_jobs):
        log.info(f"  [filter] Removed {len(current_jobs) - len(filtered_jobs)} non-job links")
        current_jobs = filtered_jobs

    log.info(f"  Found {len(current_jobs)} job links on page")

    if not current_jobs and is_tesla_careers_url(url):
        # Tesla always lists jobs, so none means the page was blocked too. Skipping
        # (instead of recording an empty first check) avoids a flood once it isn't.
        log.error("  [tesla] Tesla blocked both its jobs data and its careers page, skipping.")
        return None

    if len(current_jobs) == 0:
        log.warning(
            f"  [warning] No jobs found. If this page definitely has listings, try:\n"
            f"     - Switch mode to 'browser' if currently 'html'\n"
            f"     - Add/adjust link_selector and wait_for in config\n"
            f"     - Increase wait time for slow-loading pages"
        )

    return current_jobs


def check_target(
    target: dict,
    state: dict,
    default_keywords: list[str],
    role_patterns: dict | None = None,
    us_only: bool = False,
) -> tuple[list[dict], list[dict]]:
    """Scrape one target, record what it lists in state (as seen), and return its new
    jobs split into (matches, left out by the filters; copies carrying a "reason")."""
    name = target["name"]
    url = target["url"]
    mode = target.get("mode", "html")

    log.info(f"Checking: {name}")
    log.info(f"  URL: {url}")
    log.info(f"  Mode: {mode}")

    def fetch():
        return fetch_target_jobs(url, mode, target.get("link_selector", ""), target.get("wait_for", ""))

    current_jobs = fetch()
    if current_jobs is None:
        return [], []

    previous_jobs = state.get(url)
    if previous_jobs == [] and is_tesla_careers_url(url):
        # Tesla always lists jobs: an empty record was made while it was blocked
        # (before blocked checks were skipped), so this is really its first check.
        previous_jobs = None
    if previous_jobs is None:
        if not current_jobs:
            # Some sites render an empty list now and then. An empty baseline would
            # make the next good check alert everything the target lists, so look again.
            log.info("  [baseline] First check found no jobs; checking once more.")
            current_jobs = fetch()
            if current_jobs is None:
                return [], []
        # First check of this target: record what's already listed without
        # alerting, so adding a target doesn't email every job it has.
        state[url] = merge_jobs_new_first([], current_jobs, STATE_RETENTION_PER_TARGET)
        log.info(
            f"  [baseline] First check: recorded {len(current_jobs)} job(s). "
            f"New postings will be alerted from the next run."
        )
        return [], []

    # --- Diff against last run ---
    new_jobs = diff_jobs(previous_jobs, current_jobs)
    if target.get("us_only", us_only):
        outside = [j for j in new_jobs if is_outside_us(j)]
        if outside:
            log.info(f"  [location] Skipped {len(outside)} new posting(s) outside the US")
            new_jobs = [j for j in new_jobs if not is_outside_us(j)]
    new_jobs, left_out = split_new_jobs(new_jobs, target.get("keyword_filters", default_keywords), role_patterns)

    if new_jobs:
        log.info(f"  [new] {len(new_jobs)} NEW posting(s)!")
    else:
        log.info(f"  No new postings since last check.")
    if left_out:
        log.info(f"  [filtered] {len(left_out)} new posting(s) left out by the filters")

    state[url] = merge_jobs_new_first(previous_jobs, current_jobs, STATE_RETENTION_PER_TARGET)
    return new_jobs, left_out


def run(config_override: str | None = None) -> bool:
    """Check every target once. Returns False if new jobs were found but the email failed."""
    config = ensure_config(config_override)
    state = load_state()
    loaded_state = copy.deepcopy(state)
    keywords = config.get("keyword_filters", [])
    role_patterns = compile_role_filter(config.get("role_filter"))
    # With a role filter, postings the filters leave out are recorded as seen but also
    # kept in a pool in state.json until an email has listed them, so a role the filter
    # misjudges still reaches the inbox (in the next alert, or a digest) even if it has
    # left the careers page by then.
    show_left_out = bool(config.get("show_filtered", role_patterns is not None))
    digest_hours = _digest_hours(config)
    # Postings whose location data clearly puts them outside the US are skipped
    # (recorded as seen, not emailed). Unknown locations are kept.
    us_only = bool(config.get("us_only", False))
    all_new: dict[str, list[dict]] = {}
    new_left_out: list[dict] = []
    alerted_urls: set[str] = set()
    first_baselines: dict[str, list[dict]] = {}  # as recorded at each target's first check

    for target in config["targets"]:
        if not isinstance(target, dict):
            continue
        if target.get("_section"):
            continue

        name = target.get("name")
        url = target.get("url")
        if not name or not url:
            log.warning(f"Skipping target entry missing name/url: {target}")
            continue

        # One broken target (bad selector, unexpected API response) must not
        # stop the remaining targets from being checked, saved and emailed.
        first_check = url not in state
        try:
            new_jobs, left_out = check_target(target, state, keywords, role_patterns, us_only)
        except Exception:
            log.exception(f"  Failed to check {name}, skipping it this run")
            continue
        if first_check and url in state:
            first_baselines[url] = list(state[url])
        if new_jobs:
            all_new.setdefault(name, []).extend(new_jobs)
            alerted_urls.add(url)
        if left_out and show_left_out:
            new_left_out.extend({**job, "company": name} for job in left_out)

    # Save only the targets this run changed, so another monitor process
    # saving its own targets at the same time isn't overwritten. First-check
    # baselines are kept apart: they alert nothing, so they're saved even if
    # the email fails (otherwise a new target would re-baseline every run and
    # silently absorb whatever it posts while email is down).
    changed = {url: jobs for url, jobs in state.items() if loaded_state.get(url) != jobs}
    baselines = {url: jobs for url, jobs in changed.items() if url in first_baselines and url not in alerted_urls}
    updates = {url: jobs for url, jobs in changed.items() if url not in baselines}

    pending = _dedupe_jobs(list(state.get(PENDING_LEFT_OUT_KEY) or []) + new_left_out)
    digest_due = bool(pending) and _digest_due(state.get(LAST_EMAIL_KEY), digest_hours)
    if not all_new and not digest_due:
        if changed or new_left_out:
            save_state(updates, baselines, pending_add=new_left_out)
        if pending:
            log.info(f"{len(pending)} posting(s) left out by the filters will be listed in the next email or digest.")
        log.info("No new postings found across all targets.")
        return True

    left_out_report = _group_by_company(pending)
    report = format_plain_report(all_new, left_out_report)
    print_console_safe("\n" + report)
    if not send_email(config, all_new, left_out=left_out_report):
        # Keep the alerted targets as they were so these postings are reported again next
        # run, but keep new targets' baselines as first recorded (before any alert against
        # them, e.g. from a second config entry for the same URL). Left-out postings go
        # into the pool so the retry lists them even if they've left the page.
        if first_baselines or new_left_out:
            save_state({}, first_baselines, pending_add=new_left_out)
        log.error("New postings not marked as seen: they'll be reported again on the next run.")
        return False

    save_state(
        updates,
        baselines,
        pending_shown={compute_job_id(p) for p in pending},
        last_email=datetime.now().isoformat(timespec="seconds") if show_left_out else None,
    )
    return True


def _dedupe_jobs(jobs: list[dict]) -> list[dict]:
    seen, unique = set(), []
    for job in jobs:
        job_id = compute_job_id(job)
        if job_id not in seen:
            seen.add(job_id)
            unique.append(job)
    return unique


def _group_by_company(jobs: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for job in jobs:
        grouped.setdefault(job.get("company") or "Other", []).append(job)
    return grouped


def _digest_hours(config: dict) -> float:
    value = config.get("filtered_digest_hours", 24)
    try:
        hours = float(value)
        if hours >= 0:
            return hours
    except (TypeError, ValueError):
        pass
    log.warning(f"filtered_digest_hours is {value!r}, not a number of hours; using 24.")
    return 24.0


def _digest_due(last_email_at, hours: float) -> bool:
    """Whether left-out postings should be emailed without waiting for a new match."""
    try:
        last = datetime.fromisoformat(str(last_email_at))
    except (TypeError, ValueError):
        return True  # no email recorded yet
    return (datetime.now() - last).total_seconds() >= hours * 3600


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Monitor job postings from configured targets.")
    parser.add_argument(
        "--config",
        dest="config",
        default=None,
        help="Path to config file (relative to workspace or absolute).",
    )
    parser.add_argument(
        "--interval-minutes",
        dest="interval_minutes",
        type=float,
        default=0,
        help="Run repeatedly every N minutes (0 = run once).",
    )
    args = parser.parse_args()
    if args.interval_minutes and args.interval_minutes > 0:
        interval_seconds = max(1, int(args.interval_minutes * 60))
        log.info(f"Starting periodic mode: every {args.interval_minutes} minute(s)")
        while True:
            try:
                run(args.config)
            except Exception:
                log.exception("Periodic run failed")
            log.info(f"Sleeping for {interval_seconds} second(s) before next run")
            time.sleep(interval_seconds)
    else:
        sys.exit(0 if run(args.config) else 1)
