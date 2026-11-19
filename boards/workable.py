"""Workable companies: links like apply.workable.com/<account>/j/<job code>.

Workable publishes one file of every customer's published jobs, updated hourly, for exactly this
kind of use (FEED_URL). It is read once per boards check (If-Modified-Since: "not modified" when
unchanged), rather than asking each company's page (quick reading of those is answered "429 Too
Many Requests"). The file names companies (name and website), not their Workable accounts, so an
account is matched to its company there:

1. through a job code: one the new-grad lists linked it to, one Workable lists for the account
   when asked (look_up_accounts: once a day at most, a few accounts a run, paced), or one of the
   account's own postings recorded at earlier checks (so a company that renames itself in
   Workable stays matched);
2. failing that, by name: when exactly one company in the file has the account's name (the one
   Workable gives it, else the lists'). A job code linked later wins over a name match.

An account that isn't matched isn't checked: an empty first check would flood later emails.
"""
import io
import logging
import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime

import requests

from .common import HEADERS, MONITOR_USER_AGENT, hours_since, posted_at

log = logging.getLogger("job_monitor.workable")

FEED_URL = "https://www.workable.com/boards/workable.xml"
ACCOUNT_API = "https://www.workable.com/api/accounts/{}"  # Workable's public list of an account's published jobs
CODES_KEPT = 20             # job codes remembered per account not matched by code, to match it in the file
MIN_ROWS_SHARE = 0.5        # a file with fewer rows than this share of the last one is skipped...
SMALL_ACCEPT_HOURS = 6      # ...unless it has stayed that small this long: then it's the new normal
LOOKUPS_PER_RUN = 30        # accounts asked about per boards check...
LOOKUP_DELAY_SECONDS = 2.0  # ...this far apart...
LOOKUP_AGAIN_HOURS = 24     # ...each at most once a day

_LEGAL_WORDS = re.compile(r"\b(?:inc|incorporated|llc|ltd|limited|corp|corporation|co|company|gmbh|plc|pbc|lp|llp)\b")


def job_code(link) -> str | None:
    """The job code (10 hex digits, upper case) in a Workable posting link, else None."""
    match = re.search(r"apply\.workable\.com/(?:[^/?#]+/)?j/([0-9A-Fa-f]{10})(?=[/?#]|$)", str(link or ""))
    return match.group(1).upper() if match else None


def account_of(board_url) -> str:
    """The account in a board URL (https://apply.workable.com/<account>/)."""
    return str(board_url or "").rstrip("/").rsplit("/", 1)[-1]


def _matched_by_code(info: dict) -> bool:
    return bool(info.get("tenant")) and info.get("matched_by") != "name"


def remember_codes(known: dict, codes: dict):
    """Remember these job codes ({key: [code, ...], newest first}) for accounts in the registry
    (known) not matched by code yet (one matched by code is matched again through the postings it
    lists; one matched by name can still be corrected by a code). Only a different set of codes is
    a change."""
    for key, found in codes.items():
        info = known.get(key)
        if info is None or _matched_by_code(info) or not found:
            continue
        kept = list(dict.fromkeys(found + list(info.get("codes") or [])))[:CODES_KEPT]
        if set(kept) != set(info.get("codes") or []):
            info["codes"] = kept


