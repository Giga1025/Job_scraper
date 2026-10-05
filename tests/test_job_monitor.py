"""
Offline tests for job_monitor.py — no network, no real email.

Run from the repo root:
    python -m unittest discover -s tests -v
"""

import email.utils
import hashlib
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
from datetime import datetime, timedelta
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
    def __init__(self, text="", status_code=200, content_type=None):
        self.text = text
        self.content = text.encode("utf-8")
        self.status_code = status_code
        is_json = text.lstrip()[:1] in ("{", "[")
        self.headers = {"content-type": content_type or ("application/json" if is_json else "text/html")}

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
        self.page_sequence: dict[str, list[str]] = {}  # served first, one page per fetch
        self.requests_made: list = []

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
        self.requests_made.append((url, kwargs.get("params")))
        if self.page_sequence.get(url):
            return FakeResponse(self.page_sequence[url].pop(0))
        if url in self.pages:
            return FakeResponse(self.pages[url])
        raise jm.requests.ConnectionError(f"no fake page for {url}")

    def fake_post(self, url, *args, **kwargs):
        raise jm.requests.ConnectionError(f"no fake page for {url}")

    def run_monitor(self, targets, email=None, keyword_filters=None, **config_extra):
        """Run one monitor cycle. Returns (run() result, {target name: [new titles]}).
        Titles listed in the email's left-out section end up in self.left_out."""
        config = {
            "email": email or {"enabled": False},
            "keyword_filters": keyword_filters or [],
            "targets": targets,
            **config_extra,
        }
        config_path = self.tmp / "config.json"
        config_path.write_text(json.dumps(config))

        reported = {}
        self.left_out = {}
        self.emails_attempted = 0
        real_send_email = jm.send_email

        def spy(cfg, all_new, left_out=None):
            self.emails_attempted += 1
            reported.update({name: [j["title"] for j in jobs] for name, jobs in all_new.items()})
            self.left_out = {name: [j["title"] for j in jobs] for name, jobs in (left_out or {}).items()}
            return real_send_email(cfg, all_new, left_out=left_out)

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
        self.assertEqual([j["title"] for j in self.saved_state()[URL]], ["Data Analyst"])


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

    def test_asking_for_a_missing_config_json_creates_the_starter(self):
        self.write("config_all.json", self.ALL)  # a fresh clone always has this
        for how in ("config.json", "./config.json", str(self.tmp / "config.json"), "environment"):
            with self.subTest(how=how):
                (self.tmp / "config.json").unlink(missing_ok=True)
                if how == "environment":
                    os.environ["JOB_MONITOR_CONFIG"] = "config.json"
                with self.assertRaises(SystemExit) as exit_:
                    jm.ensure_config(None if how == "environment" else how)
                self.assertEqual(exit_.exception.code, 0)
                self.assertEqual(json.loads((self.tmp / "config.json").read_text()), jm.DEFAULT_CONFIG)
                os.environ.pop("JOB_MONITOR_CONFIG", None)

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

    def test_glitchy_empty_first_check_is_retried(self):
        # Seen live: a site that sometimes renders an empty list. If that empty
        # list became the baseline, the next good check would alert every job.
        listing = page(*jobs_named(*[f"Job {i}" for i in range(30)]))
        self.page_sequence[URL] = [page(), listing]
        _, reported = self.run_monitor([target()])
        self.assertEqual(reported, {})
        self.assertEqual(len(self.saved_state()[URL]), 30)
        self.pages[URL] = listing
        _, reported = self.run_monitor([target()])
        self.assertEqual(reported, {})

    def test_empty_first_check_still_counts_as_the_baseline(self):
        self.pages[URL] = page()  # empty on the check and the retry
        self.run_monitor([target()])
        self.assertEqual(self.saved_state()[URL], [])
        self.pages[URL] = page(*jobs_named("First Ever Opening"))
        _, reported = self.run_monitor([target()])
        self.assertEqual(reported, {"Acme": ["First Ever Opening"]})

    def test_known_target_that_had_no_jobs_alerts_its_first_opening(self):
        self.mark_known(URL)  # already monitored, and it listed nothing last time
        self.pages[URL] = page(*jobs_named("First Ever Opening"))
        _, reported = self.run_monitor([target()])
        self.assertEqual(reported, {"Acme": ["First Ever Opening"]})

    def test_empty_result_later_on_keeps_known_jobs(self):
        self.pages[URL] = page(*jobs_named("Data Analyst", "ML Engineer"))
        self.run_monitor([target()])
        self.pages[URL] = page()  # a glitchy empty render
        self.run_monitor([target()])
        self.pages[URL] = page(*jobs_named("Data Analyst", "ML Engineer"))
        _, reported = self.run_monitor([target()])
        self.assertEqual(reported, {})

    def test_failed_first_fetch_records_no_baseline(self):
        with self.assertLogs(jm.log, "ERROR"):
            self.run_monitor([target()])  # no page: the fetch fails
        self.assertFalse(jm.STATE_PATH.exists())
        self.pages[URL] = page(*jobs_named("Data Analyst"))
        _, reported = self.run_monitor([target()])
        self.assertEqual(reported, {})
        self.assertIn(URL, self.saved_state())

    def test_new_targets_baseline_is_kept_while_email_is_failing(self):
        other = "https://other.example/careers"
        self.mark_known(URL)
        self.pages[URL] = page(*jobs_named("Data Analyst"))
        self.pages[other] = page(*jobs_named("Trader"))
        targets = [target(), target(name="Other", url=other)]
        FakeSMTP.fail_with = smtplib.SMTPAuthenticationError(535, b"bad password")

        ok, reported = self.run_monitor(targets, email=WORKING_EMAIL)
        self.assertFalse(ok)
        self.assertEqual(reported, {"Acme": ["Data Analyst"]})
        self.assertEqual(self.saved_state()[URL], [])  # still pending
        self.assertEqual(len(self.saved_state()[other]), 1)  # baseline kept

        # The new target posts something while email is still down: it's reported and held.
        self.pages[other] = page(*jobs_named("Trader", "New Grad Trader"))
        ok, reported = self.run_monitor(targets, email=WORKING_EMAIL)
        self.assertFalse(ok)
        self.assertEqual(reported, {"Acme": ["Data Analyst"], "Other": ["New Grad Trader"]})

        FakeSMTP.fail_with = None
        ok, reported = self.run_monitor(targets, email=WORKING_EMAIL)
        self.assertTrue(ok)
        self.assertEqual(reported, {"Acme": ["Data Analyst"], "Other": ["New Grad Trader"]})
        _, reported = self.run_monitor(targets, email=WORKING_EMAIL)
        self.assertEqual(reported, {})

    def test_url_listed_twice_keeps_its_baseline_and_held_alerts_while_email_fails(self):
        def listing(jobs, interns):
            return "<html><body>" + "".join(
                [f'<a href="/jobs/{j}">{j}</a>' for j in jobs]
                + [f'<a href="/interns/{i}">{i}</a>' for i in interns]
            ) + "</body></html>"

        # Same careers page, two entries with different selectors.
        targets = [target(name="Jobs"), target(name="Interns", link_selector="a[href*='/interns/']")]
        FakeSMTP.fail_with = smtplib.SMTPAuthenticationError(535, b"bad password")

        self.pages[URL] = listing(["j1"], ["i1"])
        ok, reported = self.run_monitor(targets, email=WORKING_EMAIL)
        self.assertFalse(ok)
        self.assertEqual(reported, {"Interns": ["i1"]})  # alerted against the Jobs baseline
        self.assertEqual([j["title"] for j in self.saved_state()[URL]], ["j1"])  # baseline only

        self.pages[URL] = listing(["j1", "j2 New Grad"], ["i1"])  # posted while email is down
        ok, reported = self.run_monitor(targets, email=WORKING_EMAIL)
        self.assertEqual(reported, {"Jobs": ["j2 New Grad"], "Interns": ["i1"]})

        FakeSMTP.fail_with = None
        ok, reported = self.run_monitor(targets, email=WORKING_EMAIL)
        self.assertTrue(ok)
        self.assertEqual(reported, {"Jobs": ["j2 New Grad"], "Interns": ["i1"]})
        _, reported = self.run_monitor(targets, email=WORKING_EMAIL)
        self.assertEqual(reported, {})

    def test_missing_state_file_is_flagged(self):
        with self.assertLogs(jm.log, "WARNING") as logs:
            self.assertEqual(jm.load_state(), {})
        self.assertTrue(any("state.json" in line for line in logs.output))
        jm.save_state({URL: []})
        with self.assertNoLogs(jm.log, "WARNING"):
            jm.load_state()

    def test_baseline_never_overwrites_another_monitors_entry(self):
        first_seen = {"title": "Data Analyst", "url": "https://acme.example/jobs/data-analyst"}
        self.pages[URL] = page(*jobs_named("Data Analyst", "New Grad Analyst"))
        real_get = self.fake_get

        def get_after_other_monitor_baselined(url, *args, **kwargs):
            jm.save_state({URL: [first_seen]})  # another monitor recorded this target first
            return real_get(url, *args, **kwargs)

        self.patch(jm.requests, "get", get_after_other_monitor_baselined)
        _, reported = self.run_monitor([target()])
        self.assertEqual(reported, {})
        self.assertEqual(self.saved_state()[URL], [first_seen])

        self.patch(jm.requests, "get", real_get)
        _, reported = self.run_monitor([target()])
        self.assertEqual(reported, {"Acme": ["New Grad Analyst"]})

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
        self.mark_known(URL)
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
        self.assertEqual(self.saved_state(), {URL: [{"title": "Data Analyst", "url": "https://acme.example/jobs/data-analyst"}]})
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
# Role filter: leave out clearly-irrelevant roles, but still list them
# ===================================================================
REPO_ROLE_FILTER = json.loads((REPO_DIR / "config_all.json").read_text())["role_filter"]

