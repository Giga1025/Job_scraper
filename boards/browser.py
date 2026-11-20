"""One headless Chromium (Playwright) for a run, for job sites that refuse scripted requests but
let a browser open their pages (Workday since Nov 2026, see boards/workday.py).

It opens a site's page the way a visitor's browser does and takes the data the page itself
fetches; more of that data is asked from inside the page, the way the page asks for it. It
doesn't try to get past a bot check: a page that answers with one (a "Cf-Mitigated" header, or
403/429/503) is closed at once and reported as "challenge". No stealth add-ons, no proxies, no
solving anything, no retries. A page that answers with another server error (500, 502, 504) is
closed at once too ("unavailable": it won't fetch its data).

Every wait has a time limit, so a stalled site can't hold up the run. Opening a page waits at
most `timeout` seconds for the data the page fetches to arrive in full (its body too, not just
its headers), and asking for more waits at most its `timeout`; Playwright keeps both limits even
when the page's own script is stuck. A few of Playwright's calls have no limit of their own
(reading a body Chromium already has, closing a tab), and a page whose script is stuck can hold
them, so a watchdog backs every call up: if one hasn't come back GRACE_SECONDS after its limit,
this browser's Chromium is killed, which ends the call with an error. That page counts as not
read, and the next page gets a fresh Chromium.

The browser is started when first needed (Playwright's Chromium, as for "browser" entries),
reused for every page (a new tab each, closed after), and closed by close() at the end of the run.

One rule follows: while this browser is open, job_monitor.fetch_browser (used by "browser" and
"eightfold" entries) can't work. Playwright's sync API can't start a second session in a thread
that already has one running, so fetch_browser's fails ("using Playwright Sync API inside the
asyncio loop") and its entry isn't read. So job_monitor._run reads Workday (the only user of this
browser) after every other target, and no such entry may come after it; a test
(test_no_browser_entry_is_read_after_the_workday_browser_opens) checks that order.
"""
import json
import logging
import os
import re
import signal
import subprocess
import threading
from contextlib import contextmanager
from urllib.parse import urlparse

from .common import HEADERS

log = logging.getLogger("job_monitor.browser")

GRACE_SECONDS = 15  # past a call's own time limit (or for a call without one) before the watchdog kills Chromium

# Run inside the page, again and again (Playwright's wait_for_function, which keeps its time limit
# even when the page's script is stuck): the first time, POST a JSON body to the page's own site
# (same origin, with the page's cookies) as the page's own code does (a GET when body is null);
# once the answer is in, return it as JSON text {status, text}.
_POST_JSON = """([url, body, headers, ms, key]) => {
  const box = window[key] || (window[key] = {});
  if (!box.started) {
    box.started = true;
    const stop = new AbortController();
    const timer = setTimeout(() => stop.abort(), ms);
    const how = body === null ? {method: "GET"} : {method: "POST", body: JSON.stringify(body)};
    fetch(url, {...how, headers, credentials: "same-origin", signal: stop.signal})
      .then(async resp => { box.done = JSON.stringify({status: resp.status, text: await resp.text()}); })
      .catch(err => { box.done = JSON.stringify({status: 0, text: String(err)}); })
      .finally(() => clearTimeout(timer));
  }
  return box.done || false;
}"""
# Headers of the page's own request that are copied when asking for more (the rest the browser sets).
_COPIED_HEADERS = re.compile(r"^(?:content-type|accept|accept-language|x-[a-z0-9-]+)$")


class Loaded:
    """What opening a page gave: status "ok" (data: the JSON of the first response the page got
    that was wanted; request_url and request_body: that request's URL and JSON body), or
    "challenge" (a bot check), "missing" (404), "unavailable" (another server error),
    "timeout" (nothing wanted arrived in time; went: where the page had gone, if it left the
    address opened) or "error"; detail says more ("429", "no answer within 15 s", ...)."""

    def __init__(self, status, detail="", data=None, request_url="", request_body=None, headers=None, page=None):
        self.status, self.detail, self.data = status, detail, data
        self.request_url, self.request_body, self.headers, self.page = request_url, request_body, headers or {}, page
        self.went = ""


def _says(exc) -> str:
    """An error in one short line: "TimeoutError: Timeout 15000ms exceeded"."""
    return f"{exc.__class__.__name__}: {(str(exc).strip().splitlines() or [''])[0][:200]}"


