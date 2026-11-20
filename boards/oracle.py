"""Oracle Recruiting Cloud (Oracle HCM) career sites: links like
https://<host>.oraclecloud.com/hcmUI/CandidateExperience/<language>/sites/<site>/job/<id>
(about 180 company sites in the lists: American Express, Fortinet, Oracle, Texas Instruments,
Cummins...). One host can have several sites (CX_1, CX_45001...), so a site is known by its host
and site number (site_key), not by its host alone.

A site is read through the public API its career page uses (recruitingCEJobRequisitions), newest
first (sortBy=POSTING_DATES_DESC), LIMIT postings a read: the same query your JPMorgan entry uses
(job_monitor.fetch_oracle_hcm_jobs reads both). Only the newest are read, not the whole list, so
a posting missing from a read may still be open (it is never forgotten for that: see
job_monitor.FORGET_GONE_PLATFORMS).

A posting is known across links by its host and requisition number (job_monitor.job_keys), the
same in a list's link and in the API's answer whatever the language in the link."""
import re
from urllib.parse import urlparse

LIMIT = 100  # postings a read (the API's most), newest first

_HOST = re.compile(r"^[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\.oraclecloud\.com$")
_SITE_PATH = re.compile(r"^/hcmUI/CandidateExperience/[^/]+/sites/([A-Za-z0-9_-]+)(?:/|$)", re.IGNORECASE)
_JOB_PATH = re.compile(r"/sites/[^/]+/job/(\d+)(?:/|$)", re.IGNORECASE)
_API_PATH = "/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
_SITE_IN_FINDER = re.compile(r"siteNumber=([A-Za-z0-9_-]+)")


def _host(link) -> str:
    try:
        return urlparse(str(link or "").strip()).netloc.lower()
    except ValueError:
        return ""


def site_of(link) -> tuple[str, str] | None:
    """(host, site number) of a career-site link (a posting's or the site's), or of a site's API
    URL; None for anything else."""
    host = _host(link)
    if not _HOST.match(host):
        return None
    parsed = urlparse(str(link).strip())
    match = _SITE_PATH.match(parsed.path)
    if match:
        return host, match.group(1)
    if parsed.path.rstrip("/").lower() == _API_PATH.lower():
        site = _SITE_IN_FINDER.search(parsed.query)
        return (host, site.group(1)) if site else None
    return None


def api_url(host: str, site: str) -> str:
    """A site's newest LIMIT postings, newest first (with their other locations)."""
    return (f"https://{host}{_API_PATH}?onlyData=true&expand=requisitionList.secondaryLocations"
            f"&finder=findReqs;siteNumber={site},limit={LIMIT},sortBy=POSTING_DATES_DESC")


def site_key(link) -> str:
    """The same for every link to one site (its postings', its page, its API URL): host and
    site number, lower-case. Empty for anything else."""
    site = site_of(link)
    return f"oracle:{site[0]}/{site[1].lower()}" if site else ""


def page_url(link) -> str:
    """The site's job search page people visit."""
    host, site = site_of(link) or ("", "")
    return f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/jobs" if host else str(link)


def job_number(link) -> tuple[str, str] | None:
    """(host, requisition number) of a posting's link; None for anything else."""
    host = _host(link)
    if not _HOST.match(host):
        return None
    match = _JOB_PATH.search(urlparse(str(link).strip()).path)
    return (host, match.group(1)) if match else None


def is_api_url(url) -> bool:
    host = _host(url)
    return bool(_HOST.match(host)) and urlparse(str(url)).path.rstrip("/").lower() == _API_PATH.lower()
