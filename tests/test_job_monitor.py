"""
Offline tests for job_monitor.py — no network, no real email.

Run from the repo root:
    python -m unittest discover -s tests -v
"""

import email.utils
import errno
import json
import logging
import multiprocessing
import os
import shutil
import smtplib
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

REPO_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_DIR))

import job_monitor as jm  # noqa: E402

URL = "https://acme.example/careers"
SELECTOR = "a[href*='/jobs/']"
WORKING_EMAIL = {
    "enabled": True,
    "smtp_server": "smtp.example.com",
    "smtp_port": 587,
    "sender_email": "bot@example.com",
    "sender_password": "app-password",
    "recipient_email": ["me@example.com"],
}
ALERTS = {"Acme": [{"title": "Data Analyst", "url": "https://acme.example/jobs/1"}]}
EMAIL_ENV_VARS = ("SENDER_EMAIL", "SENDER_PASSWORD", "RECIPIENT_EMAIL")


def setUpModule():
    # Keep test output readable; assertLogs still captures records.
    logging.getLogger().handlers = [logging.NullHandler()]


def target(name="Acme", url=URL, **extra):
    t = {"name": name, "url": url, "mode": "html", "link_selector": SELECTOR}
    t.update(extra)
    return t


def page(*jobs):
    """Careers page HTML; each job is (id, inner_html)."""
    links = "".join(f'<li><a href="/jobs/{jid}">{inner}</a></li>' for jid, inner in jobs)
    return f"<html><body><ul>{links}</ul></body></html>"


def jobs_named(*names):
    return [(name.lower().replace(" ", "-"), name) for name in names]