# Titles the seeker wants (new grad / early-career software, ML, infra, backend, full stack),
# including company-specific naming that a keyword allow-list would miss.
MUST_KEEP = [
    "Member of Technical Staff", "Member of Technical Staff, Pretraining", "Software Engineer, Ads Manager",
    "Software Engineer, New Grad", "Software Engineer II", "Software Engineer III",
    "Research Engineer, Pre-training", "Post-Training Researcher", "Machine Learning Engineer", "AI Engineer",
    "Forward Deployed Engineer", "Backend Engineer", "Infrastructure Engineer", "Full Stack Engineer",
    "Full-Stack Engineer (Frontend focus)", "Technology Analyst Program",
    "2026 | Americas | New York | Engineering | New Analyst", "Associate Software Engineer", "Applied Scientist",
    "Data Engineer", "Site Reliability Engineer", "Quantitative Developer",
    "Software Engineer - Frontend Developer Productivity", "Systems Software Engineer - New College Grad 2026",
    "Software Engineer, Sales Engineering Tools", "Inference Engineer", "Software Engineer, Lead Generation",
    "Research Scientist, Fundamental Generative AI - New College Grad 2026", "Quantitative Researcher",
    "Software Engineer, ML Hardware", "Firmware Engineer", "HPC Operations Engineer",
    "Software Engineer Intern / New Grad", "Software Engineer, Data Platform", "Security Engineer", "iOS Engineer",
    "Applied AI Engineer", "Privacy & Civil Liberties Engineer - New Grad", "Web/App Test Engineer",
    "Software Engineer, Fleet Management",
    # Found by auditing every left-out title from the live scan:
    "Software Engineer - Mission Manager", "Site Reliability Operations Analyst",
    "Full Stack Staff & Software Engineer, Consumer Monetization",
    "Software Engineer II - React Native - Krak Frontend • Brazil; Argentina",
    "icon Infrastructure Software Engineer: Application Engineering : The D. E. Shaw group seeks a lead software engineer",
    "Infrastructure Engineer III - Amazon Connect : Lead and grow a team",
    "Solutions Architect - Manufacturing", "Product Engineer - Manufacturing Operations",
    "Hardware Tools Engineer", "RTL Tools & Methodology Engineer",
    "Leadership Development Program, Technology", "Architect - New College Grad 2026",
]
MUST_LEAVE_OUT = [
    "Senior Software Engineer", "Sr. Software Engineer", "Staff ML Engineer", "Principal Engineer",
    "Engineering Manager, Infra", "Product Manager, AI", "Manager, Software Engineering", "Account Executive",
    "Recruiter", "Frontend Engineer", "Software Engineer Intern", "Mechanical Engineer", "Barista - Memphis",
    "Tech Lead, Platform", "Director of Engineering", "Deployment Strategist", "Electrical Engineer, Power",
    "ASIC Design Verification Engineer", "Summer Analyst 2027", "HRIS Manager", "Growth Campaign Manager",
    "Civil Engineer Memphis, TN",
    # Senior roles that slipped through before the audit:
    "Software Engineer 6", "Research Engineer 5/6", "AI Research Engineer 6 - TL", "Principle Engineer",
    "Software Engineer (Technical Leadership) - Machine Learning", "Manufacturing Engineer, Motors",
    # Text appended after the title must not rescue it:
    "Data Science Intern (Winter 2027) Early Career • San Francisco • Full time",
]


