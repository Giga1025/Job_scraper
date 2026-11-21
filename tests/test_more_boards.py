"""Tests of boards/jibe.py, boards/rippling.py and boards/gem.py's own helpers (how they're used in a
run: JibeRipplingGemBoardTests in test_job_monitor.py)."""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_DIR))

from boards import gem, jibe, rippling  # noqa: E402

COUNTRY_NAMES = {"US": "United States", "IN": "India", "CA": "Canada", "GB": "United Kingdom", "MY": "Malaysia"}


def jibe_item(number, title="Software Engineer", posted="2026-11-20T21:14:00+0000", country="US", others=(),
              client="acme", canonical=None, apply_host="careers-acme.icims.com", country_name="", created=None):
    """One posting as a Jibe site's API gives it (a country's name from its code unless given; None: no name)."""
    return {"data": {"title": title, "req_id": str(number), "slug": str(number), "posted_date": posted, "create_date": created,
                     "country_code": country, "country": COUNTRY_NAMES.get(country) if country_name == "" else country_name,
                     "additional_locations": [{"country_code": c, "country": COUNTRY_NAMES.get(c), "city": f"City {c}"}
                                              for c in others] or None,
                     "full_location": f"Somewhere, {country}", "apply_url": f"https://{apply_host}/jobs/{number}/login",
                     "client_code": client, "language": "en-us", "meta_data": {"canonical_url": canonical}}}


