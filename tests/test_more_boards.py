"""Tests of boards/jibe.py, boards/rippling.py and boards/gem.py's own helpers (how they're used in a
run: JibeRipplingGemBoardTests in test_job_monitor.py)."""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_DIR))

from boards import gem, jibe, rippling  # noqa: E402


def jibe_item(number, title="Software Engineer", posted="2026-11-20T21:14:00+0000", country="US", others=(),
              client="acme", canonical=None, apply_host="careers-acme.icims.com"):
    """One posting as a Jibe site's API gives it."""
    return {"data": {"title": title, "req_id": str(number), "slug": str(number), "posted_date": posted,
                     "country_code": country, "additional_locations": [{"country_code": c} for c in others] or None,
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
                     "https://acme.jibeapply.com/jobs/85806", "https://acme.jibeapply.com/jobs/85806/", api):
            self.assertEqual(jibe.site_of(link), link.split("/")[2], link)
        # A company's own domain counts only with icims=1 (or once it's known as a Jibe site); never a classic portal.
        for other in ("https://careers.acme.com/jobs/85806", "https://careers.acme.com/jobs/85806?icims=0",
                      "https://careers-acme.icims.com/jobs/85806/job?icims=1", "https://careers.acme.com/api/jobs?page=1",
                      "https://careers.acme.com/jobs/abc?icims=1", "https://careers.acme.com/jobs?icims=1", "not a link", None):
            self.assertIsNone(jibe.site_of(other), other)
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

    def test_postings_are_read_with_every_country_and_their_date(self):
        data = {"jobs": [jibe_item(1, "Software Engineer I", country="IN", others=("US",)),
                         jibe_item(2, "Data Engineer", posted="November 20, 2026", canonical="https://careers.acme.com/jobs/2?lang=fr-fr"),
                         jibe_item(3, " "), {"data": None}, "junk",
                         jibe_item("", "No id"), jibe_item(4, "Late", posted="soon", apply_host="apply.example.com")],
                "totalCount": 9}
        jobs = jibe.jobs_from(data, "careers.acme.com")
        self.assertEqual([(j["title"], j["url"], j["countries"], j.get("posted")) for j in jobs], [
            ("Software Engineer I", "https://careers.acme.com/jobs/1?lang=en-us", ["IN", "US"], "2026-11-20T21:14:00+00:00"),
            ("Data Engineer", "https://careers.acme.com/jobs/2?lang=fr-fr", ["US"], "2026-11-20T00:00:00+00:00"),
            ("Late", "https://careers.acme.com/jobs/4?lang=en-us", ["US"], None)])
        self.assertEqual([j["apply_hosts"] for j in jobs], [["careers-acme.icims.com"]] * 2 + [[]])
        self.assertEqual(jobs[0]["client"], "acme")
        self.assertEqual(jibe.jobs_from({"jobs": None}, "x"), [])
        self.assertEqual(jibe.jobs_from(None, "x"), [])

    def test_the_next_page_is_read_only_for_a_busy_site(self):
        now = datetime.now(timezone.utc)
        full = [{"posted": (now - timedelta(hours=1)).isoformat()}] * jibe.LIMIT
        self.assertTrue(jibe.read_further(full, jibe.LIMIT))
        self.assertFalse(jibe.read_further(full, jibe.LIMIT - 1))  # (the last page)
        self.assertFalse(jibe.read_further(full[:-1] + [{"posted": (now - timedelta(days=jibe.DEEP_DAYS + 1)).isoformat()}], jibe.LIMIT))
        self.assertFalse(jibe.read_further(full[:-1] + [{}], jibe.LIMIT))
        self.assertFalse(jibe.read_further(full[:-1] + [{"posted": "soon"}], jibe.LIMIT))
        self.assertFalse(jibe.read_further([], jibe.LIMIT))
        self.assertTrue(jibe.read_further(full[:-1] + [{"posted": now.replace(tzinfo=None).isoformat()}], jibe.LIMIT))


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