def _went(page, url: str) -> str:
    """Where the tab is ("host/path") if it has left url's host; "" if not, or if it can't be told.
    (page.url is what Playwright already knows: it doesn't ask the browser, so it can't hang.)"""
    try:
        now = urlparse(str(page.url or ""))
    except Exception:
        return ""
    if not now.netloc or now.netloc.lower() == urlparse(url).netloc.lower():
        return ""
    return (now.netloc + now.path)[:120]


def _is_timeout(exc) -> bool:
    return exc.__class__.__name__ == "TimeoutError" or "Timeout" in str(exc)[:200]


def _chromiums() -> set[int]:
    """The Chromium browser processes (not their helpers) running under this program, by process
    id; empty where that can't be told (no `ps`)."""
    try:
        listed = subprocess.run(["ps", "-A", "-ww", "-o", "pid=,ppid=,args="], capture_output=True, text=True,
                                timeout=10).stdout
    except Exception:
        return set()
    parents, found = {}, set()
    for line in listed.splitlines():
        bits = line.split(None, 2)
        if len(bits) == 3 and bits[0].isdigit() and bits[1].isdigit():
            parents[int(bits[0])] = int(bits[1])
            if re.search(r"chrom|headless.shell", bits[2], re.IGNORECASE) and "--type=" not in bits[2]:
                found.add(int(bits[0]))

    def ours(pid):
        for _ in range(64):
            pid = parents.get(pid, 0)
            if pid == os.getpid():
                return True
            if pid <= 1:
                return False
        return False
    return {pid for pid in found if ours(pid)}


def _kill(pids):
    """Kill these processes (the watchdog's last resort; it runs in the watchdog's thread)."""
    for pid in pids:
        try:
            os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        except OSError:
            pass


class _Watch:
    fired = ""  # why the watchdog killed Chromium during the watched call, once it has