class JibeTests(unittest.TestCase):
    def setUp(self):
        jibe.learn({})
        self.addCleanup(jibe.learn, {})

    def test_links_and_the_api(self):
        api = "https://careers.acme.com/api/jobs?page=1&limit=100&sortBy=posted_date&descending=true&internal=false"
        self.assertEqual(jibe.api_url("careers.acme.com"), api)
        self.assertIn("page=3&", jibe.api_url("careers.acme.com", 3))
        for link in ("https://careers.acme.com/jobs/85806?icims=1", "https://careers.acme.com/careers-home/jobs/85806?lang=en-us&icims=1",
                     "https://acme.jibeapply.com/jobs/85806", "https://acme.jibeapply.com/jobs/85806/", api,
                     "https://careers.acme.com/jobs?icims=1", "https://careers.acme.com/careers-home/jobs?icims=1",
                     "https://acme.jibeapply.com/jobs", "https://acme.jibeapply.com/careers-home/jobs/"):  # (and its search page)
            self.assertEqual(jibe.site_of(link), link.split("/")[2], link)
        # A company's own domain counts only with icims=1 (or once it's known as a Jibe site); never a classic portal.
        for other in ("https://careers.acme.com/jobs/85806", "https://careers.acme.com/jobs/85806?icims=0",
                      "https://careers-acme.icims.com/jobs/85806/job?icims=1", "https://careers.acme.com/api/jobs?page=1",
                      "https://careers.acme.com/jobs/abc?icims=1", "https://careers.acme.com/careers-home/jobs",
                      "https://careers.acme.com/jobs/85806/apply?icims=1", "https://acme.jibeapply.com/about", "not a link", None):
            self.assertIsNone(jibe.site_of(other), other)
        jibe.learn({"a": {"url": api}})
        self.assertEqual(jibe.site_of("https://careers.acme.com/careers-home/jobs"), "careers.acme.com")  # (known now)
        self.assertIsNone(jibe.job_key("https://careers.acme.com/careers-home/jobs"))  # (a page, not a posting)
        self.assertTrue(jibe.is_api_url(api))
        self.assertFalse(jibe.is_api_url("https://careers.acme.com/api/jobs?page=1"))
        self.assertEqual(jibe.page_url(api), "https://careers.acme.com/jobs")

    def test_a_posting_is_known_by_its_customer_on_any_of_its_addresses(self):
        self.assertEqual(jibe.job_key("https://careers.acme.com/jobs/77?icims=1"), ("jibe", "careers.acme.com", "77"))
        self.assertIsNone(jibe.job_key("https://careers.acme.com/careers-home/jobs/77"))  # (not known as a Jibe site yet)
        self.assertIsNone(jibe.job_key("https://careers-acme.icims.com/jobs/77/job"))
        api = jibe.api_url("careers.acme.com")
        other = jibe.api_url("acme.jibeapply.com")
        known = {"a": {"url": api, "name": "Acme"}, "b": {"url": other, "name": "Acme"}, "c": {"url": "https://x.com/y"}}
        jobs = jibe.jobs_from({"jobs": [jibe_item(77), jibe_item(78, apply_host="globalcareers-acme.icims.com")]}, "careers.acme.com")
        jibe.learn(known, {api: ("ok", jobs, None), other: ("failed", None, None)})
        self.assertEqual(known["a"]["client"], "acme")
        self.assertEqual(known["a"]["portals"], ["careers-acme.icims.com", "globalcareers-acme.icims.com"])
        self.assertNotIn("client", known["b"])
        for link in ("https://careers.acme.com/jobs/77?lang=en-us", "https://careers.acme.com/careers-home/jobs/77",
                     "https://careers-acme.icims.com/jobs/77/software-engineer/job", "https://globalcareers-acme.icims.com/jobs/77/login"):
            self.assertEqual(jibe.job_key(link), ("jibe", "acme", "77"), link)
        # The other address isn't matched to the customer until it's read; then it's the same job, and only one is read.
        self.assertEqual(jibe.job_key("https://acme.jibeapply.com/jobs/77"), ("jibe", "acme.jibeapply.com", "77"))
        self.assertEqual(jibe.same_client(known), set())
        jibe.learn(known, {other: ("ok", jobs, None)})
        self.assertEqual(jibe.job_key("https://acme.jibeapply.com/jobs/77"), ("jibe", "acme", "77"))
        self.assertEqual(jibe.same_client(known), {"b"})
        # What was learned stays in the registry (the next run starts from it), and a failed read forgets nothing.
        jibe.learn({}, None)
        self.assertIsNone(jibe.job_key("https://careers-acme.icims.com/jobs/77/job"))
        jibe.learn(known, {api: ("failed", None, None)})
        self.assertEqual(jibe.job_key("https://careers-acme.icims.com/jobs/77/job"), ("jibe", "acme", "77"))

    def test_a_site_linking_its_postings_on_another_domain(self):
        # Schneider Electric's se.jibeapply.com links its postings on careers.se.com: the same job on either.
        api = jibe.api_url("se.jibeapply.com")
        known = {"se": {"url": api, "name": "Schneider Electric"}}
        jobs = jibe.jobs_from({"jobs": [jibe_item(5, client="se", canonical="https://careers.se.com/jobs/5?lang=en-us")]},
                              "se.jibeapply.com")
        self.assertEqual(jobs[0]["url"], "https://se.jibeapply.com/jobs/5?lang=en-us")  # (the site's own link: it opens the posting)
        self.assertEqual(jobs[0]["named_hosts"], ["careers.se.com"])
        self.assertIsNone(jibe.job_key("https://careers.se.com/jobs/5?lang=en-us"))  # (not learned yet)
        jibe.learn(known, {api: ("ok", jobs, None)})
        self.assertEqual(known["se"]["aliases"], ["careers.se.com"])
        self.assertEqual(jibe.addresses(known["se"], {"se.jibeapply.com"}), {"se.jibeapply.com", "careers.se.com", "careers-acme.icims.com"})
        for link in (jobs[0]["url"], "https://careers.se.com/jobs/5?lang=en-us", "https://careers.se.com/careers-home/jobs/5"):
            self.assertEqual(jibe.job_key(link), ("jibe", "se", "5"), link)
        self.assertIsNone(jibe.site_of("https://careers.se.com/jobs/6"))  # (a domain for keys, not a site to read)
        self.assertIsNone(jibe.job_key("https://careers.se.com/about"))

    def test_a_domain_that_is_another_sites_isnt_learned_as_this_ones(self):
        # H-E-B's careers.heb.com names careers.hrblock.com (H&R Block's own site) for its postings, where they aren't found.
        heb, hrb = jibe.api_url("careers.heb.com"), jibe.api_url("careers.hrblock.com")
        known = {"heb": {"url": heb, "name": "H-E-B", "aliases": ["careers.hrblock.com"]}, "hrb": {"url": hrb, "name": "H&R Block"}}
        jobs = jibe.jobs_from({"jobs": [jibe_item(254650, client="heb", canonical="https://careers.hrblock.com/jobs/254650?lang=en-us",
                                                  apply_host="jobs-heb.icims.com")]}, "careers.heb.com")
        self.assertEqual(jobs[0]["url"], "https://careers.heb.com/jobs/254650?lang=en-us")  # (the link that opens it)
        jibe.learn(known, {heb: ("ok", jobs, None), hrb: ("ok", jibe.jobs_from({"jobs": [jibe_item(76987, client="hrblock")]},
                                                                                 "careers.hrblock.com"), None)})
        self.assertNotIn("aliases", known["heb"])  # (the one saved earlier is dropped too)
        self.assertEqual(jibe.job_key("https://careers.heb.com/jobs/254650?icims=1"), ("jibe", "heb", "254650"))
        self.assertEqual(jibe.job_key("https://careers.hrblock.com/jobs/254650"), ("jibe", "hrblock", "254650"))
        self.assertEqual(jibe.addresses(known["heb"], {"careers.heb.com", "careers.hrblock.com"}), {"careers.heb.com", "jobs-heb.icims.com"})
        # A canonical link that isn't a posting's page isn't a domain for its postings either.
        odd = jibe.jobs_from({"jobs": [jibe_item(5, canonical="https://elsewhere.com/careers-home")]}, "careers.acme.com")
        self.assertEqual((odd[0]["url"], odd[0]["named_hosts"]), ("https://careers.acme.com/jobs/5?lang=en-us", []))

    def test_one_site_per_customer_one_that_can_be_read(self):
        sites = {k: jibe.api_url(h) for k, h in (("a", "acme.jibeapply.com"), ("b", "careers.acme.com"), ("c", "jobs.acme.com"))}
        known = {k: {"url": u, "name": "Acme", "client": "acme", "last_read": "ok"} for k, u in sites.items()}
        known["z"] = {"url": jibe.api_url("other.jibeapply.com"), "client": "other"}
        self.assertEqual(jibe.same_client(known), {"b", "c"})  # (the first by key)
        self.assertEqual(jibe.same_client(known, skipped={"a"}), {"a", "c"})  # (skipped for another reason: the next)
        self.assertEqual(jibe.same_client(known, missing={sites["a"], sites["b"]}), {"a", "b"})  # ("not found" at its last try)
        self.assertEqual(jibe.same_client(known, skipped={"a", "b", "c"}), set())  # (none can be read: nothing more)
        self.assertEqual(jibe.same_client(known, missing=set(sites.values())), set())  # (all tried again)
        # A site whose last read failed gives way to the next whose didn't.
        api_a, api_b = sites["a"], sites["b"]
        jibe.learn(known, {api_a: ("failed", None, None)})
        self.assertEqual(known["a"]["last_read"], "failed")
        self.assertEqual(jibe.same_client(known), {"a", "c"})
        jibe.learn(known, {api_b: ("unchanged", None, None)})
        self.assertEqual((known["b"]["last_read"], jibe.same_client(known)), ("ok", {"a", "c"}))
        jibe.learn(known, {api_b: ("missing", None, None)})
        self.assertEqual(jibe.same_client(known), {"a", "b"})
        jibe.learn(known, {sites["c"]: ("failed", None, None)})
        self.assertEqual(jibe.same_client(known), {"b", "c"})  # (all failed: the first)
        jibe.learn(known, {api_a: ("ok", [], None)})
        self.assertEqual((known["a"]["last_read"], jibe.same_client(known)), ("ok", {"b", "c"}))

    def test_odd_registry_entries_dont_stop_it(self):
        known = {"a": {"url": jibe.api_url("careers.acme.com"), "client": "acme", "portals": "careers-acme.icims.com",
                       "aliases": [None, 5, "careers.acme.org"]},
                 "b": {"url": ["not", "a", "string"]}, "c": None, "d": {"url": None}}
        jibe.learn(known, {"https://elsewhere.com/api/jobs?sortBy=posted_date": ("ok", [], None)})
        self.assertEqual(jibe.job_key("https://careers.acme.org/jobs/9"), ("jibe", "acme", "9"))
        self.assertIsNone(jibe.job_key("https://careers-acme.icims.com/jobs/9/job"))  # (portals isn't a list: ignored)
        self.assertEqual(jibe.same_client(known), set())

    def test_postings_are_read_with_every_country_and_their_date(self):
        data = {"jobs": [jibe_item(1, "Software Engineer I", country="IN", others=("US",)),
                         jibe_item(6, "Embedded Software Engineer", country="FM", country_name="United States"),  # (Viasat's)
                         jibe_item(7, "Policy Counsel", country="ZA", country_name=None),  # (Viasat's "Remote - Virginia": no name)
                         jibe_item(10, "Firmware Engineer", country="IN", others=("ZZ",)),  # (one of its places names no country)
                         jibe_item(8, "Network Engineer", country="PR", country_name="Puerto Rico"),
                         jibe_item(9, "Bulk-loaded Engineer", posted="2026-09-01T10:00:00+0000", created="2026-11-19T08:00:00Z"),
                         jibe_item(2, "Data Engineer", posted="November 20, 2026", canonical="https://careers.acme.com/jobs/2?lang=fr-fr"),
                         jibe_item(3, " "), {"data": None}, "junk",
                         jibe_item("", "No id"), jibe_item(4, "Late", posted="soon", apply_host="apply.example.com")],
                "totalCount": 9}
        jobs = jibe.jobs_from(data, "careers.acme.com")
        self.assertEqual([(j["title"], j["url"], j["countries"], j.get("posted")) for j in jobs], [
            ("Software Engineer I", "https://careers.acme.com/jobs/1?lang=en-us", ["India", "United States"], "2026-11-20T21:14:00+00:00"),
            ("Embedded Software Engineer", "https://careers.acme.com/jobs/6?lang=en-us", ["United States"], "2026-11-20T21:14:00+00:00"),
            ("Policy Counsel", "https://careers.acme.com/jobs/7?lang=en-us", [], "2026-11-20T21:14:00+00:00"),
            ("Firmware Engineer", "https://careers.acme.com/jobs/10?lang=en-us", [], "2026-11-20T21:14:00+00:00"),
            ("Network Engineer", "https://careers.acme.com/jobs/8?lang=en-us", ["United States"], "2026-11-20T21:14:00+00:00"),
            # (it reached the site long after its posting date: it went up then; the API's order is by the posting date)
            ("Bulk-loaded Engineer", "https://careers.acme.com/jobs/9?lang=en-us", ["United States"], "2026-11-19T08:00:00+00:00"),
            ("Data Engineer", "https://careers.acme.com/jobs/2?lang=fr-fr", ["United States"], "2026-11-20T00:00:00+00:00"),
            ("Late", "https://careers.acme.com/jobs/4?lang=en-us", ["United States"], None)])
        self.assertEqual(jobs[5]["listed"], "2026-09-01T10:00:00+00:00")
        self.assertEqual(jobs[0]["location"], "Somewhere, IN; City US, United States")
        self.assertEqual(jobs[3]["location"], "Somewhere, IN; City ZZ")
        self.assertEqual([j["apply_hosts"] for j in jobs], [["careers-acme.icims.com"]] * 7 + [[]])
        self.assertEqual(jobs[0]["client"], "acme")
        self.assertEqual(jibe.jobs_from({"jobs": None}, "x"), [])
        self.assertEqual(jibe.jobs_from(None, "x"), [])

    def test_the_next_page_is_read_only_for_a_busy_site(self):
        now = datetime.now(timezone.utc)
        full = [{"listed": (now - timedelta(hours=1)).isoformat()}] * jibe.LIMIT
        old = full[:-1] + [{"listed": (now - timedelta(days=jibe.DEEP_DAYS + 1)).isoformat()}]
        self.assertTrue(jibe.read_further(full, jibe.LIMIT))
        self.assertFalse(jibe.read_further(full, jibe.LIMIT - 1))  # (the last page)
        self.assertFalse(jibe.read_further(old, jibe.LIMIT))
        self.assertTrue(jibe.read_further(old, jibe.LIMIT, since=now - timedelta(days=jibe.DEEP_DAYS + 2)))  # (after an outage)
        self.assertFalse(jibe.read_further(full[:-1] + [{}], jibe.LIMIT))
        self.assertFalse(jibe.read_further(full[:-1] + [{"listed": "soon"}], jibe.LIMIT))
        self.assertFalse(jibe.read_further([], jibe.LIMIT))
        self.assertTrue(jibe.read_further(full[:-1] + [{"listed": now.replace(tzinfo=None).isoformat()}], jibe.LIMIT))

    def test_a_site_that_cant_list_newest_first_is_read_in_its_own_order(self):
        def page(numbers, posted):
            return {"jobs": [jibe_item(n, posted=p) for n, p in zip(numbers, posted)], "totalCount": 250}
        day = lambda d: (datetime.now(timezone.utc) - timedelta(days=d)).strftime("%Y-%m-%dT%H:%M:00+0000")
        # Bowman: the sort is ignored (oldest first); its own order is newest first.
        oldest = page(range(100), [day(300 - n) for n in range(100)])
        own = {1: page(range(1000, 1100), [day(n / 50) for n in range(100)]), 2: page(range(2000, 2100), [day(5)] * 100),
               3: page(range(3000, 3050), [day(9)] * 50)}
        asked = []

        def bowman(url, params=None):
            asked.append(url)
            n = int(url.split("page=")[1].split("&")[0])
            return oldest if "sortBy" in url else own.get(n)
        self.assertEqual(len(jibe.read_site("careers.bowman.com", bowman)), 250)
        self.assertEqual(asked, [jibe.api_url("careers.bowman.com")] + [jibe.unsorted_url("careers.bowman.com", n) for n in (1, 2, 3)])
        known = {"b": {"url": jibe.api_url("careers.bowman.com")}}
        jibe.learn(known, {known["b"]["url"]: ("ok", [], None)})
        self.assertIs(known["b"]["unsorted"], True)
        asked.clear()
        jibe.read_site("careers.bowman.com", bowman)  # (from then on in its own order at once: its ETag is that page's)
        self.assertEqual(asked[0], jibe.unsorted_url("careers.bowman.com"))
        # AVI-SPL: the sort is refused; read in its own order every time (it may be a passing error), up to MAX_PAGES.
        asked.clear()
        refused = lambda url, params=None: (asked.append(url), None if "sortBy" in url else own.get(int(url.split("page=")[1].split("&")[0])))[1]
        self.assertEqual(len(jibe.read_site("careers.avispl.com", refused)), 250)
        jibe.learn({"a": {"url": jibe.api_url("careers.avispl.com")}}, {jibe.api_url("careers.avispl.com"): ("ok", [], None)})
        self.assertNotIn("careers.avispl.com", jibe._UNSORTED)
        # Unchanged since the last read (304): nothing more is asked.
        asked.clear()

        def unchanged(url, params=None):
            asked.append(url)
            return None
        unchanged.last_status = 304
        self.assertIsNone(jibe.read_site("careers.acme.com", unchanged))
        self.assertEqual(asked, [jibe.api_url("careers.acme.com")])

    def test_after_an_outage_a_site_is_read_back_to_its_last_good_read(self):
        day = lambda d: (datetime.now(timezone.utc) - timedelta(days=d)).strftime("%Y-%m-%dT%H:%M:00+0000")
        pages = {n: {"jobs": [jibe_item(n * 1000 + i, posted=day(n * 2 - 1 + i / 100)) for i in range(100)]} for n in (1, 2, 3, 4)}
        get = lambda url, params=None: pages[int(url.split("page=")[1].split("&")[0])]
        self.assertEqual(len(jibe.read_site("careers.acme.com", get)), 200)  # (back DEEP_DAYS days)
        known = {"a": {"url": jibe.api_url("careers.acme.com"), "read_at": (datetime.now(timezone.utc) - timedelta(days=6)).isoformat()}}
        jibe.learn(known)
        self.assertEqual(len(jibe.read_site("careers.acme.com", get)), 400)  # (back to a day before its last good read)
        jibe.learn(known, {known["a"]["url"]: ("unchanged", None, None)})
        self.assertLess(datetime.now(timezone.utc) - datetime.fromisoformat(known["a"]["read_at"]), timedelta(minutes=1))