def look_up_accounts(accounts: list[dict], known: dict) -> None:
    """Ask Workable (ACCOUNT_API) for the name and current job codes of the accounts not matched
    by code yet: each at most once every LOOKUP_AGAIN_HOURS, LOOKUPS_PER_RUN a run, longest
    waiting first, LOOKUP_DELAY_SECONDS apart, under the monitor's own name; none more this run
    once Workable answers "too many requests" or refuses. The codes join the account's ("codes",
    see remember_codes), and the name Workable gives it is kept ("workable_name")."""
    due = [b for b in accounts if b.get("_board_key") in known and not _matched_by_code(known[b["_board_key"]])
           and hours_since(known[b["_board_key"]].get("looked_up")) >= LOOKUP_AGAIN_HOURS]
    due.sort(key=lambda b: (str(known[b["_board_key"]].get("looked_up") or ""), b["_board_key"]))
    answered, with_jobs, failed, refused = 0, 0, 0, None
    for i, board in enumerate(due[:LOOKUPS_PER_RUN]):
        if i:
            time.sleep(LOOKUP_DELAY_SECONDS)
        info = known[board["_board_key"]]
        try:
            resp = requests.get(ACCOUNT_API.format(account_of(board["url"])), timeout=30,
                                headers={**HEADERS, "User-Agent": MONITOR_USER_AGENT, "Accept": "application/json"})
        except Exception:
            failed += 1
            continue
        if resp.status_code == 404:  # no such account any more: the name may still match
            info["looked_up"] = datetime.now().isoformat(timespec="seconds")
            answered += 1
            continue
        try:
            data = resp.json() if resp.status_code == 200 else None
        except ValueError:
            data = None
        if not isinstance(data, dict):  # refused (429, 403), an error, or a page that isn't the answer (a browser check)
            refused = resp.status_code if resp.status_code != 200 else "a page that isn't the job list"
            break
        info["looked_up"] = datetime.now().isoformat(timespec="seconds")
        answered += 1
        name = str(data.get("name") or "").strip()
        if name:
            info["workable_name"] = name[:200]
        codes = [str(job.get("shortcode") or "").strip().upper() for job in data.get("jobs") or [] if isinstance(job, dict)]
        codes = [c for c in codes if c]
        if codes:
            with_jobs += 1
            remember_codes(known, {board["_board_key"]: codes})
    if due:
        log.info(f"  [workable] Asked Workable about {answered + failed + (1 if refused else 0)} of {len(due)} companies "
                 f"not matched yet: {with_jobs} list current jobs" + (f", {failed} couldn't be asked" if failed else "") + ".")
    if refused:
        log.warning(f"  [workable] Workable answered {refused} when asked for a company's jobs; "
                    f"the other companies wait for the next check.")


def name_key(name) -> str:
    """A company name for comparing: lower case, letters and digits only, without Inc, LLC and the like."""
    text = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode().lower()
    return " ".join(_LEGAL_WORDS.sub(" ", re.sub(r"[^a-z0-9]+", " ", text)).split())


def _only_company_named(by_name: dict, names) -> tuple | None:
    """The one company in the file with the first of these names that the file has; None if
    there's no such company, or several have that name."""
    for name in names:
        companies = by_name.get(name_key(name)) if name_key(name) else None
        if companies:
            return companies[0] if len(companies) == 1 else None
    return None