class Browser:
    """A headless Chromium started when first needed. start: a function returning a started
    Playwright (tests pass a fake; by default Playwright's own)."""

    def __init__(self, start=None):
        self._start = start
        self._playwright = self._browser = self._context = None
        self._pids = set()  # this browser's Chromium processes, for the watchdog
        self._dead = False  # the watchdog killed it: the next page starts a fresh one
        self._asked = 0
        self.broken = None  # why the browser couldn't be started, once it couldn't (it isn't tried again)

    @contextmanager
    def _watch(self, seconds: float):
        """Kill this browser's Chromium if what runs inside hasn't come back within seconds: the
        stuck call then fails at once (watch.fired says why), and the next page gets a fresh one."""
        watch = _Watch()

        def fire():
            self._dead = True
            watch.fired = f"the browser didn't answer within {seconds:g} s, so it was stopped"
            log.warning(f"  [browser] {watch.fired}" + ("" if self._pids else " (or would have been: its processes weren't found)")
                        + "; a fresh one is started for the next page.")
            _kill(self._pids)

        timer = threading.Timer(seconds, fire)
        timer.daemon = True
        timer.start()
        try:
            yield watch
        finally:
            timer.cancel()

    def _tab(self):
        if self.broken:
            raise RuntimeError(self.broken)
        if self._dead:
            self._drop_chromium()
        if self._context is None:
            try:
                if self._playwright is None:
                    start = self._start
                    if start is None:
                        from playwright.sync_api import sync_playwright
                        start = lambda: sync_playwright().start()  # noqa: E731
                    self._playwright = start()
                before = _chromiums()
                self._browser = self._playwright.chromium.launch(headless=True)
                self._pids = _chromiums() - before
                self._context = self._browser.new_context(
                    user_agent=HEADERS["User-Agent"], viewport={"width": 1280, "height": 900},
                    locale="en-US")  # (as fetch_browser: Chromium's own "en-US@posix" upsets some sites)
            except Exception as exc:
                self.broken = f"the browser couldn't start ({_says(exc)})"
                self.close()
                raise RuntimeError(self.broken) from exc
        with self._watch(GRACE_SECONDS) as watch:
            page = self._context.new_page()
        if watch.fired:
            raise RuntimeError(watch.fired)
        return page

    def open(self, url: str, wanted, timeout: float) -> Loaded:
        """Open url in a new tab and wait, at most timeout seconds, for the first response the
        page gets (in full) whose request wanted(url, method) accepts. The tab stays open for
        post_json() when the status is "ok"; close it with done()."""
        try:
            page = self._tab()
        except Exception as exc:
            return Loaded("error", str(exc))
        with self._watch(timeout + GRACE_SECONDS) as watch:
            loaded = self._load(page, url, wanted, timeout)
        if watch.fired:
            loaded = Loaded("error", watch.fired, page=page)
        if loaded.status != "ok":
            self.done(loaded)
        return loaded

    def _load(self, page, url: str, wanted, timeout: float) -> Loaded:
        ms = max(1, int(timeout * 1000))
        bounce = None
        try:
            # "requestfinished", not "response": the answer's body has arrived too, within the same
            # time limit (a response comes with its headers, and its body could then take forever).
            with page.expect_event("requestfinished", lambda r: wanted(r.url, r.method), timeout=ms) as caught:
                doc = page.goto(url, wait_until="commit", timeout=ms)
                status = getattr(doc, "status", 200) if doc is not None else 200
                headers = (getattr(doc, "headers", None) or {}) if doc is not None else {}
                if headers.get("cf-mitigated") or status in (403, 429, 503):
                    bounce = Loaded("challenge", str(status) + (f", {headers['cf-mitigated']}" if headers.get("cf-mitigated") else ""),
                                    page=page)
                elif status == 404:
                    bounce = Loaded("missing", "404", page=page)
                elif status >= 500:
                    bounce = Loaded("unavailable", str(status), page=page)
                if bounce:
                    raise _Leave()  # (leaves without waiting for the response)
            resp = caught.value.response()
        except _Leave:
            return bounce
        except Exception as exc:
            if _is_timeout(exc):
                loaded = Loaded("timeout", f"no answer within {timeout:g} s", page=page)
                loaded.went = _went(page, url)
                return loaded
            return Loaded("error", _says(exc), page=page)
        if resp is None:
            return Loaded("error", "the request had no answer", page=page)
        loaded = Loaded("ok", page=page, request_url=resp.url)
        try:
            request = resp.request
            loaded.headers = {k: v for k, v in (request.headers or {}).items() if _COPIED_HEADERS.match(k.lower())}
            try:
                loaded.request_body = json.loads(request.post_data or "null")
            except (TypeError, ValueError):
                loaded.request_body = None
            if resp.status == 404:
                loaded.status, loaded.detail = "missing", "404"
            elif resp.status != 200:
                loaded.status, loaded.detail = ("challenge" if resp.status in (403, 429) else "error"), str(resp.status)
            else:
                loaded.data = json.loads(resp.text())
        except Exception as exc:
            loaded.status, loaded.detail = "error", _says(exc)
        return loaded

    def post_json(self, loaded: Loaded, url: str, body: dict | None, timeout: float):
        """POST body to url from inside the open tab (GET if body is None), waiting at most
        timeout seconds; (HTTP status or 0, the answer's JSON or None)."""
        self._asked += 1
        ms = max(1, int(timeout * 1000))
        headers = {"content-type": "application/json", "accept": "application/json", **loaded.headers}
        if body is None:
            headers = {k: v for k, v in headers.items() if k.lower() != "content-type"}
        with self._watch(timeout + GRACE_SECONDS):
            try:
                handle = loaded.page.wait_for_function(_POST_JSON, arg=[url, body, headers, ms, f"__jobMonitorAsk{self._asked}"],
                                                       polling=100, timeout=ms + 1000)
                answer = json.loads(handle.json_value())
                status = int(answer.get("status") or 0)
                return status, (json.loads(answer.get("text") or "null") if status == 200 else None)
            except Exception as exc:
                log.debug(f"  [browser] asking for more from the page failed: {_says(exc)}")
                return 0, None

    def get_json(self, loaded: Loaded, url: str, timeout: float):
        """GET url from inside the open tab: like post_json."""
        return self.post_json(loaded, url, None, timeout)

    def done(self, loaded: Loaded):
        """Close the tab a Loaded holds (if any)."""
        page, loaded.page = loaded.page, None
        if page is not None:
            with self._watch(GRACE_SECONDS):
                try:
                    page.close()
                except Exception:
                    pass

    def _drop_chromium(self):
        """Close this browser's Chromium (if it was started), keeping Playwright for a fresh one."""
        for name in ("_context", "_browser"):
            thing = getattr(self, name)
            setattr(self, name, None)
            if thing is not None:
                with self._watch(GRACE_SECONDS):
                    try:
                        thing.close()
                    except Exception:
                        pass
        self._pids, self._dead = set(), False

    def close(self):
        """Close the browser (if it was started); safe to call again."""
        self._drop_chromium()
        playwright, self._playwright = self._playwright, None
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:
                pass


class _Leave(Exception):
    """Leaving the wait for the page's response early (the page is a bot check, or gone)."""