class RipplingTests(unittest.TestCase):
    def test_links_and_the_api(self):
        api = "https://ats.rippling.com/api/v2/board/Acme-Careers/jobs?page=0&pageSize=1000&groupJobsByLocation=true"
        uuid = "84d388b6-7656-434c-8862-0312eb6b97ac"
        for link in (f"https://ats.rippling.com/Acme-Careers/jobs/{uuid}", "https://ats.rippling.com/Acme-Careers/jobs",
                     f"https://ats.rippling.com/en-GB/Acme-Careers/jobs/{uuid}/apply", "https://ats.rippling.com/embed/Acme-Careers/jobs",
                     f"https://ATS.rippling.com/fr/Acme-Careers/jobs/{uuid}?src=x", api):
            self.assertEqual(rippling.board_of(link), "Acme-Careers", link)  # (case kept: the API needs it)
        for other in ("https://ats.rippling.com/", "https://ats.rippling.com/Acme/about", "https://ats.rippling.com/api/jobs",
                      f"https://example.com/Acme/jobs/{uuid}", "https://ats.rippling.com/Acme/jobs/123", None, "::"):
            self.assertIsNone(rippling.board_of(other), other)
        self.assertEqual(rippling.api_url("Acme-Careers"), api)
        self.assertIn("page=2&", rippling.api_url("Acme-Careers", 2))
        self.assertTrue(rippling.is_api_url(api))
        self.assertFalse(rippling.is_api_url("https://ats.rippling.com/Acme-Careers/jobs"))
        self.assertEqual(rippling.page_url(api), "https://ats.rippling.com/Acme-Careers/jobs")

    def test_postings_are_read_with_every_place(self):
        data = {"items": [{"id": "1", "name": " Software Engineer ", "url": "https://ats.rippling.com/acme/jobs/1",
                           "locations": [{"name": "New York, NY", "countryCode": "US"}, {"name": "Toronto, ON", "countryCode": "CA"},
                                         {"name": "New York, NY", "countryCode": "US"}]},
                          {"id": "2", "name": "Remote role", "url": "https://ats.rippling.com/acme/jobs/2", "locations": None},
                          {"id": "3", "name": "", "url": "https://ats.rippling.com/acme/jobs/3"}, {"name": "No link"}, None],
                "totalPages": "3"}
        jobs, pages = rippling.jobs_from(data)
        self.assertEqual(pages, 3)
        self.assertEqual(jobs, [{"title": "Software Engineer", "url": "https://ats.rippling.com/acme/jobs/1",
                                 "location": "New York, NY; Toronto, ON", "countries": ["US", "CA"]},
                                {"title": "Remote role", "url": "https://ats.rippling.com/acme/jobs/2", "location": "", "countries": []}])
        self.assertEqual(rippling.jobs_from(None), ([], 1))
        self.assertEqual(rippling.jobs_from({"items": [], "totalPages": "x"}), ([], 1))