def read_feed(boards: list[dict], meta: dict, recorded: dict | None = None) -> dict[str, tuple]:
    """Read FEED_URL once for these Workable boards. Returns {url: (status, jobs, None)}: "ok"
    with the postings of the board's company, "unchanged" (304) or "failed"; boards not matched
    yet are left out (tried again next time). A board's company in the file is
    meta["known"][key]["tenant"] (see the module's docstring for how it's found); recorded is
    {url: [job code of a posting recorded at earlier checks, ...]}."""
    state = meta.setdefault("workable", {})
    headers = {**HEADERS, "Accept-Encoding": "gzip"}
    if state.get("last_modified"):
        headers["If-Modified-Since"] = state["last_modified"]
    every = lambda status: {b["url"]: (status, None, None) for b in boards}
    try:
        resp = requests.get(FEED_URL, headers=headers, stream=True, timeout=(30, 300))
    except Exception as exc:
        log.warning(f"  [workable] Could not read Workable's job file: {exc.__class__.__name__}")
        return every("failed")
    if resp.status_code == 304:
        return every("unchanged")
    if resp.status_code != 200:
        log.warning(f"  [workable] Workable's job file answered {resp.status_code}"
                    + (f" (retry after {resp.headers.get('Retry-After')} s)" if resp.headers.get("Retry-After") else ""))
        return every("failed")
    known = meta["known"]
    # Codes the lists linked or Workable listed win over codes of recorded postings (which, after
    # a wrong name match, are the wrong company's).
    linked = {code: b["_board_key"] for b in boards for code in (known.get(b["_board_key"]) or {}).get("codes") or []}
    own = {code: b["_board_key"] for b in boards for code in (recorded or {}).get(b["url"]) or []}
    rows: dict[tuple, list] = {}
    learned_linked: dict[str, tuple] = {}
    learned_own: dict[str, tuple] = {}
    count = 0
    try:
        raw = getattr(resp, "raw", None)
        if raw is not None and hasattr(raw, "read"):
            raw.decode_content = True
            source = raw
        else:
            source = io.BytesIO(resp.content)
        for _, el in ET.iterparse(source):
            if el.tag != "job":
                continue
            count += 1
            field = lambda name: (el.findtext(name) or "").strip()
            tenant = (field("company"), field("website"))
            code = field("referencenumber").upper()
            rows.setdefault(tenant, []).append((field("title"), field("url"), code, field("date"),
                                                field("city"), field("state"), field("country")))
            if code in linked:
                learned_linked.setdefault(linked[code], tenant)
            if code in own:
                learned_own.setdefault(own[code], tenant)
            el.clear()
    except Exception as exc:
        log.warning(f"  [workable] Workable's job file was cut off or unreadable ({exc.__class__.__name__}); next check retries.")
        return every("failed")
    if count < MIN_ROWS_SHARE * (state.get("rows") or 0):
        since = state.setdefault("small_since", datetime.now().isoformat(timespec="seconds"))
        if hours_since(since) < SMALL_ACCEPT_HOURS:
            log.warning(f"  [workable] Workable's job file has {count} jobs (last time {state.get('rows')}): skipped "
                        f"for now; if it stays this small for {SMALL_ACCEPT_HOURS} hours, it's the new normal.")
            return every("failed")
        log.warning(f"  [workable] Workable's job file has had about {count} jobs for {SMALL_ACCEPT_HOURS} hours: "
                    f"taking that as the new normal.")
    state.pop("small_since", None)
    state.update(last_modified=resp.headers.get("Last-Modified") or resp.headers.get("last-modified"), rows=count)

    for b in boards:  # 1. through a job code
        info = known.get(b["_board_key"])
        tenant = learned_linked.get(b["_board_key"]) or learned_own.get(b["_board_key"])
        if info is None or tenant is None:
            continue
        if list(tenant) != info.get("tenant"):
            if info.get("tenant"):
                log.info(f"  [workable] {info.get('name')} is now {tenant[0]!r} in Workable's file.")
            info["tenant"] = list(tenant)
        if b["_board_key"] in learned_linked:
            info.pop("matched_by", None)

    by_name: dict[str, list] = {}  # 2. by name
    for tenant in rows:
        by_name.setdefault(name_key(tenant[0]), []).append(tenant)
    taken = {tuple(info["tenant"]) for info in known.values() if isinstance(info, dict) and info.get("tenant")}
    wanted: dict[tuple, list] = {}
    for b in boards:
        info = known.get(b["_board_key"])
        if info is not None and not info.get("tenant"):
            tenant = _only_company_named(by_name, (info.get("workable_name"), info.get("name")))
            if tenant is not None and tenant not in taken:
                wanted.setdefault(tenant, []).append(b["_board_key"])
    for tenant, keys in wanted.items():
        if len(keys) == 1:  # (two accounts with the name: neither)
            known[keys[0]].update(tenant=list(tenant), matched_by="name")
            log.info(f"  [workable] {known[keys[0]].get('name')}: matched by name to {tenant[0]!r} ({tenant[1]}) in Workable's file.")

    results = {}
    for b in boards:
        info = known.get(b["_board_key"])
        if info is not None and info.get("tenant"):
            results[b["url"]] = ("ok", jobs_from_rows(rows.get(tuple(info["tenant"]), [])), None)
    by_name_count = sum(1 for b in boards if b["url"] in results and known[b["_board_key"]].get("matched_by") == "name")
    log.info(f"  [workable] Job file: {count} jobs; {len(results)} of {len(boards)} Workable companies matched in it"
             + (f" ({by_name_count} by name)" if by_name_count else "") + ".")
    return results


def jobs_from_rows(rows: list[tuple]) -> list[dict]:
    """One posting per job code from the file's rows (one row per location): the earliest date, all locations."""
    jobs: dict[str, dict] = {}
    for title, url, code, date, city, region, country in rows:
        if not title or not url:
            continue
        job = jobs.setdefault(code or url, {"title": title[:200], "url": url, "locations": [], "countries": [], "dates": []})
        place = ", ".join(p for p in (city, region, country) if p)
        if place and place not in job["locations"]:
            job["locations"].append(place)
        if country and country not in job["countries"]:
            job["countries"].append(country)
        try:
            job["dates"].append(parsedate_to_datetime(date))
        except (TypeError, ValueError, IndexError):
            pass
    out = []
    for job in jobs.values():
        dated = [d for d in job["dates"] if d is not None]
        posted = posted_at(min(dated).isoformat()) if dated else None
        out.append({"title": job["title"], "url": job["url"], "location": "; ".join(job["locations"]),
                    "countries": job["countries"], **({"posted": posted} if posted else {})})
    return out