class RoleFilterTests(MonitorTestCase):
    def setUp(self):
        super().setUp()
        self.patterns = jm.compile_role_filter(REPO_ROLE_FILTER)

    def recent_email(self, hours_ago=0):
        stamp = (datetime.now() - timedelta(hours=hours_ago)).isoformat(timespec="seconds")
        jm.save_state({}, last_email=stamp)

    def pool_titles(self):
        return [p["title"] for p in self.saved_state().get(jm.PENDING_LEFT_OUT_KEY, [])]

    def run_filtered(self, targets=None, **kwargs):
        kwargs.setdefault("email", WORKING_EMAIL)
        kwargs.setdefault("role_filter", REPO_ROLE_FILTER)
        return self.run_monitor(targets or [target()], **kwargs)

    def test_shipped_filter_keeps_wanted_titles_and_leaves_out_the_rest(self):
        wrongly_left_out = {t: jm.role_exclusion_reason(t, self.patterns) for t in MUST_KEEP
                            if jm.role_exclusion_reason(t, self.patterns)}
        wrongly_kept = [t for t in MUST_LEAVE_OUT if not jm.role_exclusion_reason(t, self.patterns)]
        self.assertEqual(wrongly_left_out, {})
        self.assertEqual(wrongly_kept, [])

    def test_unicode_dashes_and_ampersands(self):
        keep = ["Full\u2011Stack Engineer (Frontend focus)", "Software Engineer Intern / New\u2011Grad",
                "Full\u2013Stack Engineer"]
        self.assertEqual({t: jm.role_exclusion_reason(t, self.patterns) for t in keep}, {t: None for t in keep})
        self.assertIsNotNone(jm.role_exclusion_reason("FP&A Analyst", self.patterns))

    def test_no_role_filter_configured_keeps_everything(self):
        self.assertIsNone(jm.compile_role_filter(None))
        self.assertIsNone(jm.role_exclusion_reason("Senior Staff Principal Recruiter", None))

    def test_matches_are_alerted_and_left_out_postings_listed_in_the_same_email(self):
        self.mark_known(URL)
        self.pages[URL] = page(*jobs_named("Software Engineer, New Grad", "Senior Software Engineer", "Account Executive"))
        ok, reported = self.run_filtered()
        self.assertTrue(ok)
        self.assertEqual(reported, {"Acme": ["Software Engineer, New Grad"]})
        self.assertEqual(self.left_out, {"Acme": ["Senior Software Engineer", "Account Executive"]})
        [(_, msg)] = self.emails_sent()
        body = msg.get_payload()[0].get_payload(decode=True).decode()
        self.assertIn("Senior Software Engineer  [senior (Senior)]", body)
        self.assertIn("+2 left out by filters", msg["Subject"])
        self.assertEqual(self.pool_titles(), [])

        # Everything was shown, so nothing comes back.
        _, reported = self.run_filtered()
        self.assertEqual((reported, self.left_out), ({}, {}))

    def test_left_out_postings_wait_for_the_next_email(self):
        self.mark_known(URL)
        self.recent_email(hours_ago=1)
        self.pages[URL] = page(*jobs_named("Senior Software Engineer"))
        ok, _ = self.run_filtered()
        self.assertTrue(ok)
        self.assertEqual(self.emails_attempted, 0)
        self.assertEqual(self.pool_titles(), ["Senior Software Engineer"])
        self.assertEqual(len(self.saved_state()[URL]), 1)  # seen, so never re-detected

        self.pages[URL] = page(*jobs_named("Senior Software Engineer", "Backend Engineer"))
        _, reported = self.run_filtered()
        self.assertEqual(reported, {"Acme": ["Backend Engineer"]})
        self.assertEqual(self.left_out, {"Acme": ["Senior Software Engineer"]})
        self.assertEqual(self.pool_titles(), [])

    def test_left_out_posting_is_listed_even_after_it_leaves_the_page(self):
        self.mark_known(URL)
        self.recent_email(hours_ago=1)
        self.pages[URL] = page(*jobs_named("Staff Engineer, Developer Tools"))
        self.run_filtered()
        self.pages[URL] = page(*jobs_named("Backend Engineer"))  # it scrolled off / closed
        _, reported = self.run_filtered()
        self.assertEqual(reported, {"Acme": ["Backend Engineer"]})
        self.assertEqual(self.left_out, {"Acme": ["Staff Engineer, Developer Tools"]})

    def test_digest_goes_out_when_no_email_for_the_configured_hours(self):
        self.mark_known(URL)
        self.recent_email(hours_ago=25)
        self.pages[URL] = page(*jobs_named("Senior Software Engineer"))
        _, reported = self.run_filtered(filtered_digest_hours=48)
        self.assertEqual(self.emails_attempted, 0)  # 25h < 48h

        ok, reported = self.run_filtered(filtered_digest_hours=24)
        self.assertTrue(ok)
        self.assertEqual(reported, {})
        self.assertEqual(self.left_out, {"Acme": ["Senior Software Engineer"]})
        [(_, msg)] = self.emails_sent()
        self.assertIn("Digest: 1 posting left out", msg["Subject"])
        self.assertEqual(self.pool_titles(), [])

        # The digest reset the timer.
        self.pages[URL] = page(*jobs_named("Senior Software Engineer", "Staff Engineer"))
        self.run_filtered()
        self.assertEqual(self.emails_attempted, 0)
        self.assertEqual(self.pool_titles(), ["Staff Engineer"])

    def test_digest_is_due_when_no_email_was_ever_recorded(self):
        self.mark_known(URL)
        self.pages[URL] = page(*jobs_named("Senior Software Engineer"))
        self.run_filtered()
        self.assertEqual(self.left_out, {"Acme": ["Senior Software Engineer"]})
        self.assertIn(jm.LAST_EMAIL_KEY, self.saved_state())

    def test_bad_digest_hours_falls_back_to_a_day(self):
        self.mark_known(URL)
        self.recent_email(hours_ago=1)
        self.pages[URL] = page(*jobs_named("Backend Engineer", "Senior Software Engineer"))
        for bad in (None, "", "daily", -5):
            with self.subTest(bad=bad), self.assertLogs(jm.log, "WARNING"):
                self.assertEqual(jm._digest_hours({"filtered_digest_hours": bad}), 24.0)
        ok, reported = self.run_filtered(filtered_digest_hours="daily")
        self.assertTrue(ok)
        self.assertEqual(reported, {"Acme": ["Backend Engineer"]})

    def test_failed_email_keeps_left_out_postings_for_the_retry(self):
        self.mark_known(URL)
        self.pages[URL] = page(*jobs_named("Backend Engineer", "Senior Software Engineer"))
        FakeSMTP.fail_with = smtplib.SMTPAuthenticationError(535, b"bad password")
        ok, _ = self.run_filtered()
        self.assertFalse(ok)
        self.assertEqual(self.saved_state()[URL], [])
        self.assertEqual(self.pool_titles(), ["Senior Software Engineer"])

        FakeSMTP.fail_with = None
        _, reported = self.run_filtered()
        self.assertEqual(reported, {"Acme": ["Backend Engineer"]})
        self.assertEqual(self.left_out, {"Acme": ["Senior Software Engineer"]})
        self.assertEqual(self.pool_titles(), [])

    def test_configs_sharing_state_share_the_pool(self):
        other = "https://other.example/careers"
        self.mark_known(URL, other)
        self.recent_email(hours_ago=1)
        self.pages[other] = page(*jobs_named("Senior Engineer"))
        self.run_filtered([target(name="Other", url=other)])  # batch 2: left-outs only
        self.assertEqual(self.emails_attempted, 0)
        self.pages[URL] = page(*jobs_named("Backend Engineer"))
        _, reported = self.run_filtered([target()])  # batch 1: a match
        self.assertEqual(reported, {"Acme": ["Backend Engineer"]})
        self.assertEqual(self.left_out, {"Other": ["Senior Engineer"]})

    def test_show_filtered_off_restores_quiet_filtering(self):
        self.mark_known(URL)
        self.pages[URL] = page(*jobs_named("Senior Software Engineer"))
        ok, _ = self.run_filtered(show_filtered=False)
        self.assertTrue(ok)
        self.assertEqual(self.emails_attempted, 0)
        self.assertEqual(len(self.saved_state()[URL]), 1)
        self.assertNotIn(jm.PENDING_LEFT_OUT_KEY, self.saved_state())

    def test_keyword_only_configs_stay_quiet_by_default(self):
        self.mark_known(URL)
        self.pages[URL] = page(*jobs_named("Software Engineer", "Quant Trader"))
        _, reported = self.run_monitor([target()], keyword_filters=["engineer"])
        self.assertEqual(reported, {"Acme": ["Software Engineer"]})
        self.assertEqual(self.left_out, {})
        self.assertEqual(set(self.saved_state()), {URL})

    def test_keyword_leftovers_listed_when_asked(self):
        self.mark_known(URL)
        self.pages[URL] = page(*jobs_named("Software Engineer", "Quant Trader"))
        _, reported = self.run_monitor([target()], keyword_filters=["engineer"], show_filtered=True)
        self.assertEqual(reported, {"Acme": ["Software Engineer"]})
        self.assertEqual(self.left_out, {"Acme": ["Quant Trader"]})

    def test_two_targets_with_the_same_name_both_alert(self):
        other = "https://other.example/careers"
        self.mark_known(URL, other)
        self.pages[URL] = page(*jobs_named("Backend Engineer"))
        self.pages[other] = page(*jobs_named("Data Engineer"))
        _, reported = self.run_monitor([target(), target(url=other)])
        self.assertEqual(reported, {"Acme": ["Backend Engineer", "Data Engineer"]})

    def test_email_parts_are_utf8_encoded_without_long_lines(self):
        self.mark_known(URL)
        self.pages[URL] = page(*jobs_named("Backend Engineer", *[f"Senior Engineer {i}" for i in range(200)]))
        self.run_filtered()
        [(_, msg)] = self.emails_sent()
        for part in msg.get_payload():
            self.assertEqual(part.get_content_charset(), "utf-8")
        raw = msg.as_string()
        self.assertLessEqual(max(len(line) for line in raw.splitlines()), 998)

    def test_html_email_escapes_scraped_text(self):
        html = jm.format_html_report(
            {"A&B <Co>": [{"title": "Engineer <script>", "url": 'https://x.example/?a=1&b="2"'}]},
            {"A&B <Co>": [{"title": "Senior <b>", "url": "https://x.example/2", "reason": "senior (Senior)"}]},
        )
        self.assertNotIn("<script>", html)
        self.assertNotIn("<b>", html)
        self.assertIn("Engineer &lt;script&gt;", html)
        self.assertIn("A&amp;B &lt;Co&gt;", html)
        self.assertIn('href="https://x.example/?a=1&amp;b=&quot;2&quot;"', html)


