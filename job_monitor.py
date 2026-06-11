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

import json
import hashlib
import re
import os
import sys
import argparse
import smtplib
import logging
import time
import html as html_lib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin
from urllib.parse import urlparse, parse_qs

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
    "max_jobs_per_target": 10,
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


def save_json(path: Path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def ensure_config(config_override: str | None = None):
    override = config_override or os.environ.get("JOB_MONITOR_CONFIG", "").strip()
    if override:
        active_config_path = Path(override)
        if not active_config_path.is_absolute():
            active_config_path = BASE_DIR / active_config_path
    else:
        active_config_path = CONFIG_ALL_PATH if CONFIG_ALL_PATH.exists() else CONFIG_PATH

    if not active_config_path.exists():
        save_json(CONFIG_PATH, DEFAULT_CONFIG)
        log.info(f"Created default config at {CONFIG_PATH}")
        log.info("Edit it with your targets and re-run.")
        sys.exit(0)
    log.info(f"Using config file: {active_config_path.name}")
    return load_json(active_config_path)


def load_state() -> dict:
    return load_json(STATE_PATH, default={})


def save_state(state: dict):
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
            text = el.get_text(strip=True) or a_tag.get_text(strip=True)

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
            text = a_tag.get_text(strip=True)

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
def compute_job_id(job: dict) -> str:
    raw = f"{job['url']}|{job['title']}".lower().strip()
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
        return merged[:max_jobs]
    return merged


# ===================================================================
# Keyword filtering
# ===================================================================
def filter_by_keywords(jobs: list[dict], keywords: list[str]) -> list[dict]:
    if not keywords:
        return jobs
    patterns = [re.compile(re.escape(kw), re.IGNORECASE) for kw in keywords]
    return [j for j in jobs if any(p.search(j["title"]) for p in patterns)]



# ===================================================================
# Notifications
# ===================================================================
def format_plain_report(all_new: dict[str, list[dict]]) -> str:
    lines = [
        "=" * 60,
        f"  JOB MONITOR ALERT — {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "=" * 60,
        "",
    ]
    for company, jobs in all_new.items():
        lines.append(f"- {company}  ({len(jobs)} new)")
        for j in jobs:
            lines.append(f"    * {j['title']}")
            lines.append(f"      {j['url']}")
        lines.append("")
    lines.append("Sent by job_monitor.py")
    return "\n".join(lines)


def format_html_report(all_new: dict[str, list[dict]]) -> str:
    rows = ""
    for company, jobs in all_new.items():
        rows += f'<h3 style="color:#1a73e8;margin-top:24px">{company} ({len(jobs)} new)</h3><ul>'
        for j in jobs:
            rows += (
                f'<li style="margin-bottom:8px">'
                f'<a href="{j["url"]}" style="color:#1a73e8;text-decoration:none;font-weight:600">'
                f'{j["title"]}</a></li>'
            )
        rows += "</ul>"

    return f"""
    <div style="font-family:system-ui,sans-serif;max-width:600px;margin:auto;padding:20px">
      <h2 style="border-bottom:2px solid #1a73e8;padding-bottom:8px">New Job Postings Found</h2>
      <p style="color:#555">Detected on {datetime.now().strftime('%B %d, %Y at %I:%M %p')}</p>
      {rows}
      <p style="color:#999;font-size:12px;margin-top:32px">Sent by job_monitor.py</p>
    </div>
    """


def send_email(config: dict, all_new: dict[str, list[dict]]):
    email_cfg = config["email"]
    if not email_cfg.get("enabled"):
        log.info("Email disabled — printing report to console only.")
        return

    sender_email = email_cfg.get("sender_email") or os.environ.get("SENDER_EMAIL", "")
    sender_password = email_cfg.get("sender_password") or os.environ.get("SENDER_PASSWORD", "")

    total = sum(len(v) for v in all_new.values())
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[Job Monitor] {total} new job posting{'s' if total != 1 else ''} found"
    msg["From"] = sender_email
    recipients = email_cfg["recipient_email"]
    if isinstance(recipients, list):
        recipients = ", ".join(recipients)
    msg["To"] = recipients

    msg.attach(MIMEText(format_plain_report(all_new), "plain"))
    msg.attach(MIMEText(format_html_report(all_new), "html"))

    try:
        port = int(email_cfg["smtp_port"])
        if port == 465:
            with smtplib.SMTP_SSL(email_cfg["smtp_server"], port) as server:
                server.login(sender_email, sender_password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(email_cfg["smtp_server"], port) as server:
                server.ehlo()
                server.starttls()
                server.login(sender_email, sender_password)
                server.send_message(msg)
        log.info("Email sent successfully.")
    except Exception as exc:
        log.error(f"Failed to send email: {exc}")


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


def limit_jobs_per_target(jobs: list[dict], max_jobs: int) -> list[dict]:
    """Keep only the first N jobs for stable periodic comparison; 0 disables limiting."""
    if max_jobs <= 0:
        return jobs
    return jobs[:max_jobs]


# ===================================================================
# Main
# ===================================================================
def run(config_override: str | None = None):
    config = ensure_config(config_override)
    state = load_state()
    keywords = config.get("keyword_filters", [])
    max_jobs_per_target = int(config.get("max_jobs_per_target", 10) or 0)
    all_new: dict[str, list[dict]] = {}

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
        mode = target.get("mode", "html")
        link_selector = target.get("link_selector", "")
        wait_for = target.get("wait_for", "")
        active_keywords = target.get("keyword_filters", keywords)

        log.info(f"Checking: {name}")
        log.info(f"  URL: {url}")
        log.info(f"  Mode: {mode}")

        google_jobs = fetch_google_jobs_from_page(url)
        if google_jobs is not None:
            current_jobs = limit_jobs_per_target(google_jobs, max_jobs_per_target)
            log.info(f"  [google] Found {len(current_jobs)} job links from results page")

            previous_jobs = state.get(url, [])
            new_jobs = diff_jobs(previous_jobs, current_jobs)
            new_jobs = filter_by_keywords(new_jobs, active_keywords)

            if new_jobs:
                log.info(f"  [new] {len(new_jobs)} NEW posting(s)!")
                all_new[name] = new_jobs
            else:
                log.info(f"  No new postings since last check.")

            state[url] = merge_jobs_new_first(previous_jobs, current_jobs, max_jobs_per_target)
            continue

        if mode in ("browser", "api", "eightfold"):
            eightfold_jobs = fetch_eightfold_jobs_via_api(url)
            if eightfold_jobs is not None:
                current_jobs = limit_jobs_per_target(eightfold_jobs, max_jobs_per_target)
                log.info(f"  [eightfold-api] Found {len(current_jobs)} job links from API")

                previous_jobs = state.get(url, [])
                new_jobs = diff_jobs(previous_jobs, current_jobs)
                new_jobs = filter_by_keywords(new_jobs, active_keywords)

                if new_jobs:
                    log.info(f"  [new] {len(new_jobs)} NEW posting(s)!")
                    all_new[name] = new_jobs
                else:
                    log.info(f"  No new postings since last check.")

                state[url] = merge_jobs_new_first(previous_jobs, current_jobs, max_jobs_per_target)
                continue

        if mode in ("browser", "api", "eightfold"):
            phenom_jobs = fetch_phenom_jobs_from_page(url)
            if phenom_jobs is not None:
                current_jobs = limit_jobs_per_target(phenom_jobs, max_jobs_per_target)
                log.info(f"  [phenom] Found {len(current_jobs)} job links from embedded data")

                previous_jobs = state.get(url, [])
                new_jobs = diff_jobs(previous_jobs, current_jobs)
                new_jobs = filter_by_keywords(new_jobs, active_keywords)

                if new_jobs:
                    log.info(f"  [new] {len(new_jobs)} NEW posting(s)!")
                    all_new[name] = new_jobs
                else:
                    log.info(f"  No new postings since last check.")

                state[url] = merge_jobs_new_first(previous_jobs, current_jobs, max_jobs_per_target)
                continue

        if mode in ("browser", "api", "eightfold"):
            wd_jobs = fetch_workday_jobs_via_api(url)
            if wd_jobs is not None:
                current_jobs = limit_jobs_per_target(wd_jobs, max_jobs_per_target)
                log.info(f"  [wd-api] Found {len(current_jobs)} job links from API")

                previous_jobs = state.get(url, [])
                new_jobs = diff_jobs(previous_jobs, current_jobs)
                new_jobs = filter_by_keywords(new_jobs, active_keywords)

                if new_jobs:
                    log.info(f"  [new] {len(new_jobs)} NEW posting(s)!")
                    all_new[name] = new_jobs
                else:
                    log.info(f"  No new postings since last check.")

                state[url] = merge_jobs_new_first(previous_jobs, current_jobs, max_jobs_per_target)
                continue

        # Prefer official API for Microsoft careers pages when available.
        if mode in ("browser", "api", "eightfold"):
            ms_jobs = fetch_microsoft_jobs_via_api(url)
            if ms_jobs is not None:
                current_jobs = limit_jobs_per_target(ms_jobs, max_jobs_per_target)
                log.info(f"  [ms-api] Found {len(current_jobs)} job links from API")

                previous_jobs = state.get(url, [])
                new_jobs = diff_jobs(previous_jobs, current_jobs)
                new_jobs = filter_by_keywords(new_jobs, active_keywords)

                if new_jobs:
                    log.info(f"  [new] {len(new_jobs)} NEW posting(s)!")
                    all_new[name] = new_jobs
                else:
                    log.info(f"  No new postings since last check.")

                state[url] = merge_jobs_new_first(previous_jobs, current_jobs, max_jobs_per_target)
                continue

        # --- Fetch page content ---
        html = None
        browser_payloads: list[str] = []
        if mode in ("browser", "eightfold"):
            html, browser_payloads = fetch_browser(url, wait_for=wait_for)
        else:
            html = fetch_html(url)

        if html is None:
            log.error(f"  Could not fetch page, skipping.")
            continue

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

        current_jobs = limit_jobs_per_target(current_jobs, max_jobs_per_target)

        log.info(f"  Found {len(current_jobs)} job links on page")

        if len(current_jobs) == 0:
            log.warning(
                f"  [warning] No jobs found. If this page definitely has listings, try:\n"
                f"     - Switch mode to 'browser' if currently 'html'\n"
                f"     - Add/adjust link_selector and wait_for in config\n"
                f"     - Increase wait time for slow-loading pages"
            )

        # --- Diff against last run ---
        previous_jobs = state.get(url, [])
        new_jobs = diff_jobs(previous_jobs, current_jobs)
        new_jobs = filter_by_keywords(new_jobs, keywords)

        if new_jobs:
            log.info(f"  [new] {len(new_jobs)} NEW posting(s)!")
            all_new[name] = new_jobs
        else:
            log.info(f"  No new postings since last check.")

        # Update state
        state[url] = merge_jobs_new_first(previous_jobs, current_jobs, max_jobs_per_target)

    save_state(state)

    if all_new:
        report = format_plain_report(all_new)
        print_console_safe("\n" + report)
        send_email(config, all_new)
    else:
        log.info("No new postings found across all targets.")


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
        run(args.config)