class GemTests(unittest.TestCase):
    def test_links_and_the_api(self):
        post = "https://jobs.gem.com/Nuvo/am9icG9zdDokPSI5BRzuYHmUj-Ig7JJz"
        for link in (post, post + "/application", "https://jobs.gem.com/Nuvo", "https://jobs.gem.com/Nuvo/",
                     "https://api.gem.com/job_board/v0/Nuvo/job_posts/"):
            self.assertEqual(gem.board_of(link), "Nuvo", link)
        for other in ("https://jobs.gem.com/", "https://jobs.gem.com/api/x", "https://example.com/Nuvo",
                      "https://jobs.gem.com/Nuvo/a/b/c", "https://api.gem.com/Nuvo", None):
            self.assertIsNone(gem.board_of(other), other)
        self.assertEqual(gem.job_key(post), ("gem", "nuvo", "am9icG9zdDokPSI5BRzuYHmUj-Ig7JJz"))
        self.assertEqual(gem.job_key(post.replace("Nuvo", "nuvo")), gem.job_key(post))
        self.assertNotEqual(gem.job_key(post.lower()), gem.job_key(post))  # (the posting id's case matters)
        self.assertIsNone(gem.job_key("https://jobs.gem.com/Nuvo"))
        self.assertEqual(gem.api_url("Nuvo"), "https://api.gem.com/job_board/v0/Nuvo/job_posts/")
        self.assertTrue(gem.is_api_url(gem.api_url("Nuvo")))
        self.assertFalse(gem.is_api_url(post))
        self.assertEqual(gem.page_url(gem.api_url("Nuvo")), "https://jobs.gem.com/Nuvo")

    def test_postings_are_read_with_their_countries(self):
        def post(n, offices, **extra):
            return {"id": str(n), "title": f"Role {n}", "absolute_url": f"https://jobs.gem.com/acme/{n}",
                    "first_published_at": "2026-11-20T21:06:10.407Z", "created_at": "2026-11-01T00:00:00Z",
                    "offices": [{"name": "x", "location": {"name": o}} for o in offices], **extra}
        data = [post(1, ["Toronto, Canada", "United States - Remote"]),
                post(2, ["Remote", "London, United Kingdom"]),  # ("Remote" names no country: the places decide)
                post(3, [], location={"name": "Philadelphia, United States"}, first_published_at=None),
                post(4, ["Taiwan, Province of China - Remote"]), {"title": "No link"}, post(5, [], title=" "), "junk"]
        jobs = gem.jobs_from(data)
        self.assertEqual([(j["title"], j["location"], j["countries"], j.get("posted")) for j in jobs], [
            ("Role 1", "Toronto, Canada; United States - Remote", ["Canada", "United States"], "2026-11-20T21:06:10+00:00"),
            ("Role 2", "Remote; London, United Kingdom", [], "2026-11-20T21:06:10+00:00"),
            ("Role 3", "Philadelphia, United States", ["United States"], "2026-11-01T00:00:00+00:00"),
            ("Role 4", "Taiwan, Province of China - Remote", ["Taiwan, Province of China"], "2026-11-20T21:06:10+00:00")])
        self.assertEqual(gem.jobs_from({"not": "a list"}), [])


if __name__ == "__main__":
    unittest.main()