# ===================================================================
# Job-board API readers and the US filter
# ===================================================================
GH_API = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
ASHBY_API = "https://api.ashbyhq.com/posting-api/job-board/acme"
LEVER_API = "https://api.lever.co/v0/postings/acme"
ORACLE_API = ("https://acme.fa.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
              "?onlyData=true&finder=findReqs;siteNumber=CX_1001,limit=50,sortBy=POSTING_DATES_DESC")

GREENHOUSE_PAYLOAD = {"jobs": [
    {"id": 1, "title": "Backend Engineer", "absolute_url": "https://acme.example/careers?gh_jid=1",
     "location": {"name": "NY - New York"}, "departments": [{"id": 89007, "name": "University"}], "offices": []},
    {"id": 2, "title": "Data Engineer", "absolute_url": "https://acme.example/careers?gh_jid=2",
     "location": {"name": "London, UK"}, "departments": [{"id": 5, "name": "Data"}], "offices": []},
    {"id": 3, "title": "ML Engineer", "absolute_url": "https://acme.example/careers?gh_jid=3",
     "location": {"name": "Remote"}, "departments": [{"id": 89007, "name": "University"}], "offices": []},
]}
ASHBY_PAYLOAD = {"jobs": [
    {"title": "Infra Engineer", "jobUrl": "https://jobs.ashbyhq.com/acme/a1", "isListed": True,
     "department": "Engineering", "team": "Infra", "location": "New York", "secondaryLocations": [],
     "address": {"postalAddress": {"addressCountry": "United States"}}},
    {"title": "Hidden Role", "jobUrl": "https://jobs.ashbyhq.com/acme/a2", "isListed": False,
     "department": "Engineering", "location": "New York", "secondaryLocations": []},
    {"title": "Sales Lead", "jobUrl": "https://jobs.ashbyhq.com/acme/a3", "isListed": True,
     "department": "Sales", "location": "Toronto", "secondaryLocations": [],
     "address": {"postalAddress": {"addressCountry": "Canada"}}},
    {"title": "Platform Engineer", "jobUrl": "https://jobs.ashbyhq.com/acme/a4", "isListed": True,
     "department": "Engineering", "location": "Remote - US",
     "secondaryLocations": [{"location": "London", "address": {"postalAddress": {"addressCountry": "United Kingdom"}}}]},
]}
LEVER_PAYLOAD = [
    {"text": "Backend Engineer - Music", "hostedUrl": "https://jobs.lever.co/acme/l1", "country": "US",
     "categories": {"location": "New York, NY", "allLocations": ["New York, NY"]}},
    {"text": "Android Engineer", "hostedUrl": "https://jobs.lever.co/acme/l2", "country": "GB",
     "categories": {"location": "London", "allLocations": ["London", "Stockholm"]}},
]
ORACLE_PAYLOAD = {"items": [{"TotalJobsCount": 2, "requisitionList": [
    {"Id": "210001", "Title": "Software Engineer I", "PrimaryLocation": "Plano, TX, United States", "PrimaryLocationCountry": "US"},
    {"Id": "210002", "Title": "Software Engineer I", "PrimaryLocation": "Glasgow, United Kingdom", "PrimaryLocationCountry": "GB"},
]}]}


class JobBoardReaderTests(MonitorTestCase):
    def serve(self, url, payload):
        self.pages[url] = json.dumps(payload)

    def test_greenhouse_reader(self):
        self.serve(GH_API, GREENHOUSE_PAYLOAD)
        jobs = jm.fetch_target_jobs(GH_API, "browser", "", "")
        self.assertEqual([j["title"] for j in jobs], ["Backend Engineer", "Data Engineer", "ML Engineer"])
        self.assertEqual(jobs[0], {"title": "Backend Engineer", "url": "https://acme.example/careers?gh_jid=1",
                                   "location": "NY - New York"})

    def test_greenhouse_department_filter_uses_content(self):
        self.serve(GH_API, GREENHOUSE_PAYLOAD)
        jobs = jm.fetch_target_jobs(GH_API + "?departments%5B%5D=89007", "html", "", "")
        self.assertEqual([j["title"] for j in jobs], ["Backend Engineer", "ML Engineer"])
        self.assertEqual(self.requests_made[-1], (GH_API, {"content": "true"}))

    def test_ashby_reader_skips_unlisted_and_filters_departments(self):
        self.serve(ASHBY_API, ASHBY_PAYLOAD)
        jobs = jm.fetch_target_jobs(ASHBY_API, "browser", "", "")
        self.assertEqual([j["title"] for j in jobs], ["Infra Engineer", "Sales Lead", "Platform Engineer"])
        self.assertEqual(jobs[0]["countries"], ["United States"])
        self.assertEqual(jobs[2]["countries"], [])  # primary has no address: the text decides
        jobs = jm.fetch_target_jobs(ASHBY_API + "?department=Engineering", "browser", "", "")
        self.assertEqual([j["title"] for j in jobs], ["Infra Engineer", "Platform Engineer"])

    def test_lever_reader_passes_filters_and_forces_json(self):
        self.serve(LEVER_API, LEVER_PAYLOAD)
        jobs = jm.fetch_target_jobs(LEVER_API + "?mode=html&department=Engineering&location=New%20York%2C%20NY", "html", "", "")
        self.assertEqual([(j["title"], j["countries"]) for j in jobs],
                         [("Backend Engineer - Music", ["US"]), ("Android Engineer", ["GB"])])
        _, params = self.requests_made[-1]
        self.assertIn(("mode", "json"), params)
        self.assertNotIn(("mode", "html"), params)
        self.assertIn(("department", "Engineering"), params)

    def test_oracle_reader_builds_detail_urls(self):
        self.serve(ORACLE_API, ORACLE_PAYLOAD)
        jobs = jm.fetch_target_jobs(ORACLE_API, "browser", "", "")
        self.assertEqual(jobs[0]["url"], "https://acme.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/210001")
        self.assertEqual(jobs[1]["countries"], ["GB"])

    def test_failed_api_read_skips_the_target_without_a_browser_fallback(self):
        with mock.patch.object(jm, "fetch_browser") as browser, self.assertLogs(jm.log, "ERROR"):
            self.assertIsNone(jm.fetch_target_jobs(GH_API, "browser", "", ""))
        browser.assert_not_called()

    def test_other_urls_keep_their_old_path(self):
        self.assertIsNone(jm.job_board_api_reader("https://jobs.ashbyhq.com/acme"))
        self.assertIsNone(jm.job_board_api_reader("https://job-boards.greenhouse.io/acme"))
        self.assertIsNone(jm.job_board_api_reader("https://jobs.lever.co/acme"))