class FakeResponse:
    def __init__(self, text="", status_code=200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise jm.requests.HTTPError(str(self.status_code))

    def json(self):
        return json.loads(self.text)


class FakeSMTP:
    """Stands in for smtplib.SMTP and SMTP_SSL and records what would be sent."""

    instances: list["FakeSMTP"] = []
    fail_with: Exception | None = None

    def __init__(self, host, port, **kwargs):
        self.host, self.port, self.kwargs = host, port, kwargs
        self.starttls_context = None
        self.user = None
        self.sent = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ehlo(self):
        pass

    def starttls(self, context=None):
        self.starttls_context = context

    def login(self, user, password):
        if FakeSMTP.fail_with:
            raise FakeSMTP.fail_with
        self.user = user

    def send_message(self, msg):
        recipients = [addr for _, addr in email.utils.getaddresses([msg["To"] or ""]) if addr]
        if not recipients:
            raise smtplib.SMTPRecipientsRefused({})
        self.sent.append((recipients, msg))


class MonitorTestCase(unittest.TestCase):
    """Runs the monitor against fake pages in a temp dir with SMTP faked."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.pages: dict[str, str] = {}

        self.patch(jm, "STATE_PATH", self.tmp / "state.json")
        self.patch(jm.requests, "get", self.fake_get)
        self.patch(jm.requests, "post", self.fake_post)
        self.patch(jm, "print_console_safe", lambda text: None)

        FakeSMTP.instances = []
        FakeSMTP.fail_with = None
        self.patch(jm.smtplib, "SMTP", FakeSMTP)
        self.patch(jm.smtplib, "SMTP_SSL", FakeSMTP)

        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for var in EMAIL_ENV_VARS:
            os.environ.pop(var, None)

    def patch(self, obj, attr, value):
        patcher = mock.patch.object(obj, attr, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_get(self, url, *args, **kwargs):
        if url in self.pages:
            return FakeResponse(self.pages[url])
        raise jm.requests.ConnectionError(f"no fake page for {url}")

    def fake_post(self, url, *args, **kwargs):
        raise jm.requests.ConnectionError(f"no fake page for {url}")

    def run_monitor(self, targets, email=None, keyword_filters=None):
        """Run one monitor cycle. Returns (run() result, {target name: [new titles]})."""
        config = {
            "email": email or {"enabled": False},
            "keyword_filters": keyword_filters or [],
            "targets": targets,
        }
        config_path = self.tmp / "config.json"
        config_path.write_text(json.dumps(config))

        reported = {}
        real_send_email = jm.send_email

        def spy(cfg, all_new):
            reported.update({name: [j["title"] for j in jobs] for name, jobs in all_new.items()})
            return real_send_email(cfg, all_new)

        with mock.patch.object(jm, "send_email", spy):
            result = jm.run(str(config_path))
        return result, reported

    def saved_state(self):
        return json.loads(jm.STATE_PATH.read_text())

    def mark_known(self, *urls):
        """Mark targets as already monitored (no jobs listed at the last check),
        so the next run alerts what it finds instead of recording a baseline."""
        jm.save_state({url: [] for url in urls})

    def emails_sent(self):
        return [sent for smtp in FakeSMTP.instances for sent in smtp.sent]


# ===================================================================
# Email delivery
# ===================================================================
class EmailDeliveryTests(MonitorTestCase):
    def test_failed_email_keeps_jobs_for_next_run(self):
        self.mark_known(URL)
        self.pages[URL] = page(*jobs_named("Data Analyst"))
        FakeSMTP.fail_with = smtplib.SMTPAuthenticationError(535, b"bad password")

        ok, reported = self.run_monitor([target()], email=WORKING_EMAIL)
        self.assertFalse(ok)
        self.assertEqual(reported, {"Acme": ["Data Analyst"]})
        self.assertEqual(self.saved_state(), {URL: []})

        FakeSMTP.fail_with = None
        ok, reported = self.run_monitor([target()], email=WORKING_EMAIL)
        self.assertTrue(ok)
        self.assertEqual(reported, {"Acme": ["Data Analyst"]})
        self.assertEqual(len(self.emails_sent()), 1)

        ok, reported = self.run_monitor([target()], email=WORKING_EMAIL)
        self.assertEqual(reported, {})

    def test_recipient_from_env_when_config_is_empty(self):
        cfg = {"email": dict(WORKING_EMAIL, recipient_email=[])}
        os.environ["RECIPIENT_EMAIL"] = "me@example.com, you@example.com"
        self.assertTrue(jm.send_email(cfg, ALERTS))
        [(recipients, _msg)] = self.emails_sent()
        self.assertEqual(recipients, ["me@example.com", "you@example.com"])

    def test_config_recipient_takes_precedence_over_env(self):
        os.environ["RECIPIENT_EMAIL"] = "env@example.com"
        self.assertTrue(jm.send_email({"email": WORKING_EMAIL}, ALERTS))
        [(recipients, _msg)] = self.emails_sent()
        self.assertEqual(recipients, ["me@example.com"])

    def test_blank_config_recipient_falls_back_to_env(self):
        os.environ["RECIPIENT_EMAIL"] = "me@example.com"
        for blank in ([""], "  ", [" ", ""]):
            FakeSMTP.instances = []
            cfg = {"email": dict(WORKING_EMAIL, recipient_email=blank)}
            self.assertTrue(jm.send_email(cfg, ALERTS), blank)
            self.assertEqual(self.emails_sent()[0][0], ["me@example.com"])

    def test_no_recipient_anywhere_fails(self):
        cfg = {"email": dict(WORKING_EMAIL, recipient_email=[])}
        with self.assertLogs(jm.log, "ERROR"):
            self.assertFalse(jm.send_email(cfg, ALERTS))
        self.assertEqual(self.emails_sent(), [])

    def test_committed_config_all_works_with_env_vars_only(self):
        committed = json.loads((REPO_DIR / "config_all.json").read_text())
        os.environ.update(
            SENDER_EMAIL="bot@example.com",
            SENDER_PASSWORD="app-password",
            RECIPIENT_EMAIL="me@example.com",
        )
        self.assertTrue(jm.send_email({"email": committed["email"]}, ALERTS))
        [smtp] = FakeSMTP.instances
        self.assertEqual(smtp.user, "bot@example.com")
        self.assertEqual(smtp.sent[0][0], ["me@example.com"])

    def test_disabled_email_counts_as_delivered(self):
        self.mark_known(URL)
        self.pages[URL] = page(*jobs_named("Data Analyst"))
        ok, reported = self.run_monitor([target()])
        self.assertTrue(ok)
        self.assertEqual(reported, {"Acme": ["Data Analyst"]})
        self.assertIn(URL, self.saved_state())


class SmtpSecurityTests(MonitorTestCase):
    def test_starttls_verifies_certificate_and_times_out(self):
        self.assertTrue(jm.send_email({"email": WORKING_EMAIL}, ALERTS))
        [smtp] = FakeSMTP.instances
        self.assertEqual(smtp.kwargs.get("timeout"), jm.SMTP_TIMEOUT_SECONDS)
        self.assertEqual(smtp.starttls_context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(smtp.starttls_context.check_hostname)

    def test_ssl_port_465_verifies_certificate_and_times_out(self):
        self.assertTrue(jm.send_email({"email": dict(WORKING_EMAIL, smtp_port=465)}, ALERTS))
        [smtp] = FakeSMTP.instances
        self.assertEqual(smtp.kwargs.get("timeout"), jm.SMTP_TIMEOUT_SECONDS)
        self.assertEqual(smtp.kwargs["context"].verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(smtp.kwargs["context"].check_hostname)


class SmtpTimeoutTests(unittest.TestCase):
    def test_silent_server_does_not_hang(self):
        server = socket.socket()
        self.addCleanup(server.close)
        server.bind(("127.0.0.1", 0))
        server.listen(1)  # accepts the connection but never says hello
        cfg = {"email": dict(WORKING_EMAIL, smtp_server="127.0.0.1", smtp_port=server.getsockname()[1])}

        with mock.patch.object(jm, "SMTP_TIMEOUT_SECONDS", 1), self.assertLogs(jm.log, "ERROR"):
            start = time.monotonic()
            self.assertFalse(jm.send_email(cfg, ALERTS))
        self.assertLess(time.monotonic() - start, 10)


# ===================================================================
# Choosing the config file
# ===================================================================
class ConfigFileTests(MonitorTestCase):
    MINE = {"email": {"enabled": False}, "targets": [{"name": "Mine", "url": URL}]}
    ALL = {"email": {"enabled": False}, "targets": [{"name": "All", "url": URL}]}

    def setUp(self):
        super().setUp()
        self.patch(jm, "BASE_DIR", self.tmp)
        self.patch(jm, "CONFIG_PATH", self.tmp / "config.json")
        self.patch(jm, "CONFIG_ALL_PATH", self.tmp / "config_all.json")
        os.environ.pop("JOB_MONITOR_CONFIG", None)

    def write(self, name, data):
        (self.tmp / name).write_text(json.dumps(data))

    def test_missing_config_path_is_an_error_and_writes_nothing(self):
        self.write("config.json", self.MINE)
        before = sorted(p.name for p in self.tmp.iterdir())
        for how in ("argument", "environment"):
            with self.subTest(how=how):
                if how == "environment":
                    os.environ["JOB_MONITOR_CONFIG"] = "confg.json"
                with self.assertLogs(jm.log, "ERROR") as logs, self.assertRaises(SystemExit) as exit_:
                    jm.ensure_config("confg.json" if how == "argument" else None)
                self.assertNotEqual(exit_.exception.code, 0)
                self.assertTrue(any("confg.json" in line for line in logs.output))
                self.assertEqual(json.loads((self.tmp / "config.json").read_text()), self.MINE)
                self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), before)

    def test_first_run_without_any_config_creates_a_starter(self):
        with self.assertRaises(SystemExit) as exit_:
            jm.ensure_config()
        self.assertEqual(exit_.exception.code, 0)
        self.assertEqual(json.loads((self.tmp / "config.json").read_text()), jm.DEFAULT_CONFIG)

    def test_default_is_still_config_all_and_ignored_config_json_is_flagged(self):
        self.write("config.json", self.MINE)
        self.write("config_all.json", self.ALL)
        with self.assertLogs(jm.log, "WARNING") as logs:
            self.assertEqual(jm.ensure_config(), self.ALL)
        self.assertTrue(any("config.json" in line and "--config" in line for line in logs.output))

    def test_no_warning_when_there_is_nothing_to_ignore(self):
        self.write("config_all.json", self.ALL)
        with self.assertNoLogs(jm.log, "WARNING"):
            self.assertEqual(jm.ensure_config(), self.ALL)

    def test_explicit_config_is_used_without_warning(self):
        self.write("config.json", self.MINE)
        self.write("config_all.json", self.ALL)
        with self.assertNoLogs(jm.log, "WARNING"):
            self.assertEqual(jm.ensure_config("config.json"), self.MINE)

    def test_config_json_is_used_when_there_is_no_config_all(self):
        self.write("config.json", self.MINE)
        self.assertEqual(jm.ensure_config(), self.MINE)


# ===================================================================
# First check of a target: silent baseline
# ===================================================================
class BaselineTests(MonitorTestCase):
    def test_first_check_records_jobs_without_alerting(self):
        self.pages[URL] = page(*jobs_named("Data Analyst", "Quant Researcher"))
        with self.assertLogs(jm.log, "INFO") as logs:
            ok, reported = self.run_monitor([target()], email=WORKING_EMAIL)
        self.assertTrue(ok)
        self.assertEqual(reported, {})
        self.assertEqual(self.emails_sent(), [])
        self.assertEqual(len(self.saved_state()[URL]), 2)
        self.assertTrue(any("baseline" in line.lower() for line in logs.output))

        self.pages[URL] = page(*jobs_named("Data Analyst", "Quant Researcher", "ML Engineer"))
        ok, reported = self.run_monitor([target()], email=WORKING_EMAIL)
        self.assertEqual(reported, {"Acme": ["ML Engineer"]})
        self.assertEqual(len(self.emails_sent()), 1)

    def test_adding_a_target_does_not_alert_its_existing_jobs(self):
        other = "https://other.example/careers"
        self.mark_known(URL)
        self.pages[URL] = page(*jobs_named("Data Analyst"))
        self.pages[other] = page(*jobs_named("Quant Researcher", "Trader"))
        _, reported = self.run_monitor([target(), target(name="Other", url=other)])
        self.assertEqual(reported, {"Acme": ["Data Analyst"]})
        self.assertEqual(len(self.saved_state()[other]), 2)

    def test_empty_first_check_still_counts_as_the_baseline(self):
        self.pages[URL] = page()
        self.run_monitor([target()])
        self.assertEqual(self.saved_state()[URL], [])
        self.pages[URL] = page(*jobs_named("First Ever Opening"))
        _, reported = self.run_monitor([target()])
        self.assertEqual(reported, {"Acme": ["First Ever Opening"]})

    def test_failed_first_fetch_records_no_baseline(self):
        with self.assertLogs(jm.log, "ERROR"):
            self.run_monitor([target()])  # no page: the fetch fails
        self.assertFalse(jm.STATE_PATH.exists())
        self.pages[URL] = page(*jobs_named("Data Analyst"))
        _, reported = self.run_monitor([target()])
        self.assertEqual(reported, {})
        self.assertIn(URL, self.saved_state())

    def test_targets_already_in_state_are_not_rebaselined(self):
        jm.STATE_PATH.write_text(json.dumps({
            URL: [{"title": "Data Analyst", "url": "https://acme.example/jobs/data-analyst"}]
        }))
        self.pages[URL] = page(*jobs_named("Data Analyst", "Quant Researcher"))
        _, reported = self.run_monitor([target()])
        self.assertEqual(reported, {"Acme": ["Quant Researcher"]})


# ===================================================================
# Detecting new jobs
# ===================================================================
class DetectionTests(MonitorTestCase):
    def test_jobs_beyond_the_tenth_link_are_detected(self):
        self.mark_known(URL)
        names = [f"Job {i:02d}" for i in range(15)]
        self.pages[URL] = page(*jobs_named(*names))
        _, reported = self.run_monitor([target()])
        self.assertEqual(reported["Acme"], names)

        self.pages[URL] = page(*jobs_named(*names, "Late Addition"))
        _, reported = self.run_monitor([target()])
        self.assertEqual(reported, {"Acme": ["Late Addition"]})

    def test_reordering_does_not_realert(self):
        names = [f"Job {i:02d}" for i in range(12)]
        self.pages[URL] = page(*jobs_named(*names))
        self.run_monitor([target()])
        for order in (names[::-1], names[6:] + names[:6], names):
            self.pages[URL] = page(*jobs_named(*order))
            _, reported = self.run_monitor([target()])
            self.assertEqual(reported, {})

    def test_job_that_disappears_and_returns_is_not_realerted(self):
        self.pages[URL] = page(*jobs_named("Data Analyst", "Quant Researcher"))
        self.run_monitor([target()])
        self.pages[URL] = page(*jobs_named("Quant Researcher"))
        self.run_monitor([target()])
        self.pages[URL] = page(*jobs_named("Data Analyst", "Quant Researcher"))
        _, reported = self.run_monitor([target()])
        self.assertEqual(reported, {})

    def test_retention_never_drops_jobs_still_listed(self):
        with mock.patch.object(jm, "STATE_RETENTION_PER_TARGET", 3):
            self.mark_known(URL)
            self.pages[URL] = page(*jobs_named("A", "B", "C", "D", "E"))
            _, reported = self.run_monitor([target()])
            self.assertEqual(len(reported["Acme"]), 5)
            _, reported = self.run_monitor([target()])
            self.assertEqual(reported, {})

            # Once over the limit, jobs no longer listed are the ones dropped.
            self.pages[URL] = page(*jobs_named("F", "G"))
            self.run_monitor([target()])
            remembered = [j["title"] for j in self.saved_state()[URL]]
            self.assertEqual(remembered, ["F", "G", "A"])

    def test_per_target_keywords_apply_on_html_path(self):
        self.mark_known(URL)
        self.pages[URL] = page(*jobs_named("Software Engineer", "Quant Trader"))
        targets = [target(keyword_filters=["trader"])]
        _, reported = self.run_monitor(targets, keyword_filters=["engineer"])
        self.assertEqual(reported, {"Acme": ["Quant Trader"]})

    def test_keywords_match_words_split_by_inline_tags(self):
        self.mark_known(URL)
        self.pages[URL] = page(
            ("1", "Infra<wbr>structure Engineer"),
            ("2", "<mark>Engineer</mark>ing Manager"),
            ("3", "Data Analyst"),
            ("4", "Sales Lead"),
        )
        _, reported = self.run_monitor(
            [target()], keyword_filters=["infrastructure", "engineering", "data analyst"]
        )
        self.assertEqual(len(reported["Acme"]), 3)
        self.assertNotIn("Sales Lead", reported["Acme"])

    def test_keywords_do_not_match_across_words(self):
        titles = {
            "sre": "Sales Representative",
            "pm": "Help Manager",
            "ios": "Radio Specialist",
            "hr": "Growth Recruiter",
            " ai ": "Aircraft Mechanic",
        }
        for keyword, title in titles.items():
            jobs = [{"title": title, "url": "https://acme.example/jobs/1"}]
            self.assertEqual(jm.filter_by_keywords(jobs, [keyword]), [], keyword)

    def test_keywords_keep_substring_matching(self):
        cases = {
            "data": "Metadata Engineer",
            "ios": "Scenarios Analyst",
            " ai ": "Senior AI Engineer",
            "javascript": "Java Script Developer",  # from <em>Java</em>Script
            "c++": "C ++ Developer",  # from <b>C</b>++
            "": "Anything",
        }
        for keyword, title in cases.items():
            jobs = [{"title": title, "url": "https://acme.example/jobs/1"}]
            self.assertEqual(jm.filter_by_keywords(jobs, [keyword]), jobs, keyword)

    def test_global_keywords_apply_when_target_has_none(self):
        self.mark_known(URL)
        self.pages[URL] = page(*jobs_named("Software Engineer", "Quant Trader"))
        _, reported = self.run_monitor([target()], keyword_filters=["engineer"])
        self.assertEqual(reported, {"Acme": ["Software Engineer"]})

    def test_broken_target_does_not_stop_the_others(self):
        other = "https://other.example/careers"
        self.mark_known(URL, other)
        self.pages[URL] = page(*jobs_named("Data Analyst"))
        self.pages[other] = page(*jobs_named("Quant Researcher"))
        targets = [
            target(name="Broken", url=other, link_selector="a[href*=/jobs/]"),  # invalid CSS
            target(),
        ]
        with self.assertLogs(jm.log, "ERROR") as logs:
            ok, reported = self.run_monitor(targets)
        self.assertTrue(ok)
        self.assertEqual(reported, {"Acme": ["Data Analyst"]})
        self.assertIn(URL, self.saved_state())
        self.assertTrue(any("Broken" in line for line in logs.output))

    def test_failed_fetch_leaves_saved_jobs_alone(self):
        self.pages[URL] = page(*jobs_named("Data Analyst"))
        self.run_monitor([target()])
        before = self.saved_state()[URL]
        del self.pages[URL]
        with self.assertLogs(jm.log, "ERROR"):
            self.run_monitor([target()])
        self.assertEqual(self.saved_state()[URL], before)


# ===================================================================
# Job identity (URL, not title)
# ===================================================================
class JobIdentityTests(MonitorTestCase):
    def test_changing_card_text_is_not_a_new_job(self):
        self.pages[URL] = page(("1", "<span>Software Engineer</span><span>Posted 1 day ago</span>"))
        self.run_monitor([target()])
        self.pages[URL] = page(("1", "<span>Software Engineer</span><span>Posted 2 days ago</span>"))
        _, reported = self.run_monitor([target()])
        self.assertEqual(reported, {})

    def test_existing_state_from_older_version_does_not_flood(self):
        # Titles saved by the old code were glued together and included volatile text.
        jm.STATE_PATH.write_text(json.dumps({
            URL: [{"title": "Software EngineerPosted 1 day ago", "url": "https://acme.example/jobs/1"}]
        }))
        self.pages[URL] = page(("1", "<span>Software Engineer</span><span>Posted 3 days ago</span>"))
        _, reported = self.run_monitor([target()])
        self.assertEqual(reported, {})

    def test_url_variants_of_the_same_job_match(self):
        variants = [
            "https://acme.example/jobs/1",
            "https://ACME.example/jobs/1",
            "https://acme.example/jobs/1/",
            "https://acme.example/jobs/1?utm_source=linkedin&utm_medium=social",
            " https://acme.example/jobs/1 ",
        ]
        ids = {jm.compute_job_id({"title": f"title {i}", "url": u}) for i, u in enumerate(variants)}
        self.assertEqual(len(ids), 1)

    def test_different_jobs_stay_different(self):
        urls = [
            "https://acme.example/jobs/1",
            "https://acme.example/jobs/2",
            "https://boards.example/acme/jobs?gh_jid=111",
            "https://boards.example/acme/jobs?gh_jid=222",
            "https://acme.example/careers#/job/111",
            "https://acme.example/careers#/job/222",
        ]
        ids = {jm.compute_job_id({"title": "Same Title", "url": u}) for u in urls}
        self.assertEqual(len(ids), len(urls))

    def test_tracking_params_are_ignored_but_job_params_kept(self):
        a = jm.compute_job_id({"title": "x", "url": "https://b.example/jobs?gh_jid=111&utm_source=x"})
        b = jm.compute_job_id({"title": "x", "url": "https://b.example/jobs?gh_jid=111"})
        c = jm.compute_job_id({"title": "x", "url": "https://b.example/jobs?gh_jid=222&utm_source=x"})
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_titles_keep_spaces_between_parts(self):
        html = '<a href="/jobs/1"><span>Software Engineer</span><span>Seattle, WA</span></a>'
        [job] = jm.extract_jobs_from_html(html, URL, SELECTOR)
        self.assertEqual(job["title"], "Software Engineer Seattle, WA")
        [job] = jm.extract_jobs_from_html(html, URL, "")
        self.assertEqual(job["title"], "Software Engineer Seattle, WA")

    def test_generic_button_text_split_across_tags_uses_url_slug(self):
        html = '<a href="/jobs/data-analyst-ii"><span>Apply</span><span>Now</span></a>'
        [job] = jm.extract_jobs_from_html(html, URL, SELECTOR)
        self.assertEqual(job["title"], "Data Analyst Ii")


# ===================================================================
# state.json safety
# ===================================================================
def _save_many(state_path, worker, count):
    jm.STATE_PATH = Path(state_path)
    for i in range(count):
        jm.load_state()
        jm.save_state({f"https://w{worker}.example/{i}": [{"title": "t", "url": f"https://w{worker}.example/{i}"}]})


class StateFileTests(MonitorTestCase):
    JOB_A = {"title": "A", "url": "https://acme.example/jobs/a"}
    JOB_B = {"title": "B", "url": "https://acme.example/jobs/b"}

    def test_interrupted_write_keeps_previous_file(self):
        jm.save_state({URL: [self.JOB_A]})
        before = jm.STATE_PATH.read_text()

        def dump_then_fail(obj, fh, **kwargs):
            fh.write('{"partial": [')
            raise OSError("No space left on device")

        with mock.patch.object(jm.json, "dump", dump_then_fail), self.assertRaises(OSError):
            jm.save_state({URL: [self.JOB_B]})

        self.assertEqual(jm.STATE_PATH.read_text(), before)
        leftovers = [p.name for p in self.tmp.iterdir() if p.name not in ("state.json", "state.json.lock")]
        self.assertEqual(leftovers, [])

    def test_stale_temp_files_from_a_killed_save_are_cleaned_up(self):
        stale = self.tmp / "state.json.abc123.tmp"
        stale.write_text('{"partial": [')
        an_hour_ago = time.time() - 3600
        os.utime(stale, (an_hour_ago, an_hour_ago))
        in_progress = self.tmp / "state.json.def456.tmp"  # another monitor may be writing this
        in_progress.write_text('{"partial": [')
        unrelated = self.tmp / "notes.tmp"
        unrelated.write_text("keep me")
        os.utime(unrelated, (an_hour_ago, an_hour_ago))

        jm.save_state({URL: [self.JOB_A]})

        self.assertFalse(stale.exists())
        self.assertTrue(in_progress.exists())
        self.assertTrue(unrelated.exists())
        self.assertEqual(self.saved_state(), {URL: [self.JOB_A]})

    @unittest.skipIf(sys.platform == "win32", "fcntl is POSIX-only")
    def test_save_works_where_file_locking_is_unsupported(self):
        self.mark_known(URL)

        def no_flock(*args):  # e.g. Lustre without the flock mount option
            raise OSError(errno.ENOSYS, "Function not implemented")

        self.patch(jm.fcntl, "flock", no_flock)
        self.pages[URL] = page(*jobs_named("Data Analyst"))
        with self.assertLogs(jm.log, "WARNING"):
            ok, reported = self.run_monitor([target()])
        self.assertTrue(ok)
        self.assertEqual(reported, {"Acme": ["Data Analyst"]})
        _, reported = self.run_monitor([target()])
        self.assertEqual(reported, {})

    @unittest.skipIf(sys.platform == "win32", "fcntl is POSIX-only")
    def test_save_works_when_lock_file_cannot_be_opened(self):
        self.mark_known(URL)
        lock_path = self.tmp / "state.json.lock"
        lock_path.unlink()
        lock_path.mkdir()  # opening it for append fails
        self.pages[URL] = page(*jobs_named("Data Analyst"))
        with self.assertLogs(jm.log, "WARNING"):
            ok, reported = self.run_monitor([target()])
        self.assertTrue(ok)
        self.assertEqual(reported, {"Acme": ["Data Analyst"]})
        _, reported = self.run_monitor([target()])
        self.assertEqual(reported, {})

    @unittest.skipIf(sys.platform == "win32", "POSIX permissions and symlinks")
    def test_save_keeps_file_permissions(self):
        umask = os.umask(0)
        os.umask(umask)
        jm.save_state({URL: [self.JOB_A]})
        self.assertEqual(stat.S_IMODE(jm.STATE_PATH.stat().st_mode), 0o666 & ~umask)
        os.chmod(jm.STATE_PATH, 0o640)
        jm.save_state({URL: [self.JOB_B]})
        self.assertEqual(stat.S_IMODE(jm.STATE_PATH.stat().st_mode), 0o640)

    @unittest.skipIf(sys.platform == "win32", "POSIX permissions and symlinks")
    def test_save_writes_through_a_symlink(self):
        real = self.tmp / "shared" / "real_state.json"
        real.parent.mkdir()
        real.write_text("{}")
        jm.STATE_PATH.symlink_to(real)
        jm.save_state({URL: [self.JOB_A]})
        self.assertTrue(jm.STATE_PATH.is_symlink())
        self.assertEqual(json.loads(real.read_text()), {URL: [self.JOB_A]})

    def test_windows_retries_rename_while_file_is_open_elsewhere(self):
        real_replace = os.replace
        calls = []

        def replace_blocked_twice(src, dst):
            calls.append(dst)
            if len(calls) < 3:
                raise PermissionError(13, "The process cannot access the file")
            real_replace(src, dst)

        self.patch(jm, "_IS_WINDOWS", True)
        self.patch(jm.os, "replace", replace_blocked_twice)
        self.patch(jm.time, "sleep", lambda seconds: None)
        jm.save_state({URL: [self.JOB_A]})
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.saved_state(), {URL: [self.JOB_A]})

    def test_permission_error_is_not_retried_elsewhere(self):
        calls = []

        def replace_denied(src, dst):
            calls.append(dst)
            raise PermissionError(13, "Permission denied")

        self.patch(jm, "_IS_WINDOWS", False)
        self.patch(jm.os, "replace", replace_denied)
        with self.assertRaises(PermissionError):
            jm.save_state({URL: [self.JOB_A]})
        self.assertEqual(len(calls), 1)
        self.assertEqual(list(self.tmp.glob("state.json.*.tmp")), [])

    def test_corrupt_file_is_set_aside_and_run_continues(self):
        jm.STATE_PATH.write_text('{"https://acme.example/careers": [{"title": "Data An')
        self.pages[URL] = page(*jobs_named("Data Analyst"))

        with self.assertLogs(jm.log, "ERROR"):
            ok, reported = self.run_monitor([target()])

        # Every target starts a fresh, silent baseline instead of re-alerting everything.
        self.assertTrue(ok)
        self.assertEqual(reported, {})
        self.assertEqual(len(self.saved_state()[URL]), 1)
        [backup] = self.tmp.glob("state.json.corrupt-*")
        self.assertIn("Data An", backup.read_text())

    def test_state_that_is_not_an_object_is_set_aside_on_save(self):
        jm.STATE_PATH.write_text("[]")
        with self.assertLogs(jm.log, "ERROR"):
            self.assertEqual(jm.load_state(), {})
        self.assertEqual(jm.STATE_PATH.read_text(), "[]")  # reading alone never moves it

        with self.assertLogs(jm.log, "ERROR"):
            jm.save_state({URL: [self.JOB_A]})
        [backup] = self.tmp.glob("state.json.corrupt-*")
        self.assertEqual(backup.read_text(), "[]")
        self.assertEqual(self.saved_state(), {URL: [self.JOB_A]})

    def test_corrupt_file_race_keeps_the_other_monitors_save(self):
        other = "https://other.example/careers"
        jm.STATE_PATH.write_text("{not json")
        real_load = json.load
        calls = []

        def load_while_other_monitor_saves(fh, *args, **kwargs):
            calls.append(1)
            if len(calls) > 1:
                return real_load(fh, *args, **kwargs)
            try:
                return real_load(fh, *args, **kwargs)  # monitor A fails to parse the file...
            finally:
                # ...and before A does anything about it, monitor B runs a whole save.
                with mock.patch.object(jm.json, "load", real_load):
                    jm.save_state({other: [self.JOB_B]})

        with mock.patch.object(jm.json, "load", load_while_other_monitor_saves), \
                self.assertLogs(jm.log, "ERROR"):
            self.assertEqual(jm.load_state(), {})
        jm.save_state({URL: [self.JOB_A]})  # then A saves its own target

        self.assertEqual(self.saved_state(), {other: [self.JOB_B], URL: [self.JOB_A]})
        [backup] = self.tmp.glob("state.json.corrupt-*")
        self.assertEqual(backup.read_text(), "{not json")

    def test_save_does_not_overwrite_another_writers_targets(self):
        jm.save_state({URL: [self.JOB_A], "https://other.example/careers": [self.JOB_A]})
        jm.load_state()  # this process reads...
        jm.save_state({"https://other.example/careers": [self.JOB_B]})  # ...another batch saves...
        jm.save_state({URL: [self.JOB_B]})  # ...then this process saves only its own target
        self.assertEqual(self.saved_state(), {
            URL: [self.JOB_B],
            "https://other.example/careers": [self.JOB_B],
        })

    def test_run_only_writes_targets_it_checked(self):
        other = "https://other.example/careers"
        jm.save_state({other: [self.JOB_A]})
        self.pages[URL] = page(*jobs_named("Data Analyst"))

        real_get = self.fake_get

        def get_while_other_batch_saves(url, *args, **kwargs):
            jm.save_state({other: [self.JOB_B]})  # another monitor finishes mid-run
            return real_get(url, *args, **kwargs)

        self.patch(jm.requests, "get", get_while_other_batch_saves)
        self.run_monitor([target()])
        state = self.saved_state()
        self.assertEqual(state[other], [self.JOB_B])
        self.assertIn(URL, state)

    @unittest.skipIf(sys.platform == "win32", "file locking is POSIX-only")
    def test_parallel_processes_keep_every_update(self):
        workers, saves = 4, 25
        procs = [
            multiprocessing.Process(target=_save_many, args=(str(jm.STATE_PATH), w, saves))
            for w in range(workers)
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join(60)
            self.assertEqual(p.exitcode, 0)
        self.assertEqual(len(self.saved_state()), workers * saves)


# ===================================================================
# End to end: real script, real HTTP (local server)
# ===================================================================
class _CareersPage(BaseHTTPRequestHandler):
    body = page(*jobs_named("Data Analyst", "Quant Researcher")).encode()

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args):
        pass


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        shutil.copy(REPO_DIR / "job_monitor.py", self.tmp / "job_monitor.py")

        self.handler = type("Page", (_CareersPage,), {})
        server = ThreadingHTTPServer(("127.0.0.1", 0), self.handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.page_url = f"http://127.0.0.1:{server.server_address[1]}/careers"

    def run_script(self, email_cfg, config_name="config.json"):
        config = {"email": email_cfg, "targets": [target(url=self.page_url)]}
        (self.tmp / "config.json").write_text(json.dumps(config))
        env = {k: v for k, v in os.environ.items() if k not in EMAIL_ENV_VARS + ("JOB_MONITOR_CONFIG",)}
        env.update(NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")
        return subprocess.run(
            [sys.executable, "job_monitor.py", "--config", config_name],
            cwd=self.tmp, env=env, capture_output=True, text=True, timeout=120,
        )

    def test_mistyped_config_fails_and_leaves_config_json_alone(self):
        self.run_script({"enabled": False})  # writes config.json and records a baseline
        before = (self.tmp / "config.json").read_text()
        result = self.run_script({"enabled": False}, config_name="confg.json")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("confg.json", result.stderr)
        self.assertEqual((self.tmp / "config.json").read_text(), before)
        self.assertFalse((self.tmp / "confg.json").exists())

    def test_first_run_is_silent_then_new_postings_are_emailed(self):
        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        closed_port = closed.getsockname()[1]
        closed.close()  # nothing listens here, so SMTP is refused
        unreachable = dict(WORKING_EMAIL, smtp_server="127.0.0.1", smtp_port=closed_port)

        # First run: baseline only, so no email is attempted and the run succeeds.
        result = self.run_script(unreachable)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Data Analyst", result.stdout)
        self.assertEqual(len(json.loads((self.tmp / "state.json").read_text())[self.page_url]), 2)

        # A new posting with SMTP down: reported, exit 1, and not marked as seen.
        self.handler.body = page(*jobs_named("Data Analyst", "Quant Researcher", "ML Engineer")).encode()
        result = self.run_script(unreachable)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("ML Engineer", result.stdout)
        self.assertEqual(len(json.loads((self.tmp / "state.json").read_text())[self.page_url]), 2)

        # Delivered on the next run.
        result = self.run_script({"enabled": False})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ML Engineer", result.stdout)
        self.assertEqual(len(json.loads((self.tmp / "state.json").read_text())[self.page_url]), 3)

    def test_exit_code_and_state_follow_email_delivery(self):
        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        closed_port = closed.getsockname()[1]
        closed.close()  # nothing listens here, so SMTP is refused

        unreachable = dict(WORKING_EMAIL, smtp_server="127.0.0.1", smtp_port=closed_port)
        (self.tmp / "state.json").write_text(json.dumps({self.page_url: []}))  # already monitored
        result = self.run_script(unreachable)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("Data Analyst", result.stdout)
        self.assertEqual(json.loads((self.tmp / "state.json").read_text()), {self.page_url: []})

        result = self.run_script({"enabled": False})
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads((self.tmp / "state.json").read_text())
        self.assertEqual(len(state[self.page_url]), 2)


if __name__ == "__main__":
    unittest.main()
