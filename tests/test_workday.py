"""Tests of boards/workday.py's own helpers (how it's used in a run: the Workday*Tests in test_job_monitor.py)."""
import json
import random
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_DIR))

from boards import browser, workday  # noqa: E402

NOW = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)


def shipped_entries():
    """Your Workday entries in targets/big_tech.json: {name: link}."""
    entries = json.loads((REPO_DIR / "targets" / "big_tech.json").read_text())
    entries = entries["targets"] if isinstance(entries, dict) else entries
    return {e["name"]: e["url"] for e in entries if isinstance(e, dict) and workday.site_of(e.get("url"))}


class LinkTests(unittest.TestCase):
    def test_a_posting_is_the_same_whatever_form_its_link_takes(self):
        base = "https://salesforce.wd12.myworkdayjobs.com/en-US/External_Career_Site/job/Washington---Seattle/Engineer_JR368871-1"
        for other in (base + "?CF_-_REC_-_LRV_-_Job_Posting_Anchor_-_Country=bc33", base.replace("/en-US", ""),
                      base.replace("/en-US", "/fr-CA").upper().replace("HTTPS", "https"), base + "/"):
            self.assertEqual(workday.posting_key(other), workday.posting_key(base), other)
        for other in (base.replace("-1", "-2"), base.replace("Seattle", "Bellevue"), base.replace("wd12", "wd1")):
            self.assertNotEqual(workday.posting_key(other), workday.posting_key(base), other)

    def test_dates(self):
        self.assertEqual([workday.posted(t, NOW) for t in ("Posted Today", "Posted Yesterday", "Posted 3 Days Ago",
                                                            "Posted 30+ Days Ago", "Recently", None)],
                         [NOW, datetime(2026, 11, 18, 12, tzinfo=timezone.utc), datetime(2026, 11, 16, 12, tzinfo=timezone.utc),
                          datetime(2026, 10, 20, 12, tzinfo=timezone.utc), None, None])

    def test_the_shipped_entries_are_read_with_their_filters(self):
        entries = shipped_entries()
        self.assertEqual(sorted(entries), ["Circle (USDC)", "Intel", "NVIDIA", "Salesforce"])
        facets, words, prefix, page = workday.search_of(entries["NVIDIA"])
        self.assertEqual((facets, words), ({"locationHierarchy1": ["2fcb99c455831013ea52fb338f2932d8"],
                                            "workerSubType": ["ab40a98049581037a3ada55b087049b7"]}, ""))
        self.assertEqual(prefix, "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite")
        self.assertNotIn("redirect", page)  # (a link to the sign-in page, not a filter)
        self.assertTrue(page.startswith(prefix + "?locationHierarchy1="))
        facets, _, prefix, page = workday.search_of(entries["Intel"])
        self.assertEqual((len(facets["locations"]), prefix), (6, "https://intel.wd1.myworkdayjobs.com/en-US/External"))
        self.assertEqual(page, prefix + "?" + entries["Intel"].split("?", 1)[1])
        self.assertEqual(workday.search_of(entries["Circle (USDC)"]),
                         ({}, "", "https://circle.wd1.myworkdayjobs.com/en-US/Circle", "https://circle.wd1.myworkdayjobs.com/en-US/Circle"))
        self.assertEqual(workday.api_of(workday.site_of(entries["Salesforce"]))[0],
                         "https://salesforce.wd12.myworkdayjobs.com/wday/cxs/salesforce/External_Career_Site/jobs")


class NewestPagesTests(unittest.TestCase):
    PREFIX = "https://acme.wd5.myworkdayjobs.com/en-US/External"

    def page(self, *posts):
        return {"total": 99, "jobPostings": [{"title": f"Engineer {i}", "externalPath": f"/job/NY/Engineer_R{i}",
                                              "postedOn": when, "locationsText": "New York, NY"} for i, when in posts]}

    def test_a_page_that_isnt_a_job_list(self):
        self.assertIsNone(workday.newest({"error": "x"}, None, self.PREFIX))
        self.assertIsNone(workday.newest(None, None, self.PREFIX))
        # A later page that fails keeps what was read.
        jobs = workday.newest(self.page(*[(i, "Posted Today") for i in range(20)]), lambda offset: None, self.PREFIX, now=NOW)
        self.assertEqual(len(jobs), 20)

    def test_postings_without_a_title_or_link_are_left_out_and_repeats_dropped(self):
        first = self.page((1, "Posted Today"), (1, "Posted Today"))
        first["jobPostings"] += [{"title": "", "externalPath": "/job/x_R2"}, {"title": "No link"}, "junk"]
        jobs = workday.newest(first, None, self.PREFIX, now=NOW)
        self.assertEqual([(j["title"], j["url"], j["location"], j["posted"]) for j in jobs],
                         [("Engineer 1", self.PREFIX + "/job/NY/Engineer_R1", "New York, NY", "2026-11-19T12:00:00+00:00")])

    def test_reading_stops_at_the_total(self):
        asked = []
        first = self.page(*[(i, "Posted Today") for i in range(20)])
        first["total"] = 20
        workday.newest(first, lambda offset: asked.append(offset), self.PREFIX, now=NOW)
        self.assertEqual(asked, [])