# Trimmed from https://www.citadel.com/career-sitemap.xml (Yoast SEO layout).
CITADEL_SITEMAP = "https://www.citadel.com/career-sitemap.xml"
CITADEL_SITEMAP_XML = """<?xml version="1.0" encoding="UTF-8"?><?xml-stylesheet type="text/xsl" href="//www.citadel.com/wp-content/plugins/wordpress-seo/css/main-sitemap.xsl"?>
<urlset xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:image="http://www.google.com/schemas/sitemap-image/1.1" xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:xhtml="http://www.w3.org/1999/xhtml">
\t<url>
\t\t<loc>https://www.citadel.com/careers/</loc>
\t\t<lastmod>2026-10-05T18:14:18+00:00</lastmod>
\t</url>
\t<url>
\t\t<loc>https://www.citadel.com/careers/details/software-engineer-university-graduate-us/</loc>
\t\t<xhtml:link rel="alternate" hreflang="en" href="https://www.citadel.com/careers/details/software-engineer-university-graduate-us/" />
\t\t<lastmod>2026-10-05T18:14:19+00:00</lastmod>
\t</url>
\t<url><loc>https://www.citadel.com/careers/details/software-engineer-university-graduate-europe/</loc></url>
\t<url><loc>https://www.citadel.com/careers/details/global-quantitative-strategies-c-quantitative-research-engineer/</loc></url>
\t<url><loc>https://www.citadel.com/careers/details/quantitative-trader-university-graduate-us-new-york/</loc></url>
\t<url><loc>https://www.citadel.com/careers/details/c-software-engineer-2/</loc></url>
\t<url><loc>https://www.citadel.com/careers/details/machine-learning-researcher-phd-graduate-asia/</loc></url>
\t<url><loc>https://www.citadel.com/careers/details/us-physical-gas-specialist/</loc></url>
</urlset>"""

# Real rows and lookups from www.tesla.cn's careers state (same app as www.tesla.com),
# plus a US site laid out as the geo data nests it there (site -> states -> cities).
TESLA_PAGE = "https://www.tesla.com/careers/search/?region=5&country=US&sort=created_desc"
TESLA_STATE = "https://www.tesla.com/cua-api/apps/careers/state"
TESLA_PAYLOAD = {
    "lookup": {
        "regions": {"2": "Asia Pacific", "5": "North America"},
        "sites": {"CN": "China Mainland", "US": "United States"},
        "locations": {"27665": "上海, Shanghai", "35341": "金华, Zhejiang", "1001": "Palo Alto, California",
                      "1002": "Austin, Texas", "1003": "Fremont, California"},
        "departments": {"1": "Engineering & Information Technology", "2": "Vehicle Service"},
        "types": {"1": "fulltime", "2": "parttime", "3": "intern"},
    },
    "departments": {"1": ["1"], "2": ["74"]},
    "geo": [
        {"id": "2", "sites": [{"id": "CN", "cities": {"上海": ["27665"], "金华": ["35341"]}}]},
        {"id": "5", "sites": [{"id": "US", "states": [
            {"id": "CA", "name": "California", "cities": {"Palo Alto": ["1001"], "Fremont": ["1003"]}},
            {"id": "TX", "name": "Texas", "cities": {"Austin": ["1002"]}},
        ]}]},
    ],
    "listings": [
        {"id": "146570", "t": "服务顾问-浙江金华金东钣喷", "dp": "2", "f": "74", "l": "35341", "y": 1, "sp": 51, "pu": None},
        {"id": "146211", "t": "Senior Site Reliability Engineer, Fleetnet", "dp": "1", "f": "1", "l": "27665", "y": 1, "sp": 60, "pu": None},
        {"id": "249435", "t": "Sr. Software QA Engineer, Mobile Apps, Service & Roadside Assistance",
         "dp": "1", "f": "1", "l": "1001", "y": 1, "sp": 1, "pu": None},
        {"id": "250001", "t": "Software Engineer, Vehicle Firmware", "dp": "1", "f": "1", "l": "1002", "y": 1, "sp": 2, "pu": None},
        {"id": "250002", "t": "Internship, Software Engineer (Winter 2027)", "dp": "1", "f": "1", "l": "1003", "y": 3, "sp": 3, "pu": None},
        {"id": "250003", "t": "Backend Engineer, Energy", "dp": "1", "f": "1", "l": "9999", "y": 1, "sp": 4, "pu": None},
    ],
}


