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

# Companies' own career sites that show an Oracle site's postings under their own links, with the
# Oracle requisition number in them (your Goldman Sachs and Uber entries read these): a posting
# there and on the Oracle site the lists link is one job (job_number).
FRONT_ENDS = {
    "higher.gs.com": (re.compile(r"^/roles/(\d+)(?:/|$)"), "hdpc.fa.us2.oraclecloud.com"),  # Goldman Sachs
    "jobs.uber.com": (re.compile(r"^/(?:[a-z]{2}(?:-[a-z]{2})?/)?jobs/(\d+)(?:/|$)", re.IGNORECASE),
                      "iaziqy.fa.ocs.oraclecloud.com"),  # Uber
}


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
    """(Oracle host, requisition number) of a posting's link, on an Oracle site or a career site
    in FRONT_ENDS; None for anything else."""
    host = _host(link).removeprefix("www.")
    path = urlparse(str(link).strip()).path if host else ""
    if host in FRONT_ENDS:
        pattern, oracle_host = FRONT_ENDS[host]
        match = pattern.match(path)
        return (oracle_host, match.group(1)) if match else None
    if not _HOST.match(host):
        return None
    match = _JOB_PATH.search(path)
    return (host, match.group(1)) if match else None


def knows_links_of(url) -> bool:
    """Whether postings read from this address (one of your entries) can be matched with an Oracle
    site's by requisition number: an Oracle site, or a career site in FRONT_ENDS."""
    host = _host(url).removeprefix("www.")
    return bool(_HOST.match(host)) or host in FRONT_ENDS


def is_api_url(url) -> bool:
    host = _host(url)
    return bool(_HOST.match(host)) and urlparse(str(url)).path.rstrip("/").lower() == _API_PATH.lower()