class BrowserProcessTests(unittest.TestCase):
    """The watchdog in boards/browser.py kills only the Chromium this program started."""

    def test_only_this_programs_chromium_is_found(self):
        listed = "\n".join([
            "    1     0 /sbin/init",
            "  100     1 python job_monitor.py",
            "  101   100 /ms-playwright/driver/node cli.js run-driver",
            "  102   101 /ms-playwright/chromium_headless_shell-1194/chrome-linux/headless_shell --headless --remote-debugging-pipe",
            "  103   102 /ms-playwright/chromium_headless_shell-1194/chrome-linux/headless_shell --type=renderer",
            "  104   102 /ms-playwright/chromium_headless_shell-1194/chrome-linux/headless_shell --type=zygote",
            "  105   101 /ms-playwright/chromium-1243/chrome-linux64/chrome --headless",
            "  200     1 /opt/google/chrome/chrome",  # (someone else's)
            "  201   200 /opt/google/chrome/chrome --type=renderer",
            "  junk",
        ])
        with mock.patch.object(browser.subprocess, "run", return_value=mock.Mock(stdout=listed)), \
                mock.patch.object(browser.os, "getpid", return_value=100):
            self.assertEqual(browser._chromiums(), {102, 105})
        with mock.patch.object(browser.subprocess, "run", side_effect=FileNotFoundError("ps")):
            self.assertEqual(browser._chromiums(), set())  # (then the watchdog can only say so)


class LastingBlockTests(unittest.TestCase):
    """Runs about every 30 minutes (GitHub's schedule isn't exact: 10 to 90 minutes apart here) for
    days while no company Workday site can be read (the real order(), read_sites() and note(); a
    fake browser and a fake clock, no network)."""

    SITES = 24

    class FakeBrowser:
        broken = None

        def __init__(self, answer, loads):
            self.answer, self.loads = answer, loads

        def open(self, url, wanted, timeout):
            self.loads.append(url)
            return browser.Loaded(self.answer, "403, challenge" if self.answer == "challenge" else "500")

        def done(self, loaded):
            pass

        def close(self):
            pass

    def simulate(self, answer, days):
        """The pages loaded in each run of each day."""
        sites = [{"url": f"https://s{i:02d}.wd1.myworkdayjobs.com/Careers", "name": f"S{i}", "_board_key": f"k{i:02d}"}
                 for i in range(self.SITES)]
        known = {s["_board_key"]: {"added": "2026-11-01T00:00:00"} for s in sites}
        start = datetime(2026, 11, 20)
        clock = [start]
        gaps = random.Random(7)

        def hours_since(stamp):
            return (clock[0] - datetime.fromisoformat(stamp)).total_seconds() / 3600 if stamp else float("inf")

        out = [[] for _ in range(days)]
        with mock.patch.object(workday, "hours_since", hours_since), mock.patch.object(workday, "log"):
            while clock[0] < start + timedelta(days=days):
                loads = []
                reader = workday.Reader(pause=0, browser=self.FakeBrowser(answer, loads))
                reader.api_refused = True
                ordered = workday.order(sites, known, {})
                results, _ = workday.read_sites(reader, ordered, {}, 6, known)
                workday.note(known, ordered, results, clock[0].isoformat(timespec="seconds"))
                out[(clock[0] - start).days].append(loads)
                clock[0] += timedelta(minutes=gaps.randint(10, 90))
        self.assertTrue(all(workday.failing(info) for info in known.values()))  # (every site failed its last tries)
        return out

    def test_bot_checks_everywhere_still_stop_each_run_after_three(self):
        days = self.simulate("challenge", 8)
        self.assertEqual(max(len(loads) for day in days for loads in day), workday.MAX_SKIPPED_IN_A_ROW)

    def test_a_site_that_keeps_failing_is_tried_at_most_once_a_day(self):
        days = self.simulate("unavailable", 8)  # (server errors: they don't count once a site is known to fail)
        for day in days[1:]:
            tried = [url for loads in day for url in loads]
            self.assertTrue(tried)
            self.assertEqual(sorted(tried), sorted(set(tried)))  # (no site twice in a day)


if __name__ == "__main__":
    unittest.main()