class SitemapReaderTests(MonitorTestCase):
    def test_job_pages_titles_and_regions_come_from_the_sitemap(self):
        self.pages[CITADEL_SITEMAP] = CITADEL_SITEMAP_XML
        jobs = jm.fetch_target_jobs(CITADEL_SITEMAP, "sitemap", "/careers/details/", "")
        self.assertEqual([(j["title"], j["location"], j["countries"]) for j in jobs], [
            ("Software Engineer University Graduate", "US", ["US"]),
            ("Software Engineer University Graduate", "Europe", []),
            ("Global Quantitative Strategies C++ Quantitative Research Engineer", "", []),
            ("Quantitative Trader University Graduate", "New York, US", ["US"]),
            ("C++ Software Engineer", "", []),
            ("Machine Learning Researcher PhD Graduate", "Asia", []),
            ("US Physical Gas Specialist", "", []),
        ])
        self.assertEqual(jobs[0]["url"], "https://www.citadel.com/careers/details/software-engineer-university-graduate-us/")

    def test_us_in_a_title_is_not_read_as_a_region(self):
        job = jm._job_from_slug_url("https://www.citadel.com/careers/details/software-engineer-us-equities/")
        self.assertEqual((job["title"], job["location"]), ("Software Engineer US Equities", ""))

    def test_a_job_in_the_us_and_elsewhere_is_never_skipped(self):
        for slug, location in (("software-engineer-new-york-london", "New York / London"),
                               ("software-engineer-us-europe", "US / Europe"),
                               ("software-engineer-us-salt-lake-city", "Salt Lake City, US")):
            job = jm._job_from_slug_url(f"https://www.citadel.com/careers/details/{slug}/")
            self.assertEqual((job["title"], job["location"]), ("Software Engineer", location))
            self.assertFalse(jm.is_outside_us(job), slug)

    def test_xml_that_is_not_a_list_of_pages_skips_the_target(self):
        # e.g. a sitemap index: reading it as "no jobs" would record an empty first check.
        self.pages[CITADEL_SITEMAP] = ('<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/'
                                       'sitemap/0.9"><sitemap><loc>https://www.citadel.com/career-sitemap.xml</loc>'
                                       '</sitemap></sitemapindex>')
        with self.assertLogs(jm.log, "WARNING"):
            self.run_monitor([target(name="Citadel", url=CITADEL_SITEMAP, mode="sitemap",
                                     link_selector="/careers/details/")])
        self.assertFalse(jm.STATE_PATH.exists())

    def test_run_alerts_new_us_job_pages_and_skips_other_regions(self):
        self.mark_known(CITADEL_SITEMAP)
        self.pages[CITADEL_SITEMAP] = CITADEL_SITEMAP_XML
        _, reported = self.run_monitor(
            [target(name="Citadel", url=CITADEL_SITEMAP, mode="sitemap", link_selector="/careers/details/")],
            us_only=True)
        self.assertEqual(reported["Citadel"], [
            "Software Engineer University Graduate", "Global Quantitative Strategies C++ Quantitative Research Engineer",
            "Quantitative Trader University Graduate", "C++ Software Engineer", "US Physical Gas Specialist"])

    def test_unreadable_sitemap_skips_the_target(self):
        self.pages[CITADEL_SITEMAP] = "<html><body>Access denied</body>"
        with mock.patch.object(jm, "fetch_browser") as browser, self.assertLogs(jm.log, "ERROR"):
            self.assertIsNone(jm.fetch_target_jobs(CITADEL_SITEMAP, "sitemap", "/careers/details/", ""))
        browser.assert_not_called()


class TeslaReaderTests(MonitorTestCase):
    def setUp(self):
        super().setUp()
        self.pages[TESLA_STATE] = json.dumps(TESLA_PAYLOAD)

    def test_careers_page_url_reads_the_jobs_data_for_its_country(self):
        with mock.patch.object(jm, "fetch_browser") as browser:
            jobs = jm.fetch_target_jobs(TESLA_PAGE, "browser", "a[href*='/careers/search/job/']", "")
        browser.assert_not_called()
        self.assertEqual([j["title"] for j in jobs], [
            "Sr. Software QA Engineer, Mobile Apps, Service & Roadside Assistance",
            "Software Engineer, Vehicle Firmware",
            "Internship, Software Engineer (Winter 2027)",
            "Backend Engineer, Energy",  # location missing from geo: kept, its text decides
        ])
        # Same form as the job links on tesla.com (e.g. an indexed posting).
        self.assertEqual(jobs[0]["url"], "https://www.tesla.com/careers/search/job/"
                                         "sr-software-qa-engineer-mobile-apps-service-roadside-assistance-249435")
        self.assertEqual((jobs[0]["location"], jobs[0]["countries"]), ("Palo Alto, California", ["US"]))
        self.assertEqual(jobs[3]["countries"], [])

    def test_an_empty_record_from_a_blocked_check_is_a_silent_baseline(self):
        # Before blocked checks were skipped, a blocked first check recorded no jobs.
        jm.save_state({TESLA_PAGE: []})
        tesla = target(name="Tesla", url=TESLA_PAGE, mode="browser", link_selector="a[href*='/careers/search/job/']")
        _, reported = self.run_monitor([tesla])
        self.assertEqual(reported, {})
        self.assertEqual(len(self.saved_state()[TESLA_PAGE]), 4)

    def test_a_retitled_posting_is_not_new(self):
        tesla = target(name="Tesla", url=TESLA_PAGE, mode="browser", link_selector="a[href*='/careers/search/job/']")
        self.run_monitor([tesla])
        payload = json.loads(self.pages[TESLA_STATE])
        payload["listings"][3]["t"] = "Software Engineer, Vehicle Firmware (Autopilot)"
        self.pages[TESLA_STATE] = json.dumps(payload)
        _, reported = self.run_monitor([tesla])
        self.assertEqual(reported, {})
        # A link without the title (or with another one) is the same posting too.
        same = [{"url": "https://www.tesla.com/careers/search/job/250001"},
                {"url": "https://www.tesla.com/careers/search/job/software-engineer-vehicle-firmware-250001/"}]
        self.assertEqual(jm.compute_job_id(same[0]), jm.compute_job_id(same[1]))
        self.assertNotEqual(jm.compute_job_id(same[0]),
                            jm.compute_job_id({"url": "https://www.tesla.com/careers/search/job/250002"}))

    def test_unexpected_or_empty_jobs_data_falls_back_to_the_page(self):
        for broken in ({**TESLA_PAYLOAD, "geo": 5}, {**TESLA_PAYLOAD, "geo": [{"id": "5", "sites": [["1001"]]}]},
                       {**TESLA_PAYLOAD, "listings": []}):
            self.pages[TESLA_STATE] = json.dumps(broken)
            with self.assertLogs(jm.log, "WARNING"):
                self.assertIsNone(jm.fetch_tesla_jobs(TESLA_PAGE))

    def test_site_and_type_parameters(self):
        everywhere = jm.fetch_tesla_jobs("https://www.tesla.com/careers/search/")
        self.assertEqual(len(everywhere), len(TESLA_PAYLOAD["listings"]))
        with self.assertLogs(jm.log, "INFO") as logs:
            jm.fetch_tesla_jobs(TESLA_PAGE + "&department=1")
        self.assertIn("ignoring department", "\n".join(logs.output))
        cn = jm.fetch_tesla_jobs("https://www.tesla.com/careers/search/?country=CN")
        self.assertEqual([j["url"] for j in cn], [
            "https://www.tesla.com/careers/search/job/146570",  # no latin letters: id only
            "https://www.tesla.com/careers/search/job/senior-site-reliability-engineer-fleetnet-146211",
            "https://www.tesla.com/careers/search/job/backend-engineer-energy-250003",  # site unknown: kept
        ])
        interns = jm.fetch_tesla_jobs(TESLA_STATE + "?site=US&type=intern")
        self.assertEqual([j["title"] for j in interns], ["Internship, Software Engineer (Winter 2027)"])
        self.assertEqual(jm.fetch_tesla_jobs(TESLA_STATE + "?site=US&type=3"), interns)

    def test_blocked_jobs_data_falls_back_to_the_careers_page(self):
        self.pages[TESLA_STATE] = "<html>Access Denied</html>"
        page_html = '<a href="/careers/search/job/software-engineer-vehicle-firmware-250001">Software Engineer</a>'
        with mock.patch.object(jm, "fetch_browser", return_value=(page_html, [])) as browser, \
                self.assertLogs(jm.log, "WARNING"):
            jobs = jm.fetch_target_jobs(TESLA_PAGE, "browser", "a[href*='/careers/search/job/']", "")
        browser.assert_called_once()
        self.assertEqual([j["url"] for j in jobs],
                         ["https://www.tesla.com/careers/search/job/software-engineer-vehicle-firmware-250001"])

    def test_blocked_everywhere_skips_without_recording_a_baseline(self):
        self.pages[TESLA_STATE] = "<html>Access Denied</html>"
        with mock.patch.object(jm, "fetch_browser", return_value=("<html>Access Denied</html>", [])):
            self.run_monitor([target(name="Tesla", url=TESLA_PAGE, mode="browser",
                                     link_selector="a[href*='/careers/search/job/']")])
        self.assertNotIn(TESLA_PAGE, self.saved_state() if jm.STATE_PATH.exists() else {})

    def test_blocked_state_url_is_skipped(self):
        with mock.patch.object(jm.requests, "get", return_value=FakeResponse("Forbidden", 403, "text/html")), \
                mock.patch.object(jm, "fetch_browser") as browser, self.assertLogs(jm.log, "ERROR"):
            self.assertIsNone(jm.fetch_target_jobs(TESLA_STATE, "api", "", ""))
        browser.assert_not_called()


class FakeEightfold:
    """Eightfold's search API as Morgan Stanley serves it: sorted by posting date, which is
    only a date, with each day's postings in an order that changes between visits, 10 a page."""

    def __init__(self, postings, date_only=True):
        self.postings = postings  # (id, day)
        self.date_only = date_only
        self.visit = 0
        self.calls = []
        self.fail_from = None

    def get(self, url, params=None, **kwargs):
        start = int(params["start"])
        self.calls.append(start)
        if self.fail_from is not None and start >= self.fail_from:
            raise jm.requests.ConnectionError("reset")
        def shuffled(p):
            return int(hashlib.md5(f"{p[0]}/{self.visit}".encode()).hexdigest(), 16)

        order = sorted(self.postings, key=lambda p: (-p[1], shuffled(p)))
        if not self.date_only:
            order = sorted(self.postings, key=lambda p: (-p[1], -int(p[0])))
        rows = [{"id": pid, "name": f"Software Engineer {pid}", "positionUrl": f"/careers/job/{pid}",
                 "postedTs": day * 86400 + (0 if self.date_only else int(pid))} for pid, day in order]
        return FakeResponse(json.dumps({"data": {"count": len(rows), "positions": rows[start:start + 10]}}))


MS_URL = "https://morganstanley.eightfold.ai/careers?location=United%20States"


class EightfoldNewestDaysTests(MonitorTestCase):
    def postings(self):
        # 14 postings today, 16 the posting day before, then older days.
        return ([(str(100 + i), 20000) for i in range(14)] + [(str(200 + i), 19998) for i in range(16)]
                + [(str(300 + i), 19997 - i // 5) for i in range(40)])

    def fetch(self, api):
        with mock.patch.object(jm.requests, "get", api.get):
            return jm.fetch_target_jobs(MS_URL, "eightfold", "", "")

    def test_reads_the_two_newest_posting_days_whole_in_a_fixed_order(self):
        api = FakeEightfold(self.postings())
        first = self.fetch(api)
        self.assertEqual(api.calls, [0, 10, 20, 30])  # until a third day begins
        self.assertEqual(len(first), 40)  # the third day's first rows are kept too
        self.assertEqual(first[0]["url"], "https://morganstanley.eightfold.ai/careers/job/113")
        api.visit, api.calls = 1, []
        self.assertEqual(self.fetch(api)[:30], first[:30])  # the two newest days, whole and in order

    def test_a_first_page_spanning_three_days_is_kept_whole(self):
        # e.g. a weekend: 2 postings on Monday, 1 on Sunday, 21 on Friday.
        postings = [("101", 20000), ("102", 20000), ("201", 19999)] + [(str(300 + i), 19997) for i in range(21)]
        api = FakeEightfold(postings)
        self.assertEqual(len(self.fetch(api)), 10)
        self.assertEqual(api.calls, [0])
        # A Friday posting not seen before (say the monitor was off) is alerted once it shows.
        jm.save_state({MS_URL: [{"title": f"Software Engineer {pid}",
                                 "url": f"https://morganstanley.eightfold.ai/careers/job/{pid}"}
                                for pid, _ in postings[:-1]]})
        tracked = [target(name="Morgan Stanley", url=MS_URL, mode="eightfold", link_selector="")]
        with mock.patch.object(jm.requests, "get", api.get):
            alerted = []
            for visit in range(1, 25):
                api.visit = visit
                _, reported = self.run_monitor(tracked)
                alerted += reported.get("Morgan Stanley", [])
        self.assertEqual(alerted, ["Software Engineer 320"])

    def test_an_unexpected_reply_falls_back_instead_of_crashing(self):
        with mock.patch.object(jm.requests, "get", return_value=FakeResponse('{"data": null}')), \
                self.assertLogs(jm.log, "WARNING"):
            self.assertIsNone(jm.fetch_eightfold_jobs_via_api(MS_URL))
            self.assertIsNone(jm.fetch_microsoft_jobs_via_api(
                "https://apply.careers.microsoft.com/careers?location=United+States"))

    def test_a_reshuffled_day_is_not_alerted_again(self):
        api = FakeEightfold(self.postings())
        with mock.patch.object(jm.requests, "get", api.get):
            self.run_monitor([target(name="Morgan Stanley", url=MS_URL, mode="eightfold", link_selector="")])
            for visit in range(1, 4):
                api.visit = visit
                _, reported = self.run_monitor([target(name="Morgan Stanley", url=MS_URL, mode="eightfold",
                                                       link_selector="")])
                self.assertEqual(reported, {})
            api.postings.append(("999", 20001))
            api.visit = 9
            _, reported = self.run_monitor([target(name="Morgan Stanley", url=MS_URL, mode="eightfold",
                                                   link_selector="")])
        self.assertEqual(reported, {"Morgan Stanley": ["Software Engineer 999"]})

    def test_exact_posting_times_keep_the_single_page(self):
        # Microsoft's postings carry the time too, so the order is already fixed.
        api = FakeEightfold(self.postings(), date_only=False)
        jobs = self.fetch(api)
        self.assertEqual(api.calls, [0])
        self.assertEqual(len(jobs), 10)

    def test_a_failed_later_page_keeps_what_was_read(self):
        api = FakeEightfold(self.postings())
        api.fail_from = 20
        with self.assertLogs(jm.log, "WARNING"):
            jobs = self.fetch(api)
        self.assertEqual(len(jobs), 20)

    def test_reading_stops_at_the_cap(self):
        api = FakeEightfold([(str(1000 + i), 20000) for i in range(300)])
        self.assertEqual(len(self.fetch(api)), jm.EIGHTFOLD_MAX_POSITIONS)
        self.assertEqual(len(api.calls), jm.EIGHTFOLD_MAX_POSITIONS // 10)


# Trimmed from https://jobs.intuit.com/search-jobs/results (the "results" HTML of its JSON).
INTUIT_RESULTS = ("https://jobs.intuit.com/search-jobs/results?OrganizationIds=27595&FacetFilters%5B0%5D.ID=6252001"
                  "&FacetFilters%5B0%5D.FacetType=2&FacetFilters%5B0%5D.IsApplied=true"
                  "&SearchResultsModuleName=Search+Results&SortCriteria=1&SortDirection=1&RecordsPerPage=50")
INTUIT_RESULTS_HTML = """
    <section id="search-results" data-total-results="521" data-records-per-page="50" data-sort-criteria="1" data-sort-direction="1">
        <h1>521 Results for </h1>
            <section id="search-results-list" class="search-results-list-wrapper">
                <div id="applied-filters" class="search-results-options"> <h2 id="applied-filters-label">Filtered by</h2>
                <ul aria-labelledby="applied-filters-label"> <li><button class="filter-button" data-id="6252001" data-facet-type="2">Country: United States</button></li> </ul> </div>
                <ul class="search-list">
                    <li data-remote="24525"> <a href="/job/mountain-view/senior-staff-product-designer-quickbooks-capital/27595/101575725696" class="sr-item" data-title="Senior Staff Product Designer, QuickBooks Capital"> <h2>Senior Staff Product Designer, QuickBooks Capital</h2> <span class="job-location">Mountain View, California</span> </a> <button type="button" class="js-save-job-btn" data-job-id="101575725696"><span class="wai">Save </span></button> </li>
                    <li data-remote="23124"> <a href="/job/mountain-view/senior-software-engineer/27595/101575714864" class="sr-item" data-title="Senior Software Engineer"> <h2>Senior Software Engineer</h2> <span class="job-location">Mountain View, California</span> </a> </li>
                    <li data-remote="24451"> <a href="/job/mountain-view/senior-assistant-general-counsel-privacy-data-innovation-and-protection/27595/101575714832" class="sr-item"> <h2>Senior Assistant General Counsel - Privacy, Data Innovation &amp; Protection</h2> <span class="job-location">Multiple Locations</span> </a> </li>
                </ul>
                <nav id="pagination-bottom" class="pagination"> <a class="next" href="/search-jobs/results" rel="nofollow">Next</a> </nav>
            </section>
    </section>
    <section class="related-jobs"><a class="related-jobs" href="/job/mountain-view/old-role/27595/99683387088?orgIds=27595&alp=6252001&alt=2">Old role</a></section>
"""


class TalentBrewReaderTests(MonitorTestCase):
    def test_reads_the_newest_first_results_list(self):
        self.pages[INTUIT_RESULTS] = json.dumps({"filters": "", "results": INTUIT_RESULTS_HTML, "hasJobs": True})
        with mock.patch.object(jm, "fetch_browser") as browser:
            jobs = jm.fetch_target_jobs(INTUIT_RESULTS, "api", "", "")
        browser.assert_not_called()
        self.assertEqual([(j["title"], j["location"]) for j in jobs], [
            ("Senior Staff Product Designer, QuickBooks Capital", "Mountain View, California"),
            ("Senior Software Engineer", "Mountain View, California"),
            ("Senior Assistant General Counsel - Privacy, Data Innovation & Protection", "Multiple Locations"),
        ])
        self.assertEqual(jobs[1]["url"], "https://jobs.intuit.com/job/mountain-view/senior-software-engineer/27595/101575714864")

    def test_a_location_next_to_the_link_is_read_too(self):
        # As on other TalentBrew sites (e.g. jobs.boeing.com).
        html = ('<section id="search-results"><section id="search-results-list"><ul><li>'
                '<a href="/job/seattle/software-engineer/185/1001"><h2>Software Engineer</h2></a>'
                '<span class="search-results__job-info location">Seattle, Washington</span>'
                '</li></ul></section></section>')
        url = "https://jobs.example.com/search-jobs/results?SearchResultsModuleName=Search+Results&SortCriteria=1"
        self.pages[url] = json.dumps({"results": html})
        self.assertEqual(jm.fetch_target_jobs(url, "api", "", ""), [{
            "title": "Software Engineer", "url": "https://jobs.example.com/job/seattle/software-engineer/185/1001",
            "location": "Seattle, Washington"}])

    def test_a_response_without_the_results_list_skips_the_target(self):
        self.pages[INTUIT_RESULTS] = json.dumps({"filters": "", "results": "", "hasJobs": False})
        with self.assertLogs(jm.log, "WARNING"):
            self.assertIsNone(jm.fetch_target_jobs(INTUIT_RESULTS, "api", "", ""))

    def test_the_search_page_itself_keeps_its_old_path(self):
        self.assertIsNone(jm.job_board_api_reader("https://jobs.intuit.com/search-jobs/United%20States?orgIds=27595"))


class UsFilterTests(MonitorTestCase):
    def test_location_classification(self):
        outside = ["London, UK", "Singapore", "Bengaluru, India; Mumbai, India", "Dublin OR London",
                   "Toronto, Remote-Canada", "Poland - Remote OR Romania - Remote"]
        us_or_unknown = ["NY - New York", "San Francisco, CA | New York City, NY", "United States", "Remote",
                         "", "London, United Kingdom; New York, NY, United States", "US / Canada",
                         "Remote - US", "Washington, DC", "Chicago, Toronto", "London OR New York"]
        self.assertEqual([l for l in outside if not jm.is_outside_us({"location": l})], [])
        self.assertEqual([l for l in us_or_unknown if jm.is_outside_us({"location": l})], [])
        self.assertTrue(jm.is_outside_us({"location": "Remote", "countries": ["GB"]}))
        self.assertFalse(jm.is_outside_us({"location": "London", "countries": ["GB", "US"]}))

    def test_run_skips_postings_outside_the_us(self):
        self.mark_known(GH_API)
        self.pages[GH_API] = json.dumps(GREENHOUSE_PAYLOAD)
        _, reported = self.run_monitor([target(url=GH_API)], us_only=True, role_filter=REPO_ROLE_FILTER)
        self.assertEqual(reported, {"Acme": ["Backend Engineer", "ML Engineer"]})
        self.assertEqual(self.left_out, {})  # outside-US postings aren't listed
        self.assertEqual(len(self.saved_state()[GH_API]), 3)  # but they're recorded as seen

    def test_us_only_can_be_turned_off_per_target(self):
        self.mark_known(GH_API)
        self.pages[GH_API] = json.dumps(GREENHOUSE_PAYLOAD)
        _, reported = self.run_monitor([target(url=GH_API, us_only=False)], us_only=True)
        self.assertEqual(reported, {"Acme": ["Backend Engineer", "Data Engineer", "ML Engineer"]})

    def test_location_is_shown_in_the_email(self):
        report = jm.format_plain_report({"Acme": [{"title": "Backend Engineer", "url": "u", "location": "NY - New York"}]})
        self.assertIn("* Backend Engineer (NY - New York)", report)


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

    @unittest.skipIf(sys.platform == "win32", "bash script")
    def test_periodic_script_with_missing_config_json_writes_a_starter(self):
        shutil.copy(REPO_DIR / "run_periodic_monitor.sh", self.tmp)
        shutil.copy(REPO_DIR / "config_all.json", self.tmp)
        # The script prefers .venv/bin/python (made by setup.sh); point it at this interpreter.
        venv_python = self.tmp / ".venv" / "bin" / "python"
        venv_python.parent.mkdir(parents=True)
        venv_python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
        venv_python.chmod(0o755)
        result = subprocess.run(
            ["bash", "run_periodic_monitor.sh", "config.json", "1"],
            cwd=self.tmp, capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((self.tmp / "config.json").read_text()), jm.DEFAULT_CONFIG)

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
