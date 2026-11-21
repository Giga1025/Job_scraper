"""
Offline tests for job_monitor.py — no network, no real email.

Run from the repo root:
    python -m unittest discover -s tests -v
"""

import copy
import email.header
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_DIR))

import job_monitor as jm  # noqa: E402
from boards import browser as browser_module  # noqa: E402
from boards.browser import Browser as RealBrowser  # noqa: E402

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
            page = self.pages[url]
            return page if isinstance(page, FakeResponse) else FakeResponse(page)  # (a response: its status and headers too)
        raise jm.requests.ConnectionError(f"no fake page for {url}")

    def fake_post(self, url, *args, **kwargs):
        raise jm.requests.ConnectionError(f"no fake page for {url}")

    def run_monitor(self, targets, email=None, keyword_filters=None, config_file="config.json", **config_extra):
        """Run one monitor cycle. Returns (run() result, {target name: [new titles]}).
        Titles listed in the email's left-out section end up in self.left_out."""
        config = {
            "email": email or {"enabled": False},
            "keyword_filters": keyword_filters or [],
            "targets": targets,
            **config_extra,
        }
        config_path = self.tmp / config_file
        config_path.write_text(json.dumps(config))

        reported = {}
        self.left_out = {}
        self.emails_attempted = 0
        real_send_email = jm.send_email

        def spy(cfg, all_new, left_out=None, left_out_summary=None, problems=None, **extra):
            self.emails_attempted += 1
            self.problems = problems
            self.email_extra = extra  # (alarms, top picks)
            reported.update({name: [j["title"] for j in jobs] for name, jobs in all_new.items()})
            self.left_out = {name: [j["title"] for j in jobs] for name, jobs in (left_out or {}).items()}
            self.left_out_summary = left_out_summary
            return real_send_email(cfg, all_new, left_out=left_out, left_out_summary=left_out_summary, problems=problems, **extra)

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
        # Recorded as seen, and held until an email gets through (even if it leaves the page).
        self.assertEqual([j["title"] for j in self.saved_state()[jm.ALERT_POOL_KEY]], ["Data Analyst"])
        self.pages[URL] = page()

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


class SplitConfigTests(MonitorTestCase):
    def test_target_files_are_added_after_the_configs_own_targets(self):
        (self.tmp / "targets").mkdir()
        (self.tmp / "targets" / "big.json").write_text(json.dumps({"_help": "x", "targets": [target(name="Big")]}))
        (self.tmp / "targets" / "small.json").write_text(json.dumps([target(name="Small")]))
        path = self.tmp / "config_all.json"
        path.write_text(json.dumps({"targets": [target(name="Own")], "target_files": ["targets/big.json", "targets/small.json"]}))
        self.assertEqual([t["name"] for t in jm.load_config(path)["targets"]], ["Own", "Big", "Small"])

    def test_a_missing_target_file_stops_the_run(self):
        path = self.tmp / "config_all.json"
        path.write_text(json.dumps({"target_files": ["targets/nope.json"]}))
        with self.assertLogs(jm.log, "ERROR"), self.assertRaises(SystemExit):
            jm.load_config(path)

    def test_the_shipped_config_is_split_into_three_files(self):
        config = jm.load_config(REPO_DIR / "config_all.json")
        self.assertEqual(config["target_files"], ["targets/big_tech.json", "targets/new_grad_lists.json", "targets/startups.json"])
        modes = [t.get("mode") for t in config["targets"] if not t.get("_section")]
        self.assertEqual((modes.count("feed"), modes.count("boards")), (3, 1))
        self.assertGreater(len(modes), 60)


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
        self.assertNotIn(URL, self.saved_state())  # (only the failure is noted, under _health)
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
        self.assertEqual([j["title"] for j in self.saved_state()[jm.ALERT_POOL_KEY]], ["Data Analyst"])  # held
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
        self.assertEqual([j["title"] for j in self.saved_state()[jm.ALERT_POOL_KEY]], ["i1"])  # held

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
class PausedTargetTests(MonitorTestCase):
    def test_a_paused_target_is_not_checked_and_keeps_its_saved_jobs(self):
        other = "https://other.example/careers"
        jm.save_state({URL: [{"title": "Old Role", "url": "https://acme.example/jobs/old"}]})
        self.mark_known(other)
        self.pages[URL] = page(*jobs_named("Backend Engineer"))
        self.pages[other] = page(*jobs_named("Data Engineer"))
        with self.assertLogs(jm.log, "INFO") as logs:
            _, reported = self.run_monitor([target(enabled=False), target(name="Other", url=other)])
        self.assertEqual(reported, {"Other": ["Data Engineer"]})
        self.assertNotIn(URL, [u for u, _ in self.requests_made])
        self.assertEqual(self.saved_state()[URL], [{"title": "Old Role", "url": "https://acme.example/jobs/old"}])
        self.assertIn('Paused ("enabled": false): Acme', "\n".join(logs.output))
        # Resuming picks up where it left off: what it lists now that wasn't there is new.
        _, reported = self.run_monitor([target()])
        self.assertEqual(reported, {"Acme": ["Backend Engineer"]})

    def test_the_shipped_config_pauses_only_the_blocked_entries(self):
        targets = jm.load_config(REPO_DIR / "config_all.json")["targets"]
        paused = [t["name"] for t in targets if t.get("name") and t.get("enabled", True) is False]
        self.assertEqual(paused, ["Tesla", "Citadel", "Citadel Securities", "Atlassian"])


class SameCompanyTests(MonitorTestCase):
    """A company checked through several entries (same name), e.g. its new-grad page and its
    full US list: each posting is emailed once, whichever entry finds it first."""

    ALL = "https://acme.example/careers/all"

    def test_a_posting_found_through_one_entry_is_not_new_under_another(self):
        self.mark_known(URL, self.ALL)
        self.pages[URL] = page(*jobs_named("Backend Engineer"))
        self.pages[self.ALL] = page()
        targets = [target(), target(url=self.ALL)]
        _, reported = self.run_monitor(targets, email=WORKING_EMAIL)
        self.assertEqual(reported, {"Acme": ["Backend Engineer"]})
        # Next run the full list shows it too (and something new): only the new one is emailed.
        self.pages[self.ALL] = page(*jobs_named("Backend Engineer", "Data Engineer"))
        with self.assertLogs(jm.log, "INFO") as logs:
            _, reported = self.run_monitor(targets, email=WORKING_EMAIL)
        self.assertEqual(reported, {"Acme": ["Data Engineer"]})
        self.assertIn("[same company] 1 of them were already found through another Acme entry", "\n".join(logs.output))
        self.assertEqual(len(self.saved_state()[self.ALL]), 2)  # still recorded under the second entry
        # And the other way round: the first entry showing it later doesn't email it again.
        self.pages[URL] = page(*jobs_named("Backend Engineer", "Data Engineer"))
        self.emails_attempted = 0
        _, reported = self.run_monitor(targets, email=WORKING_EMAIL)
        self.assertEqual((reported, self.emails_attempted), ({}, 0))
        self.assertEqual(len(self.emails_sent()), 2)

    def test_found_by_both_in_one_run_it_is_listed_once_and_so_is_a_left_out_one(self):
        # The two pages link each job differently (same Greenhouse job id), so only the
        # same-company check can tell they are the same posting.
        self.mark_known(URL, self.ALL)
        self.pages[URL] = page(("backend?gh_jid=7712345", "Backend Engineer"),
                               ("senior-backend?gh_jid=7799999", "Senior Backend Engineer"))
        self.pages[self.ALL] = page(("7712345?gh_jid=7712345", "Backend Engineer"),
                                    ("7799999?gh_jid=7799999", "Senior Backend Engineer"))
        _, reported = self.run_monitor([target(), target(url=self.ALL)], email=WORKING_EMAIL,
                                       role_filter=REPO_ROLE_FILTER)
        self.assertEqual(reported, {"Acme": ["Backend Engineer"]})
        self.assertEqual(self.left_out, {"Acme": ["Senior Backend Engineer"]})

    def test_a_left_out_posting_is_not_listed_again_by_another_entry(self):
        self.mark_known(URL, self.ALL)
        self.pages[URL] = page(*jobs_named("Backend Engineer", "Senior Backend Engineer"))
        self.pages[self.ALL] = page()
        targets = [target(), target(url=self.ALL)]
        self.run_monitor(targets, email=WORKING_EMAIL, role_filter=REPO_ROLE_FILTER)
        self.assertEqual(self.left_out, {"Acme": ["Senior Backend Engineer"]})
        self.pages[self.ALL] = page(*jobs_named("Senior Backend Engineer", "Data Engineer"))
        _, reported = self.run_monitor(targets, email=WORKING_EMAIL, role_filter=REPO_ROLE_FILTER)
        self.assertEqual((reported, self.left_out), ({"Acme": ["Data Engineer"]}, {}))

    def test_a_shared_number_that_is_not_a_posting_id_does_not_count(self):
        # Apple-style links: one role number, a different posting per city.
        self.mark_known(URL, self.ALL)
        self.pages[URL] = page(("200694453-0157/gpu-performance-engineer", "GPU Performance Engineer"))
        self.pages[self.ALL] = page()
        targets = [target(), target(url=self.ALL)]
        self.run_monitor(targets, email=WORKING_EMAIL)
        self.pages[self.ALL] = page(("200694453-1242/gpu-performance-engineer", "GPU Performance Engineer"))
        _, reported = self.run_monitor(targets, email=WORKING_EMAIL)
        self.assertEqual(reported, {"Acme": ["GPU Performance Engineer"]})

    def test_a_new_entry_listed_first_hides_nothing_from_the_existing_one(self):
        # The new entry's first check records the posting silently; the existing entry,
        # checked after it in the same run, must still email it.
        self.mark_known(URL)
        self.pages[URL] = self.pages[self.ALL] = page(*jobs_named("Backend Engineer"))
        _, reported = self.run_monitor([target(url=self.ALL), target()], email=WORKING_EMAIL)
        self.assertEqual(reported, {"Acme": ["Backend Engineer"]})
        self.assertNotIn(jm.REPORTED_MARK, self.saved_state()[self.ALL][0])  # only recorded
        self.assertEqual(self.saved_state()[URL][0][jm.REPORTED_MARK], jm.SHOWN_AS_MATCH)  # emailed

    def test_postings_saved_before_this_check_existed_hide_nothing(self):
        # A state.json from before: no marks, so it can't say which postings were emailed.
        jm.save_state({URL: [{"title": "PhD Software Engineer", "url": "https://acme.example/jobs/phd-software-engineer"}]})
        self.pages[URL] = page(*jobs_named("PhD Software Engineer"))
        self.pages[self.ALL] = page(*jobs_named("Data Engineer"))
        targets = [target(), target(url=self.ALL)]
        _, reported = self.run_monitor(targets, email=WORKING_EMAIL)  # the new entry's first check
        self.assertEqual(reported, {})
        self.pages[self.ALL] = page(*jobs_named("Data Engineer", "PhD Software Engineer"))
        _, reported = self.run_monitor(targets, email=WORKING_EMAIL)
        self.assertEqual(reported, {"Acme": ["PhD Software Engineer"]})

    def test_a_company_wide_id_in_ashby_links_is_not_a_posting_id(self):
        org = "f7d8749a-72d6-407c-bf98-292ea630c73a"
        job1, job2 = "0e7ecf76-1111-4a2b-9c3d-000000000001", "0e7ecf76-2222-4a2b-9c3d-000000000002"

        def ashby_page(*jobs):
            return "".join(f'<a href="https://jobs.ashbyhq.com/{board}/{jid}/application">{title}</a>'
                           for board, jid, title in jobs)

        self.mark_known(URL, self.ALL)
        targets = [target(link_selector="a[href*='ashbyhq']"), target(url=self.ALL, link_selector="a[href*='ashbyhq']")]
        self.pages[URL] = ashby_page((org, job1, "Software Engineer 2"))
        self.pages[self.ALL] = ashby_page()
        self.run_monitor(targets, email=WORKING_EMAIL)
        # A different posting on the same board is new; the same posting under its board name is not.
        self.pages[self.ALL] = ashby_page((org, job2, "Software Engineer 3"), ("acme", job1, "Software Engineer 2"))
        _, reported = self.run_monitor(targets, email=WORKING_EMAIL)
        self.assertEqual(reported, {"Acme": ["Software Engineer 3"]})

    def test_the_existing_entry_failing_while_the_new_one_starts_loses_nothing(self):
        self.mark_known(URL)
        self.pages[self.ALL] = page(*jobs_named("Backend Engineer"))
        targets = [target(), target(url=self.ALL)]
        with self.assertLogs(jm.log, "ERROR"):
            _, reported = self.run_monitor(targets, email=WORKING_EMAIL)  # no page at URL: it fails
        self.assertEqual(reported, {})
        self.pages[URL] = page(*jobs_named("Backend Engineer"))
        _, reported = self.run_monitor(targets, email=WORKING_EMAIL)
        self.assertEqual(reported, {"Acme": ["Backend Engineer"]})
        _, reported = self.run_monitor(targets, email=WORKING_EMAIL)  # and only once
        self.assertEqual(reported, {})

    def test_a_paused_entry_still_counts_for_its_company(self):
        self.mark_known(URL, self.ALL)
        self.pages[URL] = page(*jobs_named("Backend Engineer"))
        self.pages[self.ALL] = page()
        self.run_monitor([target(), target(url=self.ALL)], email=WORKING_EMAIL)
        self.pages[self.ALL] = page(*jobs_named("Backend Engineer"))
        _, reported = self.run_monitor([target(enabled=False), target(url=self.ALL)], email=WORKING_EMAIL)
        self.assertEqual(reported, {})

    def test_a_posting_skipped_as_outside_the_us_is_still_emailed_by_an_entry_that_wants_it(self):
        # The us_only entry records the London posting without emailing it; an entry for the
        # same company that wants every location still emails it.
        worldwide = "https://boards-api.greenhouse.io/v1/boards/acme-worldwide/jobs"
        self.mark_known(GH_API, worldwide)
        self.pages[GH_API] = json.dumps(GREENHOUSE_PAYLOAD)
        self.pages[worldwide] = json.dumps({"jobs": []})
        targets = [target(url=GH_API, mode="api"), target(url=worldwide, mode="api", us_only=False)]
        _, reported = self.run_monitor(targets, email=WORKING_EMAIL, us_only=True)
        self.assertEqual(reported, {"Acme": ["Backend Engineer", "ML Engineer"]})
        self.pages[worldwide] = json.dumps({"jobs": GREENHOUSE_PAYLOAD["jobs"][1:2]})  # the London one
        _, reported = self.run_monitor(targets, email=WORKING_EMAIL, us_only=True)
        self.assertEqual(reported, {"Acme": ["Data Engineer"]})

    def test_the_same_job_number_under_another_link_counts(self):
        other_site = "https://jobs.acme-careers.example/search"
        self.mark_known(URL, other_site)
        self.pages[URL] = page(("backend?gh_jid=7712345", "Backend Engineer"))
        self.pages[other_site] = page()
        targets = [target(), target(name="ACME, Inc.", url=other_site)]  # same company, written differently
        _, reported = self.run_monitor(targets, email=WORKING_EMAIL)
        self.assertEqual(reported, {"Acme": ["Backend Engineer"]})
        self.pages[other_site] = page(("7712345?gh_jid=7712345", "Backend Engineer"))
        _, reported = self.run_monitor(targets, email=WORKING_EMAIL)
        self.assertEqual(reported, {})

    def test_a_posting_another_entry_filtered_out_unseen_is_still_emailed(self):
        other = "https://other.example/careers"
        self.mark_known(URL, self.ALL, other)
        self.pages[URL] = page(*jobs_named("Backend Engineer"))
        self.pages[self.ALL] = self.pages[other] = page()
        # The first entry only shows titles with "Intern", so it records Backend Engineer
        # without showing it: the second entry must still email it when it lists it. (Another
        # company listing the same title is unaffected.)
        targets = [target(keyword_filters=["Intern"]), target(url=self.ALL), target(name="Other", url=other)]
        _, reported = self.run_monitor(targets, email=WORKING_EMAIL)
        self.assertEqual(reported, {})
        self.pages[self.ALL] = self.pages[other] = page(*jobs_named("Backend Engineer"))
        _, reported = self.run_monitor(targets, email=WORKING_EMAIL)
        self.assertEqual(reported, {"Acme": ["Backend Engineer"], "Other": ["Backend Engineer"]})

    def last_email(self, hours_ago):
        jm.save_state({}, last_email=(datetime.now() - timedelta(hours=hours_ago)).isoformat(timespec="seconds"))

    def test_a_left_out_listing_by_one_entry_does_not_stop_another_emailing_it(self):
        self.mark_known(URL, self.ALL)
        self.pages[URL] = page(*jobs_named("Backend Engineer"))
        self.pages[self.ALL] = page()
        targets = [target(keyword_filters=["Intern"]), target(url=self.ALL)]  # the first only wants interns
        self.run_monitor(targets, email=WORKING_EMAIL, role_filter=REPO_ROLE_FILTER)
        self.assertEqual(self.left_out, {"Acme": ["Backend Engineer"]})
        self.pages[self.ALL] = page(*jobs_named("Backend Engineer"))
        _, reported = self.run_monitor(targets, email=WORKING_EMAIL, role_filter=REPO_ROLE_FILTER)
        self.assertEqual(reported, {"Acme": ["Backend Engineer"]})

    def test_a_posting_another_entry_emailed_is_not_listed_as_left_out_again(self):
        self.mark_known(URL, self.ALL)
        self.pages[URL] = page()
        self.pages[self.ALL] = page(*jobs_named("Backend Engineer"))
        targets = [target(keyword_filters=["Intern"]), target(url=self.ALL)]
        _, reported = self.run_monitor(targets, email=WORKING_EMAIL, role_filter=REPO_ROLE_FILTER)
        self.assertEqual(reported, {"Acme": ["Backend Engineer"]})
        self.pages[URL] = page(*jobs_named("Backend Engineer", "Data Engineer"))
        self.run_monitor(targets, email=WORKING_EMAIL, role_filter=REPO_ROLE_FILTER)
        # No new match, so no email yet: the left-out list waiting for the next one has only the new title.
        self.assertEqual([p["title"] for p in self.saved_state()[jm.PENDING_LEFT_OUT_KEY]], ["Data Engineer"])

    def test_a_left_out_posting_never_shown_is_listed_once_left_outs_are_shown(self):
        self.mark_known(URL, self.ALL)
        self.pages[URL] = page(*jobs_named("Senior Backend Engineer"))
        self.pages[self.ALL] = page()
        targets = [target(), target(url=self.ALL)]
        self.run_monitor(targets, email=WORKING_EMAIL, role_filter=REPO_ROLE_FILTER, show_filtered=False)
        self.pages[self.ALL] = page(*jobs_named("Senior Backend Engineer", "Backend Engineer"))
        self.run_monitor(targets, email=WORKING_EMAIL, role_filter=REPO_ROLE_FILTER, show_filtered=True)
        self.assertEqual(self.left_out, {"Acme": ["Senior Backend Engineer"]})

    def test_a_waiting_posting_is_emailed_after_its_entrys_link_is_edited(self):
        edited = URL + "?team=University"
        self.mark_known(URL)
        self.last_email(hours_ago=1)
        self.pages[URL] = self.pages[edited] = page(*jobs_named("Backend Engineer"))
        _, reported = self.run_monitor([target()], email=WORKING_EMAIL, email_every_hours=2)
        self.assertEqual(self.emails_attempted, 0)  # waiting for the 2-hour email
        self.last_email(hours_ago=3)
        _, reported = self.run_monitor([target(url=edited)], email=WORKING_EMAIL, email_every_hours=2)
        self.assertEqual(reported, {"Acme": ["Backend Engineer"]})

    def test_a_waiting_posting_is_emailed_after_the_entry_that_found_it_is_removed(self):
        self.mark_known(URL, self.ALL)
        self.last_email(hours_ago=1)
        self.pages[URL] = page(("backend?gh_jid=7712345", "Backend Engineer"))
        self.pages[self.ALL] = page(("7712345?gh_jid=7712345", "Backend Engineer"))
        self.run_monitor([target(), target(url=self.ALL)], email=WORKING_EMAIL, email_every_hours=2)
        self.assertEqual(self.emails_attempted, 0)  # found by the first entry, waiting; the second dropped it
        self.last_email(hours_ago=3)
        _, reported = self.run_monitor([target(url=self.ALL)], email=WORKING_EMAIL, email_every_hours=2)
        self.assertEqual(reported, {"Acme": ["Backend Engineer"]})

    def test_after_a_failed_email_the_posting_goes_out_once(self):
        self.mark_known(URL, self.ALL)
        self.pages[URL] = page(*jobs_named("Backend Engineer"))
        self.pages[self.ALL] = page()
        targets = [target(), target(url=self.ALL)]
        FakeSMTP.fail_with = smtplib.SMTPAuthenticationError(535, b"bad password")
        ok, _ = self.run_monitor(targets, email=WORKING_EMAIL)
        self.assertFalse(ok)
        FakeSMTP.fail_with = None
        self.pages[self.ALL] = page(*jobs_named("Backend Engineer"))
        _, reported = self.run_monitor(targets, email=WORKING_EMAIL)
        self.assertEqual(reported, {"Acme": ["Backend Engineer"]})
        _, reported = self.run_monitor(targets, email=WORKING_EMAIL)
        self.assertEqual((reported, len(self.emails_sent())), ({}, 1))

    def test_a_batched_posting_goes_out_once_when_due(self):
        self.mark_known(URL, self.ALL)
        self.last_email(hours_ago=1)
        self.pages[URL] = page(*jobs_named("Backend Engineer"))
        self.pages[self.ALL] = page()
        targets = [target(), target(url=self.ALL)]
        self.run_monitor(targets, email=WORKING_EMAIL, email_every_hours=2)
        self.pages[self.ALL] = page(*jobs_named("Backend Engineer"))
        self.last_email(hours_ago=3)
        _, reported = self.run_monitor(targets, email=WORKING_EMAIL, email_every_hours=2)
        self.assertEqual(reported, {"Acme": ["Backend Engineer"]})
        self.assertEqual(len(self.emails_sent()), 1)

    def test_names_in_any_script_are_compared_and_empty_ones_never_grouped(self):
        names = ["字节跳动", "抖音", "メルカリ Japan", "楽天 Japan", "字节跳动 ", "🙂", "🙂", "ACME, Inc.", "acme"]
        targets = [target(name=n, url=f"https://jobs.example/{i}") for i, n in enumerate(names)]
        self.assertEqual(jm._sibling_urls(targets), {
            "https://jobs.example/0": {"https://jobs.example/4"}, "https://jobs.example/4": {"https://jobs.example/0"},
            "https://jobs.example/7": {"https://jobs.example/8"}, "https://jobs.example/8": {"https://jobs.example/7"}})

    def test_job_ids_in_ashby_and_lever_links(self):
        u1, org = "0e7ecf76-1111-4a2b-9c3d-000000000001", "f7d8749a-72d6-407c-bf98-292ea630c73a"
        same = ["https://jobs.lever.co/acme/" + u1, "https://jobs.lever.co/acme/" + u1 + "/apply",
                "https://jobs.eu.lever.co/acme/" + u1.upper() + "/apply?lever-source=LinkedIn",
                "https://jobs.ashbyhq.com/acme/" + u1, "https://jobs.ashbyhq.com/" + org + "/" + u1 + "/application"]
        for link in same:
            self.assertEqual(jm.job_keys(link), {("uuid", u1)}, link)
            self.assertEqual(jm._posting_ids(link), {("uuid", u1)}, link)
        # Elsewhere a UUID still matches feed rows (job_keys) but isn't taken as one posting's id.
        self.assertEqual(jm.job_keys("https://careers.example/jobs/" + u1), {("uuid", u1)})
        self.assertEqual(jm._posting_ids("https://careers.example/jobs/" + u1), set())

    def test_the_shipped_config_groups_only_the_two_uber_entries(self):
        config = jm.load_config(REPO_DIR / "config_all.json")
        targets = [t for t in config["targets"] if not t.get("_section")]
        siblings = jm._sibling_urls(targets)
        uber = {t["url"] for t in targets if t.get("name") == "Uber"}
        self.assertEqual(len(uber), 2)
        self.assertEqual(siblings, {url: uber - {url} for url in uber})


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
        # (marked as reported, for any other entry for the same company: see SameCompanyTests)
        saved = self.saved_state()
        self.assertEqual({k: v for k, v in saved.items() if k not in (jm.HEALTH_KEY, jm.SOURCES_KEY)},
                         {URL: [{"title": "Data Analyst", "url": "https://acme.example/jobs/data-analyst",
                                 jm.REPORTED_MARK: jm.SHOWN_AS_MATCH}]})
        self.assertEqual(list(saved[jm.HEALTH_KEY]), [other])  # the broken one is noted (see HealthTests)
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
    "Product Engineer - Manufacturing Operations",
    "Hardware Tools Engineer", "RTL Tools & Methodology Engineer",
    "Leadership Development Program, Technology", "Architect - New College Grad 2026",
    # Tech roles whose titles carry a word of the "not tech" list (Nov 2026: the owner asked for sales and
    # other non-tech roles to go): the role itself is named, so they stay.
    "Software Engineer, Sales Platform", "Machine Learning Engineer, Sales Engineering", "Data Engineer - Sales Analytics",
    "Salesforce Developer", "Software Engineer - Partner Integrations", "Software Engineer, Recruiting Platform",
    "Forward Deployed Engineer, Professional Services", "Electronic Trading Support Engineer",
    "Cloud Infrastructure Support Engineer", "Production Support Engineer", "Field Engineer", "Junior Solution Architect",
    "Implementation Engineer", "Embedded Security Operator", "Data Scientist - Clinical", "Software Engineer, Real Estate Tech",
    "Research Scientist - Generative AI Graduate - Global Frontier Tech Recruitment Program - 2027 Start - PhD",
    "Machine Learning Materials Science Postdoctoral Researcher - Molecular Foundry", "Data Analyst - Clinical Analytics",
    "Business Intelligence Engineer, Prime Video Partner Experience", "Digital Automation Engineer - Cell Therapy",
    # Wrongly dropped by a first version of the "not tech" list (found by auditing every title it dropped):
    "Assistant Data Solutions Engineer", "Sales, Marketing, and Distribution Technology - Marketing and Distribution Technology",
    "Data Analyst - Shift D Wednesday-Saturday 5:00pm-3:30am",
    # Simplify's Quant category (read since Nov 2026): quant researchers, developers and traders stay.
    "Quantitative Trader", "Graduate Quantitative Researcher", "Quantitative Developer Grad", "ETF Quantitative Developer",
    "Trading System Engineer New Grad", "Quantitative Investment Analyst", "Quantitative Researcher - Trading Team",
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
    # Not tech even with an engineering word in the title (Nov 2026: "no sales engineer roles, and other not
    # tech roles"); most were in real emails.
    "Sales Engineer", "Enterprise Sales Engineer", "Solutions Engineer", "Pre-sales Solutions Engineer",
    "Solutions Architect - Manufacturing", "Partner Solutions Architect", "Applied AI Architect, Enterprise",
    "Customer Engineer, Google Cloud", "Field Application Engineer", "AI Support Engineer - San Francisco",
    "Technical Support Engineer II", "Robotics Aftermarket - Inside Sales Representative", "Private Cloud AI Sales Specialist",
    "Territory Development Executive - Software/SaaS", "Account Executive, Strategic Platform Partnerships",
    "Manheim Mobile Vehicle Condition Inspector, West Palm Beach South, FL", "Mobile Diesel Tech II -Variable Shift",
    "Certificate Curriculum Developer - Kenneth C. Griffin School of Computer Science - LTI",
    "Registered Nurse New Graduate Progressive Care", "Quantitative Tutoring & Statistical Software Support (Student)",
    "Campus Recruiter, Machine Learning and Quantitative Research", "Recruiter, AI/ML Research",
    "Data Center Technician, Hardware Operations", "Critical Facility Engineer", "Research Scientist - Antibody Discovery",
    "Clinical Research Coordinator I", "AppleCare Strategy & Solutions Analyst", "Car Wash Attendant", "Claims Adjuster",
    "Forklift Operator", "Investment Banking Analyst", "AI Tutor - Software Engineering",
    "Legal Research Attorney - AI Training", "Economists - Economic Analysis for AI Training",
    "Res Life Embedded Clinician", "Project Dorado | Robotics Teleoperator",
    # ... but not its trading and investment analysts, or its misfiled jobs.
    "Trading Analyst", "Trading Associate", "Trading Graduate Program - Trading", "Investment Analyst - Parametric",
    "Food Services Worker - Student Worker",
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

    def test_not_tech_postings_are_counted_but_not_listed_in_the_email(self):
        jobs = [{"title": t, "url": f"https://acme.example/{i}", "company": "Acme",
                 "reason": jm.role_exclusion_reason(t, self.patterns)}
                for i, t in enumerate(["Sales Engineer", "Solutions Engineer", "Senior Software Engineer"])]
        summary = jm.summarize_left_out(jobs, self.patterns)
        self.assertEqual(summary["by_reason"], {"not tech": 2, "senior": 1})
        self.assertEqual([j["title"] for j in summary["listed"]], ["Senior Software Engineer"])

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
        # Titles that still look like engineering roles are listed; the others are counted.
        self.assertEqual(self.left_out, {"Acme": ["Senior Software Engineer"]})
        [(_, msg)] = self.emails_sent()
        body = msg.get_payload()[0].get_payload(decode=True).decode()
        self.assertIn("Left out by your filters: 2 (senior 1, not tech 1)", body)
        self.assertIn("Senior Software Engineer  [senior (Senior)]", body)
        self.assertNotIn("Account Executive", body)
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
        self.assertEqual([j["title"] for j in self.saved_state()[jm.ALERT_POOL_KEY]], ["Backend Engineer"])
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
        self.assertEqual(set(self.saved_state()) - {jm.SOURCES_KEY}, {URL})

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
        self.assertNotIn(CITADEL_SITEMAP, self.saved_state())

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


class EmailBatchTests(MonitorTestCase):
    """Runs record postings every time; emails go out at most every email_every_hours."""

    def last_email(self, hours_ago):
        jm.save_state({}, last_email=(datetime.now() - timedelta(hours=hours_ago)).isoformat(timespec="seconds"))

    def run_batched(self, **extra):
        extra.setdefault("email_every_hours", 2)
        return self.run_monitor([target()], email=WORKING_EMAIL, role_filter=REPO_ROLE_FILTER, **extra)

    def test_matches_wait_until_two_hours_after_the_last_email(self):
        self.mark_known(URL)
        self.last_email(hours_ago=0.5)
        self.pages[URL] = page(*jobs_named("Backend Engineer"))
        ok, reported = self.run_batched()
        self.assertTrue(ok)
        self.assertEqual(self.emails_attempted, 0)
        state = self.saved_state()
        self.assertEqual(len(state[URL]), 1)  # recorded as seen at once
        self.assertEqual([j["title"] for j in state[jm.ALERT_POOL_KEY]], ["Backend Engineer"])

        # Another posting 30 minutes later: still waiting, now two.
        self.last_email(hours_ago=1.0)
        self.pages[URL] = page(*jobs_named("Data Engineer"))  # the first one even left the page
        self.run_batched()
        self.assertEqual(self.emails_attempted, 0)

        # Two hours after the last email (give or take a few minutes): both in one email.
        self.last_email(hours_ago=1.9)
        ok, reported = self.run_batched()
        self.assertTrue(ok)
        self.assertEqual(reported, {"Acme": ["Backend Engineer", "Data Engineer"]})
        self.assertEqual(len(self.emails_sent()), 1)
        self.assertNotIn(jm.ALERT_POOL_KEY, self.saved_state())

        # The timer restarted.
        self.pages[URL] = page(*jobs_named("Data Engineer", "ML Engineer"))
        self.run_batched()
        self.assertEqual(len(self.emails_sent()), 1)
        self.assertEqual([j["title"] for j in self.saved_state()[jm.ALERT_POOL_KEY]], ["ML Engineer"])

    def test_first_posting_after_a_quiet_spell_goes_out_at_once(self):
        self.mark_known(URL)
        self.last_email(hours_ago=5)
        self.pages[URL] = page(*jobs_named("Backend Engineer"))
        _, reported = self.run_batched()
        self.assertEqual(reported, {"Acme": ["Backend Engineer"]})

    def test_zero_means_every_run_and_bad_values_fall_back_to_it(self):
        self.mark_known(URL)
        self.last_email(hours_ago=0.1)
        self.pages[URL] = page(*jobs_named("Backend Engineer"))
        _, reported = self.run_batched(email_every_hours=0)
        self.assertEqual(reported, {"Acme": ["Backend Engineer"]})
        with self.assertLogs(jm.log, "WARNING"):
            self.assertEqual(jm._email_every_hours({"email_every_hours": "often"}), 0.0)

    def test_failed_email_keeps_the_batch_for_the_next_run(self):
        self.mark_known(URL)
        self.pages[URL] = page(*jobs_named("Backend Engineer"))
        FakeSMTP.fail_with = smtplib.SMTPAuthenticationError(535, b"bad password")
        ok, _ = self.run_batched()
        self.assertFalse(ok)
        FakeSMTP.fail_with = None
        ok, reported = self.run_batched()  # an email was never recorded as sent, so it's due
        self.assertTrue(ok)
        self.assertEqual(reported, {"Acme": ["Backend Engineer"]})

    def test_another_config_sharing_state_leaves_this_configs_alerts_alone(self):
        other = "https://other.example/careers"
        self.mark_known(URL, other)
        self.last_email(hours_ago=0.5)
        self.pages[URL] = page(*jobs_named("Backend Engineer"))
        self.run_batched()  # held for this config's next email
        self.pages[other] = page()
        self.run_monitor([target(name="Other", url=other)], email={"enabled": False})  # e.g. the starter config
        self.assertEqual([j["title"] for j in self.saved_state()[jm.ALERT_POOL_KEY]], ["Backend Engineer"])
        self.last_email(hours_ago=3)
        _, reported = self.run_batched()
        self.assertEqual(reported, {"Acme": ["Backend Engineer"]})

    @unittest.skipUnless(sys.platform.startswith("linux"), "only Linux allows folder names that aren't UTF-8")
    def test_a_config_folder_whose_name_is_not_utf8_still_saves_waiting_postings(self):
        folder = os.fsdecode(b"caf\xe9")
        (self.tmp / folder).mkdir()
        self.mark_known(URL)
        self.last_email(hours_ago=0.5)
        self.pages[URL] = page(*jobs_named("Backend Engineer"))
        ok, _ = self.run_batched(config_file=f"{folder}/config.json")
        self.assertTrue(ok)
        pool = self.saved_state()[jm.ALERT_POOL_KEY]
        self.assertEqual([p["title"] for p in pool], ["Backend Engineer"])
        self.assertRegex(pool[0]["source_config"], r"^[0-9a-f]{16}$")

    def test_another_config_with_an_entry_of_the_same_name_leaves_this_configs_alerts_alone(self):
        other = "https://other.example/careers"
        self.mark_known(URL, other)
        self.last_email(hours_ago=0.5)
        self.pages[URL] = page(*jobs_named("Backend Engineer"))
        self.run_batched()  # held for this config's next email
        self.pages[other] = page()
        _, reported = self.run_monitor([target(url=other)], email={"enabled": False}, config_file="mine.json")
        self.assertEqual(reported, {})
        self.assertEqual([j["title"] for j in self.saved_state()[jm.ALERT_POOL_KEY]], ["Backend Engineer"])
        self.last_email(hours_ago=3)
        _, reported = self.run_batched()
        self.assertEqual(reported, {"Acme": ["Backend Engineer"]})

    def test_a_last_email_time_in_the_future_doesnt_hold_emails_back(self):
        self.mark_known(URL)
        self.last_email(hours_ago=-1)  # e.g. the clock went back an hour
        self.pages[URL] = page(*jobs_named("Backend Engineer"))
        _, reported = self.run_batched()
        self.assertEqual(reported, {"Acme": ["Backend Engineer"]})

    def test_dry_run_prints_instead_of_emailing(self):
        self.mark_known(URL)
        self.pages[URL] = page(*jobs_named("Backend Engineer"))
        os.environ["JOB_MONITOR_NO_EMAIL"] = "1"
        with self.assertLogs(jm.log, "INFO") as logs:
            ok, reported = self.run_batched()
        self.assertTrue(ok)
        self.assertEqual(reported, {"Acme": ["Backend Engineer"]})
        self.assertEqual(FakeSMTP.instances, [])
        self.assertIn("Dry run", "\n".join(logs.output))


class LeftOutSummaryTests(MonitorTestCase):
    def setUp(self):
        super().setUp()
        self.patterns = jm.compile_role_filter(REPO_ROLE_FILTER)

    def left(self, title, reason):
        return {"title": title, "url": f"https://acme.example/{title.replace(' ', '-')}", "reason": reason, "group": "Acme"}

    def test_engineering_looking_titles_are_listed_first_and_the_rest_counted(self):
        jobs = [
            self.left("Account Executive", "not engineering (Account Executive)"),
            self.left("Senior Software Engineer", "senior (Senior)"),
            self.left("Data Analyst", "not engineering (Analyst)"),
            self.left("Software Engineer Intern", "internship (Intern)"),
            self.left("Frontend Engineer", "frontend (Frontend)"),
        ]
        summary = jm.summarize_left_out(jobs, self.patterns)
        self.assertEqual(summary["total"], 5)
        self.assertEqual(summary["by_reason"], {"not engineering": 2, "senior": 1, "internship": 1, "frontend": 1})
        # Kind-of-role exclusions before level ones, engineering words first; non-technical ones only counted.
        self.assertEqual([j["title"] for j in summary["listed"]],
                         ["Frontend Engineer", "Data Analyst", "Senior Software Engineer", "Software Engineer Intern"])

    def test_a_title_repeated_at_one_company_is_listed_once(self):
        jobs = [self.left("Data Center Technician", "not engineering (Technician)") for _ in range(3)]
        jobs = [{**j, "url": f"{j['url']}/{i}"} for i, j in enumerate(jobs)]
        summary = jm.summarize_left_out(jobs, self.patterns)
        self.assertEqual((summary["total"], len(summary["listed"])), (3, 1))

    def test_postings_waiting_before_an_upgrade_reach_the_page(self):
        # A pool written by the previous version (no left_out.md log yet).
        self.mark_known(URL)
        jm.save_state({}, pending_add=[{"title": "Staff Engineer", "url": "https://acme.example/staff",
                                       "company": "Acme", "reason": "senior (Staff)"}])
        self.pages[URL] = page()
        self.run_monitor([target()], email=WORKING_EMAIL, role_filter=REPO_ROLE_FILTER)  # digest due: never emailed
        self.assertEqual(len(self.emails_sent()), 1)
        self.assertIn("[Staff Engineer](https://acme.example/staff)", jm.left_out_page_path().read_text())

    def test_line_breaks_in_titles_dont_break_the_page(self):
        page_md = jm.format_left_out_page([{"title": "icon Associate : The group\n\nseeks people", "url": "https://x.example/1",
                                            "reason": "senior (Associate)", "seen": "2026-10-08T10:00"}])
        self.assertIn("- [icon Associate : The group seeks people](https://x.example/1)", page_md)

    def test_at_most_25_are_listed_and_the_email_points_to_the_full_list(self):
        self.mark_known(URL)
        self.pages[URL] = page(*jobs_named("Backend Engineer", *[f"Senior Engineer {i}" for i in range(40)]))
        with mock.patch.dict(os.environ, GITHUB_REPOSITORY="me/jobs", JOB_MONITOR_STATE_BRANCH="monitor-state"):
            self.run_monitor([target()], email=WORKING_EMAIL, role_filter=REPO_ROLE_FILTER)
        self.assertEqual(sum(len(v) for v in self.left_out.values()), 25)
        [(_, msg)] = self.emails_sent()
        plain = msg.get_payload()[0].get_payload(decode=True).decode()
        html = msg.get_payload()[1].get_payload(decode=True).decode()
        self.assertIn("Left out by your filters: 40 (senior 40)", plain)
        self.assertIn("https://github.com/me/jobs/blob/monitor-state/left_out.md", plain)
        self.assertIn('href="https://github.com/me/jobs/blob/monitor-state/left_out.md"', html)
        self.assertIn("+40 left out by filters", msg["Subject"])

    def test_left_out_page_keeps_seven_days(self):
        self.mark_known(URL)
        old = {"title": "Old Role", "url": "https://acme.example/old", "reason": "senior (Senior)",
               "seen": (datetime.now() - timedelta(days=8)).isoformat(timespec="minutes")}
        jm.save_state({}, last_email=datetime.now().isoformat(timespec="seconds"))
        state = self.saved_state()
        state[jm.LEFT_OUT_LOG_KEY] = [old]
        jm.STATE_PATH.write_text(json.dumps(state))
        self.pages[URL] = page(*jobs_named("Staff Engineer [Platform]", "Account Executive"))
        self.run_monitor([target()], email=WORKING_EMAIL, role_filter=REPO_ROLE_FILTER, email_every_hours=2)
        log_titles = [e["title"] for e in self.saved_state()[jm.LEFT_OUT_LOG_KEY]]
        self.assertEqual(log_titles, ["Staff Engineer [Platform]", "Account Executive"])  # the 8-day-old one is gone
        page_md = jm.left_out_page_path().read_text()
        self.assertIn("[Staff Engineer \\[Platform\\]](https://acme.example/jobs/staff-engineer-[platform]) · Acme", page_md)
        self.assertIn("Account Executive", page_md)  # every left-out posting is on the page
        self.assertNotIn("Old Role", page_md)


SIMPLIFY_URL = "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json"
SPEEDY_URL = "https://raw.githubusercontent.com/speedyapply/2027-SWE-College-Jobs/main/NEW_GRAD_USA.md"


def simplify_row(company, title, days_ago=1, **extra):
    row = {"source": "Simplify", "category": "Software", "company_name": company, "title": title, "active": True,
           "is_visible": True, "date_posted": int(time.time() - days_ago * 86400), "id": f"{company}-{title}",
           "url": f"https://job-boards.greenhouse.io/{company.lower()}/jobs/{abs(hash((company, title))) % 10**8}",
           "locations": ["San Francisco, CA"], "company_url": "", "sponsorship": "Other", "degrees": []}
    row.update(extra)
    return row


# Rows as speedyapply writes them: FAANG+ tables have a Salary column, "Other" tables don't.
SPEEDY_MD = """## 2027 USA SWE New Graduate Positions
<!-- TABLE_FAANG_START -->
| Company | Position | Location | Salary | Posting | Age |
|---|---|---|---|---|---|
| <a href="https://www.roblox.com"><strong>Roblox</strong></a> | Software Engineer - Test Frameworks &amp; Tooling | San Mateo, CA | $150k/yr | <a href="https://careers.roblox.com/jobs/8229705?gh_jid=8229705"><img src="https://i.imgur.com/JpkfjIq.png" alt="Apply" width="70"/></a> | 1d |
<!-- TABLE_FAANG_END -->
<!-- TABLE_START -->
| Company | Position | Location | Posting | Age |
|---|---|---|---|---|
| <a href="https://thesirius.ai/"><strong>Sirius</strong></a> | Backend Engineer \\| Platform | San Francisco, CA | <a href="https://jobs.ashbyhq.com/thesirius/662f4814"><img src="https://i.imgur.com/JpkfjIq.png" alt="Apply" width="70"/></a> | 2d |
| <a href="https://old.example/"><strong>Old Co</strong></a> | Backend Engineer | Austin, TX | <a href="https://jobs.lever.co/oldco/1"><img src="https://i.imgur.com/JpkfjIq.png" alt="Apply" width="70"/></a> | 40d |
<!-- TABLE_END -->
"""


class FeedTests(MonitorTestCase):
    def simplify_target(self, **extra):
        extra.setdefault("max_age_days", 7)
        return target(name="SimplifyJobs New Grad", url=SIMPLIFY_URL, mode="feed", format="simplify",
                      categories=["Software", "AI/ML/Data"], **extra)

    def test_simplify_rows_are_filtered_by_activity_category_and_age(self):
        self.pages[SIMPLIFY_URL] = json.dumps([
            simplify_row("Clay", "Early Career Software Engineer", locations=["New York, NY", "Remote in USA"]),
            simplify_row("Mach9", "Machine Learning Engineer", category="AI/ML/Data"),
            simplify_row("Volt", "Hardware Engineer", category="Hardware"),
            simplify_row("Gone", "Software Engineer", active=False),
            simplify_row("Hidden", "Software Engineer", is_visible=False),
            simplify_row("Stale", "Software Engineer", days_ago=30),
        ])
        jobs = jm.fetch_feed_jobs(self.simplify_target())
        self.assertEqual([(j["company"], j["title"], j.get("listed", True)) for j in jobs],
                         [("Clay", "Early Career Software Engineer", True), ("Mach9", "Machine Learning Engineer", True),
                          ("Gone", "Software Engineer", False)])  # closed rows are remembered, not emailed
        self.assertEqual(jobs[0]["location"], "New York, NY; Remote in USA")

    def test_speedyapply_tables_with_and_without_salary(self):
        self.pages[SPEEDY_URL] = SPEEDY_MD
        jobs = jm.fetch_feed_jobs(target(url=SPEEDY_URL, mode="feed", format="speedyapply", max_age_days=7))
        self.assertEqual({j.pop("first_seen") for j in jobs}, {datetime.now().date().isoformat()})
        self.assertEqual(jobs, [
            {"title": "Software Engineer - Test Frameworks & Tooling", "url": "https://careers.roblox.com/jobs/8229705?gh_jid=8229705",
             "company": "Roblox", "location": "San Mateo, CA"},
            {"title": "Backend Engineer | Platform", "url": "https://jobs.ashbyhq.com/thesirius/662f4814",
             "company": "Sirius", "location": "San Francisco, CA"},
        ])

    def test_unknown_format_or_unreadable_feed_skips_the_target(self):
        with self.assertLogs(jm.log, "ERROR"):
            self.assertIsNone(jm.fetch_feed_jobs(target(url=SPEEDY_URL, mode="feed", format="rss")))
        self.pages[SIMPLIFY_URL] = "<html>rate limited</html>"
        with self.assertLogs(jm.log, "WARNING"):
            self.assertIsNone(jm.fetch_feed_jobs(self.simplify_target()))

    def test_feed_postings_are_emailed_under_their_company(self):
        self.mark_known(SIMPLIFY_URL)
        self.pages[SIMPLIFY_URL] = json.dumps([simplify_row("Clay", "Early Career Software Engineer"),
                                               simplify_row("Clay", "Senior Software Engineer")])
        _, reported = self.run_monitor([self.simplify_target()], email=WORKING_EMAIL, role_filter=REPO_ROLE_FILTER)
        self.assertEqual(reported, {"Clay (via SimplifyJobs New Grad)": ["Early Career Software Engineer"]})
        self.assertEqual(self.left_out, {"Clay (via SimplifyJobs New Grad)": ["Senior Software Engineer"]})

    def test_first_check_of_a_feed_is_silent(self):
        self.pages[SIMPLIFY_URL] = json.dumps([simplify_row("Clay", "Early Career Software Engineer")])
        _, reported = self.run_monitor([self.simplify_target()], email=WORKING_EMAIL)
        self.assertEqual((reported, self.emails_attempted), ({}, 0))
        self.assertEqual(len(self.saved_state()[SIMPLIFY_URL]), 1)

    def test_a_tracked_companys_posting_is_skipped_only_if_its_own_entry_found_it(self):
        # Company entries are often filtered (a team, a level, the newest page), so a feed can
        # list a posting the company's own entry never sees: that one is still emailed.
        roblox = "https://boards-api.greenhouse.io/v1/boards/roblox/jobs"
        self.mark_known(SIMPLIFY_URL, roblox)
        self.pages[roblox] = json.dumps({"jobs": [{"title": "Software Engineer", "location": {"name": "San Mateo, CA"},
                                                   "absolute_url": "https://careers.roblox.com/jobs/8229705?gh_jid=8229705"}]})
        self.pages[SIMPLIFY_URL] = json.dumps([
            simplify_row("Roblox", "Software Engineer", url="https://job-boards.greenhouse.io/roblox/jobs/8229705"),
            simplify_row("Roblox", "ML Engineer, Safety", url="https://job-boards.greenhouse.io/roblox/jobs/8230001"),
        ])
        targets = [target(name="Roblox", url=roblox, mode="api"), self.simplify_target()]
        _, reported = self.run_monitor(targets, email=WORKING_EMAIL)
        self.assertEqual(reported, {"Roblox": ["Software Engineer"],
                                    "Roblox (via SimplifyJobs New Grad)": ["ML Engineer, Safety"]})

    def test_a_company_wide_id_in_ashby_links_does_not_make_new_postings_repeats(self):
        # Simplify links Shield AI's postings as jobs.ashbyhq.com/<company id>/<posting id>/application.
        org = "f7d8749a-72d6-407c-bf98-292ea630c73a"
        job1, job2 = "0e7ecf76-1111-4a2b-9c3d-000000000001", "0e7ecf76-2222-4a2b-9c3d-000000000002"
        first = simplify_row("Shield AI", "Engineer 1 New Grad - Software", url=f"https://jobs.ashbyhq.com/{org}/{job1}/application")
        second = simplify_row("Shield AI", "Engineer 2 New Grad - Perception", url=f"https://jobs.ashbyhq.com/{org}/{job2}/application")
        self.mark_known(SIMPLIFY_URL)
        self.pages[SIMPLIFY_URL] = json.dumps([first])
        _, reported = self.run_monitor([self.simplify_target()], email=WORKING_EMAIL)
        self.assertEqual(reported, {"Shield AI (via SimplifyJobs New Grad)": ["Engineer 1 New Grad - Software"]})
        self.pages[SIMPLIFY_URL] = json.dumps([first, second])
        _, reported = self.run_monitor([self.simplify_target()], email=WORKING_EMAIL)
        self.assertEqual(reported, {"Shield AI (via SimplifyJobs New Grad)": ["Engineer 2 New Grad - Perception"]})
        # The same posting linked under the board's name is still the same posting.
        again = simplify_row("Shield AI", "Engineer 2 New Grad - Perception", url=f"https://jobs.ashbyhq.com/shieldai/{job2}")
        self.pages[SIMPLIFY_URL] = json.dumps([first, second, again])
        _, reported = self.run_monitor([self.simplify_target()], email=WORKING_EMAIL)
        self.assertEqual(reported, {})

    def test_the_same_job_under_another_link_is_not_new(self):
        boeing = "https://boeing.wd1.myworkdayjobs.com/en-US/{site}/job/Seattle/Software-Application-Tester_JR2026524037{suffix}"
        self.mark_known(SIMPLIFY_URL)
        self.pages[SIMPLIFY_URL] = json.dumps([
            simplify_row("Boeing", "Software Application Tester", url=boeing.format(site="external_careers", suffix="-1")),
            simplify_row("Boeing", "Software Application Tester", url=boeing.format(site="external_subsidiary", suffix="")),
        ])
        _, reported = self.run_monitor([self.simplify_target()], email=WORKING_EMAIL)
        self.assertEqual(reported, {"Boeing (via SimplifyJobs New Grad)": ["Software Application Tester"]})  # once
        # Later the feed links it under yet another site path: still the same job.
        self.pages[SIMPLIFY_URL] = json.dumps([
            simplify_row("Boeing", "Software Application Tester", url=boeing.format(site="boeing_jobs", suffix=""))])
        _, reported = self.run_monitor([self.simplify_target()], email=WORKING_EMAIL)
        self.assertEqual(reported, {})

    def test_title_matches_across_feeds_only_look_back_two_weeks(self):
        old = (datetime.now() - timedelta(days=30)).date().isoformat()
        jm.save_state({SPEEDY_URL: [{"title": "Software Engineer - New Grad", "url": "https://ixl.example/old",
                                     "company": "IXL Learning", "first_seen": old}], SIMPLIFY_URL: []})
        self.pages[SPEEDY_URL] = ""
        self.pages[SIMPLIFY_URL] = json.dumps([simplify_row("IXL Learning", "Software Engineer - New Grad",
                                                            url="https://job-boards.greenhouse.io/ixl/jobs/8862043002")])
        targets = [self.simplify_target(), target(name="speedyapply New Grad SWE", url=SPEEDY_URL, mode="feed", format="speedyapply")]
        _, reported = self.run_monitor(targets, email=WORKING_EMAIL)
        self.assertEqual(reported, {"IXL Learning (via SimplifyJobs New Grad)": ["Software Engineer - New Grad"]})

    def test_a_new_link_is_new_whatever_its_date_and_closed_rows_stay_quiet(self):
        wide = self.simplify_target(max_age_days=90)
        self.pages[SIMPLIFY_URL] = json.dumps([
            simplify_row("Old Co", "Software Engineer", days_ago=40),                 # listed at the first check
            simplify_row("Closed Co", "Software Engineer", days_ago=20, active=False),
        ])
        self.run_monitor([wide], email=WORKING_EMAIL)  # first check: recorded silently
        self.pages[SIMPLIFY_URL] = json.dumps([
            simplify_row("Old Co", "Software Engineer", days_ago=40),
            simplify_row("Closed Co", "Software Engineer", days_ago=20),              # re-activated: not new
            simplify_row("Late Co", "Software Engineer", days_ago=35),                # added late: new
            simplify_row("Shut Co", "Software Engineer", days_ago=1, active=False),   # new but closed: quiet
            simplify_row("Ancient Co", "Software Engineer", days_ago=200),            # beyond max_age_days
        ])
        _, reported = self.run_monitor([wide], email=WORKING_EMAIL)
        self.assertEqual(reported, {"Late Co (via SimplifyJobs New Grad)": ["Software Engineer"]})

    def test_a_posting_in_two_feeds_or_already_known_is_emailed_once(self):
        self.mark_known(SIMPLIFY_URL, SPEEDY_URL, URL)
        roblox_link = "https://careers.roblox.com/jobs/8229705?gh_jid=8229705"
        self.pages[URL] = page()
        jm.save_state({URL: [{"title": "Software Engineer - Test Frameworks & Tooling", "url": roblox_link}]})
        self.pages[SIMPLIFY_URL] = json.dumps([
            simplify_row("Sirius", "Backend Engineer | Platform", url="https://jobs.ashbyhq.com/thesirius/662f4814?utm_source=Simplify"),
            simplify_row("Roblox Corp", "SWE", url=roblox_link),
        ])
        self.pages[SPEEDY_URL] = SPEEDY_MD
        targets = [target(name="Big Co"), self.simplify_target(),
                   target(name="speedyapply New Grad SWE", url=SPEEDY_URL, mode="feed", format="speedyapply")]
        _, reported = self.run_monitor(targets, email=WORKING_EMAIL)
        self.assertEqual(reported, {"Sirius (via SimplifyJobs New Grad)": ["Backend Engineer | Platform"]})

        # Next run: the other feed listing the same job under another link isn't new either.
        self.pages[SPEEDY_URL] = SPEEDY_MD.replace("662f4814", "662f4814-other-link")
        _, reported = self.run_monitor(targets, email=WORKING_EMAIL)
        self.assertEqual(reported, {})

    def test_the_shipped_feeds(self):
        feeds = [t for t in jm.load_config(REPO_DIR / "config_all.json")["targets"] if t.get("mode") == "feed"]
        self.assertEqual({t["format"] for t in feeds}, {"simplify", "speedyapply"})
        self.assertTrue(all(t["url"].startswith("https://raw.githubusercontent.com/") for t in feeds))


GH_ACME = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
ASHBY_ZETA = "https://api.ashbyhq.com/posting-api/job-board/zeta"
INTERNSHIPS_URL = "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json"


def gh_link(jid, host="job-boards.greenhouse.io"):
    return f"https://{host}/acme/jobs/{4000000 + jid}"  # Greenhouse job ids are long numbers


def gh_board(*jobs):
    """A Greenhouse board API response; each job is (id, title[, location])."""
    return json.dumps({"jobs": [{"id": 4000000 + jid, "title": title, "location": {"name": rest[0] if rest else "New York, NY"},
                                 "absolute_url": gh_link(jid)} for jid, title, *rest in jobs]})


def gh_dated(*jobs):
    """A Greenhouse board API response with posting dates; each job is (id, title, posted how
    many days ago or None[, location])."""
    now = datetime.now(timezone.utc)
    return json.dumps({"jobs": [{"id": 4000000 + jid, "title": title, "location": {"name": rest[0] if rest else "New York, NY"},
                                 "absolute_url": gh_link(jid),
                                 **({"first_published": (now - timedelta(days=days)).isoformat()} if days is not None else {})}
                                for jid, title, days, *rest in jobs]})


AMEX_ORACLE_HOST = "egug.fa.us2.oraclecloud.com"
AMEX_ORACLE_API = jm.oracle.api_url(AMEX_ORACLE_HOST, "CX_1")


def oracle_link(number, site="CX_1", language="en", host=AMEX_ORACLE_HOST):
    """A posting's link as the lists give it."""
    return f"https://{host}/hcmUI/CandidateExperience/{language}/sites/{site}/job/{number}?utm_source=Simplify&ref=Simplify"


def oracle_api(*postings):
    """An Oracle site's API answer: (number, title, posted, country[, other countries])."""
    return json.dumps({"items": [{"TotalJobsCount": len(postings), "requisitionList": [
        {"Id": str(number), "Title": title, "PostedDate": posted, "PrimaryLocation": f"Somewhere, {country}",
         "PrimaryLocationCountry": country,
         "secondaryLocations": [{"Name": f"Elsewhere, {c}", "CountryCode": c} for c in (rest[0] if rest else [])]}
        for number, title, posted, country, *rest in postings]}]})


class OracleBoardTests(MonitorTestCase):
    """Companies' Oracle Recruiting Cloud sites (boards/oracle.py), found through the lists like other boards."""

    def run_lists(self, *rows):
        self.pages[SIMPLIFY_URL] = json.dumps(list(rows))
        feed = target(name="SimplifyJobs New Grad", url=SIMPLIFY_URL, mode="feed", format="simplify",
                      categories=["Software"], max_age_days=7)
        boards = {"name": "Company job boards", "mode": "boards", "every_hours": 1, "catch_up_days": 3,
                  "platforms": ["greenhouse", "oracle"], "tech_titles": ["software", "ml", "new grad"]}
        return self.run_monitor([feed, boards], email=WORKING_EMAIL, role_filter=REPO_ROLE_FILTER, us_only=True)[1]

    def test_oracle_links_are_recognised_by_site(self):
        self.assertEqual(jm.board_api_url(oracle_link(25018475)), ("oracle", AMEX_ORACLE_API))
        self.assertEqual(jm.board_api_url(oracle_link(1, language="en-US")), ("oracle", AMEX_ORACLE_API))
        self.assertEqual(jm._board_platform(AMEX_ORACLE_API), "oracle")
        self.assertEqual(jm._board_page(AMEX_ORACLE_API), f"https://{AMEX_ORACLE_HOST}/hcmUI/CandidateExperience/en/sites/CX_1/jobs")
        # One site whatever the link; two sites on one host are two boards.
        self.assertEqual(len({jm._registry_key(x) for x in (AMEX_ORACLE_API, jm.board_api_url(oracle_link(9))[1])}), 1)
        self.assertNotEqual(jm._registry_key(AMEX_ORACLE_API), jm._registry_key(jm.oracle.api_url(AMEX_ORACLE_HOST, "CX_45001")))
        known = {}
        jm.grow_board_registry(known, [(oracle_link(1), "American Express"), (oracle_link(2, site="CX_45001"), "American Express"),
                                       (oracle_link(3, language="en-US"), "American Express")], "a list", "2026-11-20")
        self.assertEqual(sorted(i["url"] for i in known.values()), sorted([AMEX_ORACLE_API, jm.oracle.api_url(AMEX_ORACLE_HOST, "CX_45001")]))
        for bad in ("https://example.com/hcmUI/CandidateExperience/en/sites/CX_1/job/1", f"https://{AMEX_ORACLE_HOST}/somewhere/else"):
            self.assertIsNone(jm.board_api_url(bad))
        # The same posting under any language, site or query is one job.
        self.assertEqual(jm.job_keys(oracle_link(25018475, language="en-US")),
                         jm.job_keys(f"https://{AMEX_ORACLE_HOST}/hcmUI/CandidateExperience/en/sites/CX_1/job/25018475"))
        self.assertIn(("oracle", AMEX_ORACLE_HOST, "25018475"), jm.job_keys(oracle_link(25018475, site="CX_45001")))

    def test_the_reader_gives_dates_and_every_country(self):
        today = datetime.now(timezone.utc).date().isoformat()
        jobs = jm.fetch_oracle_hcm_jobs(AMEX_ORACLE_API, get_json=lambda u, params=None: json.loads(oracle_api(
            (11, "Software Engineer", today, "IN", ["US"]), (12, "Data Engineer", today, "US"))))
        self.assertEqual([(j["countries"], j["posted"][:10]) for j in jobs], [(["IN", "US"], today), (["US"], today)])
        self.assertEqual(jobs[0]["location"], "Somewhere, IN; Elsewhere, US")
        self.assertFalse(jm.is_outside_us(jobs[0]))  # (one of its places is in the US)

    def test_an_oracle_site_the_lists_link_is_read_and_its_new_us_roles_emailed(self):
        today = datetime.now(timezone.utc).date().isoformat()
        old = (datetime.now(timezone.utc) - timedelta(days=30)).date().isoformat()
        row = simplify_row("American Express", "Software Engineer I", url=oracle_link(1001))
        self.pages[AMEX_ORACLE_API] = oracle_api((1001, "Software Engineer I", today, "US"), (1002, "Backend Software Engineer", today, "US"),
                                            (1000, "Software Engineer", old, "US"))
        self.assertEqual(self.run_lists(row), {})          # (the list's first check records its row)
        # The list links Amex's Oracle site: it joins the registry, and its first check emails only what went up
        # in the last 3 days and wasn't found before (the list's row is the same job: not again; the old one: not new).
        self.assertEqual(self.run_lists(row), {"American Express": ["Backend Software Engineer"]})
        self.assertEqual(len(self.saved_state()[AMEX_ORACLE_API]), 3)
        known = self.saved_state()[jm.BOARDS_KEY]["known"]
        self.assertEqual([(i["name"], i["url"]) for i in known.values()], [("American Express", AMEX_ORACLE_API)])
        self.pages[AMEX_ORACLE_API] = oracle_api((1003, "Software Engineer, New Grad", today, "US"),
                                            (1004, "Software Engineer II", today, "IN"),
                                            (1005, "ML Engineer", today, "CA", ["US"]),
                                            (1001, "Software Engineer I", today, "US"), (1002, "Backend Software Engineer", today, "US"))
        meta = self.saved_state()[jm.BOARDS_KEY]
        meta["checked_at"] = (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds")
        jm.save_state({}, boards=meta)
        self.assertEqual(self.run_lists(row), {"American Express": ["Software Engineer, New Grad", "ML Engineer"]})
        self.assertIn(f"| American Express | [Oracle](https://{AMEX_ORACLE_HOST}/hcmUI/CandidateExperience/en/sites/CX_1/jobs) |",
                      jm.boards_page_path().read_text())
        self.assertIn("1 Oracle.", jm.boards_page_path().read_text())

    def test_the_site_your_entry_reads_is_not_read_again(self):
        mine = "https://jpmc.fa.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true" \
               "&finder=findReqs;siteNumber=CX_1001,limit=100,locationId=300000000289738,sortBy=POSTING_DATES_DESC"
        meta = {"known": {}, "list_etags": {}}
        jm.grow_board_registry(meta["known"], [(oracle_link(5, site="CX_1001", host="jpmc.fa.oraclecloud.com"), "JPMorgan Chase"),
                                               (oracle_link(6, site="CX_1002", host="jpmc.fa.oraclecloud.com"), "JPMorgan Chase")],
                               "a list", "2026-11-20")
        boards = jm.plan_boards({"platforms": ["oracle"]}, [target(name="JPMorgan Chase", url=mine, mode="api")], {}, meta, False)
        self.assertEqual([jm.oracle.site_of(b["url"])[1] for b in boards], ["CX_1002"])
        self.assertNotIn("_forget_gone_days", boards[0])  # (only its newest 100 are read: a missing one may be open)

    GS_HOST = "hdpc.fa.us2.oraclecloud.com"
    GS_PAGE = "https://higher.gs.com/roles?division=Engineering&country=United+States&sort=NEWEST"

    def test_goldman_and_uber_links_name_their_oracle_postings(self):
        # Your Goldman Sachs and Uber entries read the companies' own sites, which show their Oracle sites'
        # postings under the same numbers: one job either way (checked live: Uber 304922, Goldman 176573).
        gs = ("oracle", self.GS_HOST, "176573")
        self.assertIn(gs, jm.job_keys("https://higher.gs.com/roles/176573"))
        self.assertIn(gs, jm.job_keys(oracle_link(176573, site="LateralHiring", host=self.GS_HOST)))
        self.assertIn(gs, jm._posting_ids("https://higher.gs.com/roles/176573?source=x"))
        uber = ("oracle", "iaziqy.fa.ocs.oraclecloud.com", "304922")
        for link in ("https://jobs.uber.com/en/jobs/304922/", "https://jobs.uber.com/jobs/304922",
                     "https://www.jobs.uber.com/pt-br/jobs/304922/", oracle_link(304922, host="iaziqy.fa.ocs.oraclecloud.com")):
            self.assertIn(uber, jm._posting_ids(link), link)
        for other in ("https://higher.gs.com/roles", "https://higher.gs.com/about/176573", "https://jobs.uber.com/en/jobs/",
                      "https://jobs.uber.com/en/saved-jobs/304922/", "https://example.com/roles/176573"):
            self.assertFalse({k for k in jm.job_keys(other) if k[0] == "oracle"}, other)
        self.assertTrue(jm.oracle.knows_links_of(self.GS_PAGE))
        self.assertFalse(jm.oracle.knows_links_of("https://careers.example.com/jobs"))

    def test_goldmans_oracle_site_and_your_entry_email_a_job_once(self):
        today = datetime.now(timezone.utc).date().isoformat()
        old = (datetime.now(timezone.utc) - timedelta(days=30)).date().isoformat()
        gs_api = jm.oracle.api_url(self.GS_HOST, "LateralHiring")
        page = {"html": ""}
        self.patch(jm, "fetch_browser", lambda url, wait_for="", wait_seconds=8: (page["html"], []))

        def run(*shown):
            page["html"] = "".join(f'<a href="/roles/{n}">{title}</a>' for n, title in shown)
            self.pages[SIMPLIFY_URL] = json.dumps([simplify_row("Goldman Sachs", "Software Engineer",
                                                                url=oracle_link(999, site="LateralHiring", host=self.GS_HOST))])
            feed = target(name="SimplifyJobs New Grad", url=SIMPLIFY_URL, mode="feed", format="simplify",
                          categories=["Software"], max_age_days=7)
            mine = target(name="Goldman Sachs", url=self.GS_PAGE, mode="browser", link_selector="a[href*='/roles/']")
            boards = {"name": "Company job boards", "mode": "boards", "every_hours": 1, "catch_up_days": 3,
                      "platforms": ["oracle"], "tech_titles": ["software", "ml", "engineer"]}
            reported = self.run_monitor([feed, mine, boards], email=WORKING_EMAIL, role_filter=REPO_ROLE_FILTER, us_only=True)[1]
            meta = self.saved_state()[jm.BOARDS_KEY]
            meta["checked_at"] = (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds")
            jm.save_state({}, boards=meta)
            return reported

        self.assertEqual(run((1000, "Software Engineer, Analyst")), {})  # (first checks)
        # Your entry shows 1001; Goldman's Oracle site (linked by the list) has it too, and 1002, which your entry's page
        # doesn't show (another division): each is emailed once.
        self.pages[gs_api] = oracle_api((1001, "Software Engineer", today, "US"), (1002, "Backend Software Engineer", today, "US"),
                                        (999, "Software Engineer", old, "US"))
        self.assertEqual(run((1000, "Software Engineer, Analyst"), (1001, "Software Engineer")),
                         {"Goldman Sachs": ["Software Engineer", "Backend Software Engineer"]})
        self.assertIn(gs_api, self.saved_state())
        # Later your entry shows 1002 (the site emailed it): not again; 1003 is new to both, emailed once.
        self.pages[gs_api] = oracle_api((1003, "ML Engineer", today, "US"), (1001, "Software Engineer", today, "US"),
                                        (1002, "Backend Software Engineer", today, "US"))
        self.assertEqual(run((1001, "Software Engineer"), (1002, "Backend Software Engineer"), (1003, "ML Engineer")),
                         {"Goldman Sachs": ["ML Engineer"]})

    def test_an_oracle_site_with_the_name_of_an_entry_it_cant_be_matched_with_is_not_read(self):
        # Your entry reads another site whose links don't carry the Oracle numbers: both would email each job.
        meta = {"known": {}, "list_etags": {}}
        jm.grow_board_registry(meta["known"], [(oracle_link(5, site="LateralHiring", host=self.GS_HOST), "Goldman Sachs"),
                                               (oracle_link(6), "American Express")], "a list", "2026-11-20")

        def plan(*mine):
            return sorted(b["name"] for b in jm.plan_boards({"platforms": ["oracle"]}, list(mine), {}, meta, False))

        self.assertEqual(plan(), ["American Express", "Goldman Sachs"])
        self.assertEqual(plan(target(name="Goldman Sachs", url=self.GS_PAGE, mode="browser")), ["American Express", "Goldman Sachs"])
        self.assertEqual(plan(target(name="American Express, Inc.", url="https://careers.americanexpress.com/jobs", mode="browser"),
                              target(name="Goldman Sachs", url=self.GS_PAGE, mode="browser"),
                              target(name="Goldman Sachs", url="https://www.goldmansachs.com/careers/students", mode="browser",
                                     paused=True)), [])


def gh_redirect(board, job):
    """Greenhouse's answer about an open job: a redirect naming its board."""
    response = FakeResponse("", 301)
    response.headers["location"] = f"https://job-boards.greenhouse.io/embed/job_app?for={board}&token={job}"
    return response


class FakeWeb:
    """A get() for greenhouse.find_boards: url -> FakeResponse or page text (missing: 404)."""

    def __init__(self, pages):
        self.pages, self.asked = pages, []

    def __call__(self, url, headers=None, timeout=None, allow_redirects=True):
        self.asked.append(url)
        page = self.pages.get(url, FakeResponse("", 404))
        return page if isinstance(page, FakeResponse) else FakeResponse(page)


def gh_site_board(site, *jobs):
    """A Greenhouse board whose postings link to a company's own site: (id, title[, days ago])."""
    now = datetime.now(timezone.utc)
    return json.dumps({"jobs": [{"id": jid, "title": title, "location": {"name": "New York, NY"},
                                 "absolute_url": f"https://{site}/positions/{jid}?gh_jid={jid}",
                                 "first_published": (now - timedelta(days=rest[0] if rest else 0)).isoformat()}
                                for jid, title, *rest in jobs]})


class GreenhouseSitesTests(MonitorTestCase):
    """Greenhouse boards behind postings on companies' own sites (boards/greenhouse.py)."""

    EMBED = "https://boards.greenhouse.io/embed/job_app?token={}"
    API = "https://boards-api.greenhouse.io/v1/boards/{}/jobs"

    def link(self, job, site="careers.acme.com"):
        return f"https://{site}/positions/{job}?gh_jid={job}"

    def test_job_ids_on_companies_own_sites(self):
        job_id = jm.greenhouse.job_id
        self.assertEqual(job_id("https://careers.airbnb.com/positions/7859317?gh_jid=7859317"), "7859317")
        self.assertEqual(job_id("https://www.alphasights.com/careers/open-roles?gh_jid=8184208&utm_source=Simplify"), "8184208")
        for other in ("https://job-boards.greenhouse.io/airbnb/jobs/7859317?gh_jid=7859317",  # (board_api_url reads these)
                      "https://careers.airbnb.com/positions/7859317", "https://acme.com/jobs?gh_jid=abc", "https://[x?gh_jid=1", None, ""):
            self.assertIsNone(job_id(other), other)
        self.assertEqual(jm.greenhouse.site_of("https://www.Careers.Acme.com:443/x"), "careers.acme.com")
        self.assertEqual(job_id("https://careers.acme.com/x?GH_JID=123"), "123")  # (any case, as job_keys reads it)
        self.assertEqual(jm.greenhouse.guesses("careers.airbnb.com", ["Airbnb"]), [("airbnb", "Airbnb")])
        self.assertEqual(jm.greenhouse.guesses("abnormal.ai", ["Abnormal Security"]),
                         [("abnormal", "Abnormal Security"), ("abnormalsecurity", "Abnormal Security")])
        # A site several companies use: no guess from its address (it isn't any one's); each named after its company.
        self.assertEqual(jm.greenhouse.guesses("app.careerpuck.com", ["Lyft", "Domino Data Lab"]),
                         [("lyft", "Lyft"), ("dominodatalab", "Domino Data Lab")])
        self.assertEqual(jm.greenhouse.embedded_names(
            '<script src="https://boards.greenhouse.io/embed/job_board/js?for=acmecareers"></script>'
            '<a href="https://job-boards.greenhouse.io/acmecampus/jobs/1">x</a> boards-api.greenhouse.io/v1/boards/acmeapi/jobs'
            ' https://boards.greenhouse.io/embed/job_board?t=1&amp;for=acmeescaped {"u":"https:\\/\\/job-boards.eu.greenhouse.io\\/acmeeu"}'),
            ["acmecareers", "acmecampus", "acmeapi", "acmeescaped", "acmeeu"])
        # The same page's link with and without its Greenhouse job id is one posting (an entry reading the site records
        # it without), and only for the number that is the job id.
        self.assertIn(("block.xyz", "4567890"), jm.job_keys("https://block.xyz/careers/jobs/4567890?gh_jid=4567890"))
        self.assertTrue(jm.job_keys("https://block.xyz/careers/jobs/4567890?gh_jid=4567890")
                        & jm.job_keys("https://block.xyz/careers/jobs/4567890"))
        self.assertEqual(jm.job_keys("https://acme.com/team/1234567/jobs?gh_jid=7654321"), {("gh", "7654321")})

    def test_a_posting_names_its_board_and_each_job_is_asked_about_once(self):
        web = FakeWeb({self.EMBED.format(111): gh_redirect("acme", 111)})  # 222 and 333: closed (404)
        links = [(self.link(222), "Acme"), (self.link(111), "Acme"), (self.link(333), "Acme"), (self.link(444), "Acme")]
        memory = {}
        found = jm.greenhouse.find_boards(links, memory, "2026-11-20", get=web)
        self.assertEqual(found, [("https://job-boards.greenhouse.io/acme", "Acme")])
        self.assertEqual(jm.board_api_url(found[0][0]), ("greenhouse", self.API.format("acme")))
        # The three newest asked (in the order given: the lists' newest first); a board found: no guessing.
        self.assertEqual([u for u in web.asked], [self.EMBED.format(j) for j in (222, 111, 333)])
        self.assertEqual(memory["careers.acme.com"], {"boards": {"acme": "Acme"}, "asked": ["111", "222", "333"], "guessed": "2026-11-20"})
        web.asked.clear()
        self.assertEqual(jm.greenhouse.find_boards(links, memory, "2026-11-20", get=web), found)
        self.assertEqual(web.asked, [self.EMBED.format(444)])  # (each once)
        # A job the boards found already list isn't asked about.
        web.asked.clear()
        jm.greenhouse.find_boards(links + [(self.link(555), "Acme")], memory, "2026-11-20", get=web,
                                  listed=lambda names: {"555"} if names == ["acme"] else set())
        self.assertEqual(web.asked, [])

    def test_a_guess_is_taken_only_if_its_board_links_back_to_the_site(self):
        links = [(self.link(222), "Acme Robotics")]  # closed: Greenhouse can't name its board
        web = FakeWeb({self.API.format("acme"): gh_site_board("www.othersite.com", (1, "Engineer")),  # someone else's "acme"
                       self.API.format("acmerobotics"): gh_site_board("jobs.acme.com", (1, "Engineer"))})  # (same domain)
        memory = {}
        found = jm.greenhouse.find_boards(links, memory, "2026-11-20", get=web)
        self.assertEqual(found, [("https://job-boards.greenhouse.io/acmerobotics", "Acme Robotics")])
        self.assertEqual(memory["careers.acme.com"]["guessed"], "2026-11-20")

    def test_a_site_not_matched_is_guessed_again_a_week_later_and_a_page_can_name_its_board(self):
        links = [(self.link(222), "Acme")]
        memory = {}
        web = FakeWeb({})
        self.assertEqual(jm.greenhouse.find_boards(links, memory, "2026-11-20", get=web), [])
        self.assertEqual(memory["careers.acme.com"]["guessed"], "2026-11-20")
        # The posting's page embeds the board (a name no guess makes), whose postings link to the site.
        web.pages[self.link(222)] = '<script src="https://boards.greenhouse.io/embed/job_board/js?for=acmeco"></script>'
        web.pages[self.API.format("acmeco")] = gh_site_board("careers.acme.com", (1, "Engineer"))
        self.assertEqual(jm.greenhouse.find_boards(links, memory, "2026-11-25", get=web), [])  # (not yet)
        self.assertEqual(jm.greenhouse.find_boards(links, memory, "2026-11-27", get=web),
                         [("https://job-boards.greenhouse.io/acmeco", "Acme")])
        # A page that links another company's board on Greenhouse's own pages doesn't name this one's.
        memory, web.pages[self.link(222)] = {}, '<a href="https://job-boards.greenhouse.io/portfolioco">a portfolio company</a>'
        web.pages[self.API.format("portfolioco")] = json.dumps({"jobs": [{"id": 1, "title": "x", "absolute_url": gh_link(1)}]})
        self.assertEqual(jm.greenhouse.find_boards(links, memory, "2026-11-27", get=web), [])

    def test_a_site_several_companies_use_gets_each_its_board(self):
        site = "app.careerpuck.com"
        web = FakeWeb({self.EMBED.format(1): gh_redirect("lyft", 1), self.EMBED.format(2): gh_redirect("dominodatalab", 2)})
        found = jm.greenhouse.find_boards([(self.link(1, site), "Lyft"), (self.link(2, site), "Domino Data Lab")], {},
                                          "2026-11-20", get=web)
        self.assertEqual(sorted(found), [("https://job-boards.greenhouse.io/dominodatalab", "Domino Data Lab"),
                                         ("https://job-boards.greenhouse.io/lyft", "Lyft")])

    def test_too_many_requests_stops_the_run_and_unanswered_jobs_are_asked_again(self):
        web = FakeWeb({self.EMBED.format(1): FakeResponse("", 429)})
        memory = {}
        with self.assertLogs(jm.log, "INFO") as logs:
            self.assertEqual(jm.greenhouse.find_boards([(self.link(1), "Acme"), (self.link(2), "Acme")], memory,
                                                       "2026-11-20", get=web), [])
        self.assertEqual(web.asked, [self.EMBED.format(1)])
        self.assertEqual(memory["careers.acme.com"], {"boards": {}, "asked": []})  # (not "guessed": tried again next run)
        self.assertTrue(any("too many requests" in line for line in logs.output))
        web.pages[self.EMBED.format(1)] = gh_redirect("acme", 1)
        self.assertEqual(jm.greenhouse.find_boards([(self.link(1), "Acme")], memory, "2026-11-20", get=web),
                         [("https://job-boards.greenhouse.io/acme", "Acme")])

    def test_no_answer_is_not_a_no(self):
        # A guess or a page that got no answer (a timeout, a 5xx, a 403 from Greenhouse) isn't "no board":
        # the site is tried again next run, not a week later.
        links = [(self.link(222), "Acme")]
        web = FakeWeb({self.API.format("acme"): FakeResponse("", 503), self.link(222): FakeResponse("", 502)})
        memory = {}
        self.assertEqual(jm.greenhouse.find_boards(links, memory, "2026-11-20", get=web), [])
        self.assertNotIn("guessed", memory["careers.acme.com"])
        web.pages[self.API.format("acme")] = gh_site_board("careers.acme.com", (1, "Engineer"))
        self.assertEqual(jm.greenhouse.find_boards(links, memory, "2026-11-20", get=web),
                         [("https://job-boards.greenhouse.io/acme", "Acme")])
        # A closed posting's page (404) is often the site's usual page: it can still name the board.
        memory, web = {}, FakeWeb({self.link(222): FakeResponse('<script src="https://boards.greenhouse.io/embed/job_board/js?for=acmeco">', 404),
                                   self.API.format("acmeco"): gh_site_board("careers.acme.com", (1, "Engineer"))})
        self.assertEqual(jm.greenhouse.find_boards(links, memory, "2026-11-20", get=web),
                         [("https://job-boards.greenhouse.io/acmeco", "Acme")])

    def test_greenhouse_giving_no_answer_again_and_again_stops_the_run(self):
        # Greenhouse refuses the job lookups (as Workday refuses GitHub's machines): the run stops asking, but keeps guessing.
        web = FakeWeb({self.EMBED.format(j): FakeResponse("", 403) for j in range(1, 40)})
        links = [(self.link(j, f"site{j}.com"), f"Company {j}") for j in range(1, 40)]
        memory = {}
        with self.assertLogs(jm.log, "INFO") as logs:
            jm.greenhouse.find_boards(links, memory, "2026-11-20", get=web)
        lookups = [u for u in web.asked if "embed/job_app" in u]
        self.assertLessEqual(len(lookups), jm.greenhouse.GIVE_UP_AFTER + jm.greenhouse.WORKERS)
        self.assertTrue(any("No answer from Greenhouse to 10 lookup requests" in line for line in logs.output))
        self.assertEqual(sum(1 for r in memory.values() if r["asked"]), 0)  # (asked again next run)
        self.assertEqual(sum(1 for r in memory.values() if r.get("guessed")), 39)  # (the guesses were answered: no board)
        # Refusing everything: the board reads stop too, and no site is put off for a week.
        web, memory = FakeWeb({}), {}
        web.pages = type("All403", (dict,), {"get": lambda self, url, default=None: FakeResponse("", 403)})()
        with self.assertLogs(jm.log, "INFO"):
            jm.greenhouse.find_boards(links, memory, "2026-11-20", get=web)
        self.assertLessEqual(len([u for u in web.asked if "boards-api" in u]), jm.greenhouse.GIVE_UP_AFTER + jm.greenhouse.WORKERS)
        self.assertEqual(sum(1 for r in memory.values() if r.get("guessed")), 0)

    def test_a_side_board_doesnt_hide_the_main_one_and_odd_saved_records_are_repaired(self):
        # Thoughtworks' open postings in the lists were on its referrals board; its guess finds its main board too.
        web = FakeWeb({self.EMBED.format(1): gh_redirect("thoughtworksreferral", 1),
                       self.API.format("thoughtworks"): gh_site_board("www.thoughtworks.com", (5, "Developer"))})
        memory = {"thoughtworks.com": {"boards": ["not", "a", "dict"], "asked": "x"}, "other.com": "junk"}
        found = jm.greenhouse.find_boards([(self.link(1, "thoughtworks.com"), "Thoughtworks")], memory, "2026-11-20", get=web)
        self.assertEqual(sorted(found), [("https://job-boards.greenhouse.io/thoughtworks", "Thoughtworks"),
                                         ("https://job-boards.greenhouse.io/thoughtworksreferral", "Thoughtworks")])
        self.assertEqual(memory["other.com"], "junk")  # (a site not in this run's links is left as it is)
        # The highest job ids asked about are kept (they grow over time), whatever the order they were asked in.
        memory = {"careers.acme.com": {"boards": {"acme": "Acme"}, "asked": [str(n) for n in range(5000, 3000, -1)], "guessed": "2026-11-20"}}
        jm.greenhouse.find_boards([(self.link(1), "Acme")], memory, "2026-11-20", get=FakeWeb({}))
        self.assertEqual(len(memory["careers.acme.com"]["asked"]), jm.greenhouse.ASKED_KEPT)
        self.assertEqual(memory["careers.acme.com"]["asked"][-1], "5000")

    def test_a_board_your_entry_already_reads_on_the_site_is_not_read_again(self):
        # Your "Square / Block" entry reads block.xyz and records its links without the Greenhouse job id; Block's
        # Greenhouse board (found through the lists' block.xyz?gh_jid= links) would email each Block role a second time.
        meta = {"known": {}, "list_etags": {}, "greenhouse_sites": {
            "block.xyz": {"boards": {"block": "Block"}, "asked": [], "guessed": "2026-11-20"},
            "pinterestcareers.com": {"boards": {"pinterest": "Pinterest"}, "asked": [], "guessed": "2026-11-20"}}}
        jm.grow_board_registry(meta["known"], [("https://job-boards.greenhouse.io/block", "Block"),
                                               ("https://job-boards.greenhouse.io/pinterest", "Pinterest")], "companies' own sites", "2026-11-20")
        block = target(name="Square / Block", url="https://block.xyz/careers?locations=United%20States", mode="browser")
        pinterest = target(name="Pinterest", url="https://www.pinterestcareers.com/jobs/?team=University", mode="browser")
        state = {block["url"]: [{"url": "https://block.xyz/careers/jobs/4567890"}],
                 pinterest["url"]: [{"url": "https://www.pinterestcareers.com/jobs/8214783/intern/?gh_jid=8214783"}]}

        def plan(*mine):
            return sorted(b["name"] for b in jm.plan_boards({}, list(mine), state, meta, False))

        self.assertEqual(plan(), ["Block", "Pinterest"])
        # Pinterest's entry has the board's name and records the job ids: the two are matched both ways, both read.
        self.assertEqual(plan(block, pinterest), ["Pinterest"])
        self.assertEqual(plan(target(name="Pinterest University", url=pinterest["url"], mode="browser")), ["Block"])

    def test_a_companys_board_behind_its_own_site_is_found_checked_and_its_new_roles_emailed_once(self):
        feed = target(name="SimplifyJobs New Grad", url=SIMPLIFY_URL, mode="feed", format="simplify",
                      categories=["Software"], max_age_days=7)
        boards = {"name": "Company job boards", "mode": "boards", "every_hours": 1, "catch_up_days": 3,
                  "tech_titles": ["software", "new grad"]}
        self.pages[SIMPLIFY_URL] = json.dumps([simplify_row("Acme", "Software Engineer", url=self.link(4000001))])
        self.pages[self.EMBED.format(4000001)] = gh_redirect("acme", 4000001)
        self.pages[self.API.format("acme")] = gh_site_board("careers.acme.com", (4000001, "Software Engineer"),
                                                            (4000002, "Backend Engineer, New Grad"), (4000003, "Software Engineer II", 20))

        def run():
            reported = self.run_monitor([feed, boards], email=WORKING_EMAIL)[1]
            meta = self.saved_state()[jm.BOARDS_KEY]
            meta["checked_at"] = (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds")
            jm.save_state({}, boards=meta)
            return reported

        self.assertEqual(run(), {})  # (the list's first check records its row)
        # The row's link is on Acme's own site: Greenhouse names the board, which joins the registry and is checked;
        # its first check emails what went up in the last 3 days, but not the list's row again (the same job).
        self.assertEqual(run(), {"Acme": ["Backend Engineer, New Grad"]})
        meta = self.saved_state()[jm.BOARDS_KEY]
        self.assertEqual([(i["name"], i["url"], i["via"]) for i in meta["known"].values()],
                         [("Acme", self.API.format("acme"), "companies' own sites")])
        self.assertEqual(meta["greenhouse_sites"]["careers.acme.com"]["boards"], {"acme": "Acme"})
        self.pages[self.API.format("acme")] = gh_site_board("careers.acme.com", (4000001, "Software Engineer"),
                                                            (4000002, "Backend Engineer, New Grad"), (4000004, "Software Engineer, ML"))
        self.assertEqual(run(), {"Acme": ["Software Engineer, ML"]})
        self.assertEqual(sum(1 for u, _ in self.requests_made if u == self.EMBED.format(4000001)), 1)  # (asked once)
        self.assertIn("| Acme | [Greenhouse](https://job-boards.greenhouse.io/acme) |", jm.boards_page_path().read_text())


def roles(company, n, title="Software Engineer", start=0):
    return [{"title": f"{title} (team {i})", "url": f"https://{company.lower()}.example/jobs/{start + i}", "location": "New York, NY",
             "group": company} for i in range(n)]


def subject_of(msg) -> str:
    return str(email.header.make_header(email.header.decode_header(msg["Subject"])))


def html_of(msg) -> str:
    return next(p.get_payload(decode=True).decode() for p in msg.walk() if p.get_content_type() == "text/html")


class EmailPartsTests(MonitorTestCase):
    """A big email goes in parts, so Gmail (which cuts off an email past about 102 KB) shows all of it."""

    def test_a_small_email_is_one_part(self):
        parts = jm.email_parts({"Acme": roles("Acme", 3)})
        self.assertEqual([p["part"] for p in parts], [(1, 1)])
        self.assertNotIn("Part 1 of", jm.format_html_report(**parts[0]))
        self.assertTrue(jm.send_email({"email": WORKING_EMAIL}, {"Acme": roles("Acme", 3)}))
        self.assertEqual([subject_of(m) for _, m in self.emails_sent()], ["[Job Monitor] 3 new job postings found"])

    def test_a_big_email_goes_in_parts_each_under_the_limit_with_every_role_once(self):
        all_new = {"Acme": roles("Acme", 40), "Bigco": roles("Bigco", 700), **{f"Co{i}": roles(f"Co{i}", 5) for i in range(60)}}
        left_out = {"Acme": [{"title": "Senior Engineer", "url": "https://acme.example/jobs/s", "reason": "senior"}]}
        summary = {"total": 1, "by_reason": {"senior": 1}, "listed": [], "link": "left_out.md"}
        problems = [{"name": "Beta", "url": "u", "why": "it couldn't be read", "since": "2026-11-20T10:00:00"}]
        alarms = [{"name": "Workday", "why": "no company Workday site could be read", "since": None}]
        top = roles("Acme", 2, "Software Engineer, New Grad")
        parts = jm.email_parts(all_new, left_out, summary, problems, alarms, top)
        count = len(parts)
        self.assertGreater(count, 2)
        htmls = [jm.format_html_report(**p) for p in parts]
        self.assertTrue(all(len(h.encode()) <= jm.EMAIL_MAX_HTML_BYTES for h in htmls), [len(h) for h in htmls])
        # Every role exactly once, in order; Bigco is continued from part to part.
        listed = [j["url"] for p in parts for jobs in p["all_new"].values() for j in jobs]
        self.assertEqual(listed, [j["url"] for jobs in all_new.values() for j in jobs])
        self.assertIn("Bigco (continued)", [name for p in parts for name in p["all_new"]])
        # The warnings and top picks in the first part only; the left-out list in the last.
        self.assertIn("Sources that may have stopped working", htmls[0])
        self.assertIn("aren't working", htmls[0])
        self.assertIn("Top picks", htmls[0])
        self.assertFalse(any("Top picks" in h or "aren't working" in h for h in htmls[1:]))
        self.assertIn("Senior Engineer", htmls[-1])
        self.assertFalse(any("Senior Engineer" in h for h in htmls[:-1]))
        self.assertIn(f"Part 2 of {count}", htmls[1])
        self.assertIn(f"Part 2 of {count}", jm.format_plain_report(**parts[1]))
        # Sent as that many emails, numbered.
        self.assertTrue(jm.send_email({"email": WORKING_EMAIL}, all_new, left_out, summary, problems, alarms, top))
        sent = self.emails_sent()
        self.assertEqual([subject_of(m) for _, m in sent],
                         [f"[Job Monitor] 1040 new job postings found (+1 left out by filters) (part {i} of {count})" for i in range(1, count + 1)])
        self.assertTrue(all(len(html_of(m).encode()) <= jm.EMAIL_MAX_HTML_BYTES for _, m in sent))

    def test_after_a_failed_part_only_the_parts_not_sent_go_next_run(self):
        fails = {"left": 1}

        class FailSecond(FakeSMTP):
            def send_message(self, msg):
                if self.sent and fails["left"]:
                    fails["left"] -= 1
                    raise smtplib.SMTPServerDisconnected("gone")
                super().send_message(msg)
        self.patch(jm.smtplib, "SMTP", FailSecond)
        result = jm.send_email({"email": WORKING_EMAIL}, {"Bigco": roles("Bigco", 900)})
        self.assertFalse(result)
        self.assertEqual((result.sent, len(result.parts)), (1, 3))
        self.assertEqual(len(result.delivered()), sum(len(j) for j in result.parts[0]["all_new"].values()))
        # In a run: what went out isn't emailed again; the rest goes at the next run, then the pool is empty.
        FakeSMTP.instances.clear()
        fails["left"] = 1
        self.mark_known(URL)
        self.pages[URL] = page(*jobs_named(*[f"Software Engineer (team {i})" for i in range(800)]))
        with self.assertLogs(jm.log, "ERROR") as logs:
            ok, _ = self.run_monitor([target()], email=WORKING_EMAIL)
        self.assertFalse(ok)
        self.assertTrue(any("Only 1 of the email's" in line for line in logs.output))
        ok, _ = self.run_monitor([target()], email=WORKING_EMAIL)
        self.assertTrue(ok)
        urls = [a["href"] for _, m in self.emails_sent() for a in jm.BeautifulSoup(html_of(m), "html.parser").select("li a")]
        self.assertEqual(len(urls), 800)
        self.assertEqual(len(set(urls)), 800)
        self.assertNotIn(jm.ALERT_POOL_KEY, self.saved_state())


class TopPicksTests(MonitorTestCase):
    def test_new_grad_titles_first_then_the_lists_then_level_one(self):
        alerts = (roles("Acme", 6) + [{"title": "Software Engineer II", "url": "https://acme.example/jobs/ii", "group": "Acme"},
                                      {"title": "Software Engineer I", "url": "https://acme.example/jobs/i", "group": "Acme"},
                                      {"title": "Backend Engineer", "url": "https://zeta.example/jobs/1",
                                       "group": "Zeta (via SimplifyJobs New Grad)", "source": SIMPLIFY_URL},
                                      {"title": "Software Engineer, New Grad 2026", "url": "https://beta.example/jobs/ng", "group": "Beta"},
                                      {"title": "University Graduate - Software Engineer", "url": "https://gamma.example/jobs/u", "group": "Gamma"}])
        picks = jm.pick_top(alerts, jm.TOP_PICKS, {SIMPLIFY_URL})
        self.assertEqual([j["title"] for j in picks], ["Software Engineer, New Grad 2026", "University Graduate - Software Engineer",
                                                       "Software Engineer I", "Backend Engineer"])
        self.assertIn("Zeta: Backend Engineer", jm.format_plain_report({"Acme": alerts}, top=picks))
        self.assertEqual(jm.pick_top(alerts[:9], jm.TOP_PICKS, {SIMPLIFY_URL}), [])  # (fewer than 10 roles: no section)
        self.assertEqual(jm.pick_top(alerts, False, {SIMPLIFY_URL}), [])
        self.assertEqual(len(jm.pick_top(alerts, {**jm.TOP_PICKS, "limit": 2}, {SIMPLIFY_URL})), 2)
        for off in (None, 0, "off", "false", False):
            self.assertEqual(jm.pick_top(alerts, off, {SIMPLIFY_URL}), [], off)
        with self.assertLogs(jm.log, "WARNING"):
            self.assertEqual(len(jm.pick_top(alerts, {"limit": "abc", "titles": "new grad"}, {SIMPLIFY_URL})), 4)  # (defaults)

    def test_software_first_and_a_lists_senior_roles_left_out(self):
        patterns = jm.compile_role_filter(REPO_ROLE_FILTER)
        listed = lambda title, n: {"title": title, "url": f"https://x.example/{n}", "group": f"Co{n} (via SimplifyJobs New Grad)",
                                   "source": SIMPLIFY_URL}
        alerts = roles("Acme", 8) + [listed("Customer Experience Analyst", 1), listed("Software Engineer II", 2),
                                      listed("Backend Developer", 3), listed("Senior Data Engineer", 4),
                                      {"title": "Entry Level Associate Scientist - Biochemistry", "url": "https://b.example/1", "group": "Bio"},
                                      {"title": "Entry Level Software Developer", "url": "https://c.example/1", "group": "Cee"}]
        picks = [j["title"] for j in jm.pick_top(alerts, jm.TOP_PICKS, {SIMPLIFY_URL}, patterns)]
        self.assertEqual(picks, ["Entry Level Software Developer", "Entry Level Associate Scientist - Biochemistry",
                                 "Backend Developer", "Customer Experience Analyst"])
        for title in ("Software Engineer - New College Graduate", "2027 Grads: Software Engineer", "Undergrad Software Engineer"):
            self.assertTrue(jm.pick_top(roles("Acme", 9) + [{"title": title, "url": "u", "group": "Z"}], jm.TOP_PICKS), title)

    def test_the_email_has_its_top_picks(self):
        self.mark_known(URL)
        self.pages[URL] = page(*jobs_named(*[f"Data Engineer (team {i})" for i in range(9)], "Software Engineer - New Grad"))
        _, reported = self.run_monitor([target()], email=WORKING_EMAIL)
        self.assertEqual([j["title"] for j in self.email_extra["top"]], ["Software Engineer - New Grad"])
        self.assertIn("Top picks", html_of(self.emails_sent()[0][1]))


def source_record(**extra):
    return {**jm.sources.record_of(None), **extra}


def run_at(hours_ago, **extra):
    return {"at": (datetime.now() - timedelta(hours=hours_ago)).isoformat(timespec="seconds"), "matches": 0, "left_out": 0,
            "entries": 0, "failing": 0, **extra}


class SourceAlarmTests(unittest.TestCase):
    """Whole sources that look like they stopped (sources.py)."""

    def alarms(self, record, **kwargs):
        return [a["name"] + ": " + a["why"] for a in jm.sources.alarms(record, datetime.now(), **{"boards_on": True, **kwargs})]

    def test_workday_reading_nothing_three_runs_in_a_row(self):
        blocked = {"sites": 300, "read": 0, "skipped": 3, "missing": 0}
        late = {"sites": 300, "read": 0, "skipped": 0, "missing": 0}
        fine = {"sites": 300, "read": 250, "skipped": 2, "missing": 0}
        self.assertEqual(self.alarms(source_record(runs=[run_at(2, workday=fine), run_at(1.5, workday=blocked), run_at(1, workday=blocked)])), [])
        found = self.alarms(source_record(runs=[run_at(h, workday=blocked) for h in (1.5, 1, 0.5)]))
        self.assertEqual(len(found), 1)
        self.assertIn("refusing the browser", found[0])
        self.assertIn("ran out of time", self.alarms(source_record(runs=[run_at(h, workday=late) for h in (1.5, 1, 0.5)]))[0])
        # (runs that didn't read Workday, as when you pause it, don't count; nor the runs before you paused it)
        self.assertEqual(self.alarms(source_record(runs=[run_at(2, workday=fine)] + [run_at(h) for h in (1.5, 1, 0.5)])), [])
        self.assertEqual(self.alarms(source_record(runs=[run_at(h, workday=blocked) for h in (1.5, 1, 0.5)]), workday_on=False), [])
        # The reason Workday reading stopped, when it says one; every site answering "not found".
        stopped = {**late, "why": "3 Workday sites in a row couldn't be read (last: the browser didn't start)"}
        self.assertIn("the browser didn't start", self.alarms(source_record(runs=[run_at(h, workday=stopped) for h in (1.5, 1, 0.5)]))[0])
        gone = {**late, "missing": 40}
        self.assertIn("answered \"not found\"", self.alarms(source_record(runs=[run_at(h, workday=gone) for h in (1.5, 1, 0.5)]))[0])

    def test_a_platform_whose_boards_mostly_fail_in_three_checks(self):
        bad = {"lever": {"ok": 20, "unchanged": 30, "missing": 1, "failed": 150}, "greenhouse": {"ok": 900, "unchanged": 0, "missing": 0, "failed": 3}}
        found = self.alarms(source_record(runs=[run_at(h, boards=bad) for h in (3, 2, 1)]))
        self.assertEqual(found, ["Lever: 151 of its 201 company boards couldn't be read or answered \"not found\", and as many in each of the last 3 checks"])
        self.assertEqual(self.alarms(source_record(runs=[run_at(h, boards=bad) for h in (3, 2, 1)]), boards_on=False), [])
        # A platform whose every board answers 404 (its API moved).
        moved = {"lever": {"ok": 0, "unchanged": 0, "missing": 345, "failed": 0}}
        self.assertEqual(len(self.alarms(source_record(runs=[run_at(h, boards=moved) for h in (3, 2, 1)]))), 1)
        self.assertEqual(self.alarms(source_record(runs=[run_at(h, boards=bad) for h in (2, 1)])), [])
        few = {"ashby": {"ok": 1, "unchanged": 0, "missing": 0, "failed": 5}}
        self.assertEqual(self.alarms(source_record(runs=[run_at(h, boards=few) for h in (3, 2, 1)])), [])

    def test_boards_not_checked_and_lists_gone_quiet(self):
        old = (datetime.now() - timedelta(hours=80)).isoformat(timespec="seconds")
        recent = (datetime.now() - timedelta(hours=5)).isoformat(timespec="seconds")
        record = source_record(feeds={SIMPLIFY_URL: {"name": "SimplifyJobs New Grad", "last_new": old},
                                      "https://x.example/f": {"name": "Other list", "last_new": recent}},
                               lists={INTERNSHIPS_URL: {"name": "SimplifyJobs Internships", "read": old}})
        found = self.alarms(record, boards_checked_at=(datetime.now() - timedelta(hours=7)).isoformat(), boards_on=True)
        self.assertEqual([f.split(":")[0] for f in found], ["Company job boards", "SimplifyJobs New Grad", "SimplifyJobs Internships"])
        # Boards checked every 8 hours: 7 hours is normal.
        self.assertEqual(len(self.alarms(record, boards_checked_at=(datetime.now() - timedelta(hours=7)).isoformat(), every_hours=8)), 2)
        self.assertEqual(len(self.alarms(record, boards_checked_at=recent, boards_on=True)), 2)
        self.assertEqual(len(self.alarms(record, boards_checked_at=None, boards_on=True)), 2)  # (never checked yet)

    def test_odd_saved_records_are_repaired_and_old_runs_dropped(self):
        record = jm.sources.record_of({"runs": [{"at": "x"}, run_at(60), run_at(1)], "feeds": [], "lists": {"a": "b"}, "summary_at": 5})
        self.assertEqual((len(record["runs"]), record["feeds"], record["lists"], record["summary_at"]), (2, {}, {}, None))
        odd = jm.sources.record_of({"runs": [{**run_at(1), "matches": "7", "at": "2026-11-21T10:00:00Z",
                                              "workday": {"sites": "x", "read": None}, "boards": {"lever": {"failed": "2"}}}]})
        self.assertEqual(odd["runs"][0]["matches"], 7)
        self.assertEqual(jm.sources.alarms(odd, datetime(2026, 11, 21, 12), boards_on=True), [])  # (no crash)
        jm.sources.note_run(record, datetime.now())
        self.assertEqual(len(record["runs"]), 2)  # (the one 60 hours old is dropped)

    def test_the_daily_check_is_due_once_a_utc_day_from_its_hour(self):
        due = jm.sources.summary_due
        at = lambda d, h, m=0: datetime(2026, 11, d, h, m)
        sent = lambda d, h: source_record(summary_at=at(d, h).isoformat(), summary_day=at(d, h).date().isoformat())
        self.assertFalse(due(source_record(), at(21, 13, 30), 14))
        self.assertTrue(due(source_record(), at(21, 14, 5), 14))
        self.assertFalse(due(sent(21, 14), at(21, 23), 14))
        self.assertTrue(due(sent(21, 14), at(22, 14, 2), 14))
        self.assertFalse(due(sent(21, 14), at(22, 9), 14))
        # (the hour is UTC whatever the machine's clock: here local time is 5 hours behind)
        self.assertTrue(due(sent(21, 14), at(22, 9, 5), 14, now_utc=at(22, 14, 5)))
        # Hour 0 doesn't creep earlier day by day: once per UTC day.
        self.assertFalse(due(sent(21, 0), at(21, 20), 0))
        self.assertTrue(due(sent(21, 0), at(22, 0, 3), 0))
        # The runs stopped from before 14:00 to the next morning: at once, then not again that day.
        self.assertTrue(due(sent(20, 14), at(21, 22), 14))
        self.assertFalse(due(sent(21, 22), at(22, 9), 14))

    def test_a_run_is_noted_with_boards_waiting_after_a_404_and_lists_another_config_read(self):
        lever = "https://api.lever.co/v0/postings/acme"
        gone = {f"https://api.lever.co/v0/postings/co{i}": "2026-11-21T01:00:00" for i in range(30)}
        meta = {"missing": gone, "workday": {}, "list_read": {}}
        today = datetime.now().date().isoformat()
        feed = {"name": "SimplifyJobs New Grad", "url": SIMPLIFY_URL, "mode": "feed"}
        state = {SIMPLIFY_URL: [{"url": "https://a.example/1", "first_seen": today}]}
        record = jm._note_sources(state, dict(state), "cfg", [feed], [{"mode": "boards"}], meta, {"workday": {}},
                                  {lever: ("ok", [], None)}, True, True, [], [], 0, [], datetime.now())
        # (30 Lever boards in their day's wait after a 404 are counted as not found, so a moved API is noticed)
        self.assertEqual(record["runs"][-1]["boards"]["lever"], {"ok": 1, "unchanged": 0, "missing": 30, "failed": 0})
        # The list added nothing this run (another config sharing state.json recorded it), but its newest
        # posting was first seen today: not quiet.
        record["feeds"][SIMPLIFY_URL]["last_new"] = (datetime.now() - timedelta(days=5)).isoformat(timespec="seconds")
        jm.sources.note_feed(record, SIMPLIFY_URL, "SimplifyJobs New Grad", False, datetime.now(), today)
        self.assertEqual(jm.sources.alarms(record, datetime.now()), [])

    def test_the_first_day_counts_runs_since_the_first_one_noted(self):
        record = source_record(runs=[run_at(h / 2) for h in range(18, -1, -1)])
        _, plain, _ = jm.sources.summary(record, datetime.now(), {}, [], [])
        self.assertIn("Runs since the first run noted", plain)
        self.assertIn(": 19 (about 19 expected, one every 30 minutes)", plain)
        _, plain, _ = jm.sources.summary(source_record(runs=[run_at(h / 2) for h in range(60, -1, -1)]), datetime.now(), {}, [], [])
        self.assertIn("Runs in the last 24 hours: 48 (about 48 expected", plain)


class DailyCheckTests(MonitorTestCase):
    """The daily "still running" email, and the source warnings in the emails."""

    def run_daily(self, **extra):
        self.mark_known(URL) if not jm.STATE_PATH.exists() else None
        self.pages[URL] = page(*jobs_named("Software Engineer"))
        return self.run_monitor([target()], email=WORKING_EMAIL, daily_check_hour_utc=0, email_every_hours=2, **extra)

    def daily_emails(self):
        return [m for _, m in self.emails_sent() if "Daily check" in subject_of(m)]

    def test_once_a_day_with_what_was_checked_and_sent(self):
        self.run_daily()
        daily = self.daily_emails()
        self.assertEqual(len(daily), 1)
        self.assertEqual(subject_of(daily[0]), "[Job Monitor] Daily check: running fine (1 role emailed in the last day)")
        body = html_of(daily[0])
        self.assertIn("All sources look normal", body)
        self.assertIn("Your own entries: 1 checked each run, all working.", body)
        record = next(iter(self.saved_state()[jm.SOURCES_KEY].values()))
        self.assertTrue(record["summary_at"])
        self.assertEqual([e["roles"] for e in record["emails"]], [1])
        self.run_daily()
        self.assertEqual(len(self.daily_emails()), 1)  # (not again the same day)

    def test_off_unless_the_config_asks_for_it(self):
        self.mark_known(URL)
        self.pages[URL] = page(*jobs_named("Software Engineer"))
        self.run_monitor([target()], email=WORKING_EMAIL)
        self.assertEqual(self.daily_emails(), [])

    def test_a_list_gone_quiet_is_warned_about_in_the_emails_and_the_daily_check(self):
        feed = target(name="SimplifyJobs New Grad", url=SIMPLIFY_URL, mode="feed", format="simplify", categories=["Software"], max_age_days=7)
        self.pages[SIMPLIFY_URL] = json.dumps([simplify_row("Acme", "Software Engineer")])
        self.run_monitor([feed], email=WORKING_EMAIL)  # (first check)
        state = self.saved_state()
        config_id, record = next(iter(state[jm.SOURCES_KEY].items()))
        record["feeds"][SIMPLIFY_URL]["last_new"] = (datetime.now() - timedelta(days=4)).isoformat(timespec="seconds")
        old_day = (datetime.now() - timedelta(days=4)).date().isoformat()
        jm.save_state({SIMPLIFY_URL: [{**r, "first_seen": old_day} for r in state[SIMPLIFY_URL]]}, sources=(config_id, record))
        self.pages[URL] = page(*jobs_named("Software Engineer"))
        self.mark_known(URL)
        with self.assertLogs(jm.log, "WARNING") as logs:
            self.run_monitor([feed, target()], email=WORKING_EMAIL, daily_check_hour_utc=0)
        self.assertTrue(any("[sources] 1 may have stopped working: SimplifyJobs New Grad" in line for line in logs.output))
        self.assertEqual([a["name"] for a in self.email_extra["alarms"]], ["SimplifyJobs New Grad"])
        daily = self.daily_emails()
        self.assertEqual(subject_of(daily[0]), "[Job Monitor] Daily check: 1 source(s) may have stopped working (1 role emailed in the last day)")
        self.assertIn("no new posting since", html_of(daily[0]))
        # A new posting in the list: the warning goes.
        self.pages[SIMPLIFY_URL] = json.dumps([simplify_row("Acme", "Software Engineer"), simplify_row("Beta", "Data Engineer")])
        self.run_monitor([feed, target()], email=WORKING_EMAIL)
        record = self.saved_state()[jm.SOURCES_KEY][config_id]
        self.assertEqual(jm.sources.alarms(record, datetime.now()), [])

    def test_a_daily_check_that_cant_be_sent_fails_the_run_and_is_tried_again(self):
        self.mark_known(URL)
        self.pages[URL] = page()
        FakeSMTP.fail_with = smtplib.SMTPAuthenticationError(535, b"bad password")
        self.addCleanup(setattr, FakeSMTP, "fail_with", None)
        with self.assertLogs(jm.log, "ERROR"):
            ok, _ = self.run_monitor([target()], email=WORKING_EMAIL, daily_check_hour_utc=0)
        self.assertFalse(ok)
        self.assertIsNone(next(iter(self.saved_state()[jm.SOURCES_KEY].values()))["summary_at"])
        FakeSMTP.fail_with = None
        ok, _ = self.run_monitor([target()], email=WORKING_EMAIL, daily_check_hour_utc=0)
        self.assertTrue(ok)
        self.assertEqual(len(self.daily_emails()), 1)


class BoardTests(MonitorTestCase):
    """Company job boards found through the feeds ("mode": "boards")."""

    def setUp(self):
        super().setUp()
        self.patch(jm, "LEVER_DELAY_SECONDS", 0)
        self.pages[SIMPLIFY_URL] = json.dumps([
            simplify_row("Acme", "New Grad Software Engineer", url=gh_link(1))])

    def boards_entry(self, **extra):
        return {"name": "Company job boards", "mode": "boards", "every_hours": 1,
                "tech_titles": ["software", "ml", "infrastructure", "new grad"], **extra}

    def targets(self, **extra):
        return [target(name="SimplifyJobs New Grad", url=SIMPLIFY_URL, mode="feed", format="simplify",
                       categories=["Software"], max_age_days=7), self.boards_entry(**extra)]

    def run_boards(self, boards=None, **config_extra):
        """One run with the feed and the boards entry (plus the settings in boards)."""
        config_extra.setdefault("role_filter", REPO_ROLE_FILTER)
        config_extra.setdefault("us_only", True)
        return self.run_monitor(self.targets(**(boards or {})), email=WORKING_EMAIL, **config_extra)

    def boards_meta(self):
        return self.saved_state().get(jm.BOARDS_KEY, {})

    def age_board_check(self, hours=2):
        meta = self.boards_meta()
        meta["checked_at"] = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
        jm.save_state({}, boards=meta)

    def board_requests(self, url=GH_ACME):
        return [u for u, _ in self.requests_made if u == url]

    def start(self, *board_jobs):
        """Feed and board recorded (silent first checks); the board was just checked."""
        self.pages[GH_ACME] = gh_board(*board_jobs)
        self.run_boards()                      # the feed's first check
        _, reported = self.run_boards()        # finds Acme's board; its first check
        self.assertEqual(reported, {})
        self.assertIn(GH_ACME, self.saved_state())

    def test_board_links_are_recognised(self):
        cases = {
            "https://job-boards.greenhouse.io/Acme/jobs/123": ("greenhouse", GH_ACME),
            "https://boards.greenhouse.io/acme/jobs/123?gh_jid=123": ("greenhouse", GH_ACME),
            "https://job-boards.eu.greenhouse.io/acme/jobs/5": ("greenhouse", GH_ACME),
            "https://boards.greenhouse.io/embed/job_app?for=acme&token=5": ("greenhouse", GH_ACME),
            "https://jobs.ashbyhq.com/zeta/0e7ecf76-1111-4a2b-9c3d-000000000001/application": ("ashby", ASHBY_ZETA),
            "https://jobs.lever.co/Beta/0e7ecf76-1111-4a2b-9c3d-000000000001/apply": ("lever", "https://api.lever.co/v0/postings/beta"),
            "https://jobs.eu.lever.co/beta/x": ("lever", "https://api.eu.lever.co/v0/postings/beta"),
        }
        for link, expected in cases.items():
            self.assertEqual(jm.board_api_url(link), expected, link)
        for link in ("https://careers.acme.example/jobs/1", "https://jobs.ashbyhq.com/", "https://boards.greenhouse.io/embed/job_app"):
            self.assertIsNone(jm.board_api_url(link), link)

    def test_a_board_is_found_through_a_feed_checked_hourly_and_kept_for_good(self):
        self.start((1, "New Grad Software Engineer"))
        self.assertEqual(self.boards_meta()["known"]["https://boards-api.greenhouse.io/v1/boards/acme/jobs"]["name"], "Acme")
        # Within the hour the board isn't asked again.
        self.pages[GH_ACME] = gh_board((1, "New Grad Software Engineer"), (2, "Software Engineer, Infrastructure"))
        before = len(self.board_requests())
        _, reported = self.run_boards()
        self.assertEqual((reported, len(self.board_requests())), ({}, before))
        # An hour later it is, and its new posting is emailed under the company's name.
        self.age_board_check(hours=1)
        _, reported = self.run_boards()
        self.assertEqual(reported, {"Acme": ["Software Engineer, Infrastructure"]})
        # The feed drops Acme: the board stays.
        self.pages[SIMPLIFY_URL] = json.dumps([simplify_row("Other", "Data Engineer")])
        self.run_boards()
        self.age_board_check()
        self.pages[GH_ACME] = gh_board((1, "New Grad Software Engineer"), (2, "Software Engineer, Infrastructure"), (3, "ML Engineer"))
        _, reported = self.run_boards()
        self.assertEqual(reported, {"Acme": ["ML Engineer"]})

    def test_only_tech_titles_and_left_outs_are_not_listed(self):
        self.start((1, "New Grad Software Engineer"))
        self.age_board_check()
        self.pages[GH_ACME] = gh_board((1, "New Grad Software Engineer"), (2, "Account Executive"), (3, "Senior Software Engineer"),
                                       (4, "Software Engineer"), (5, "Software Engineer", "London, UK"))
        _, reported = self.run_boards()
        self.assertEqual((reported, self.left_out), ({"Acme": ["Software Engineer"]}, {}))
        self.assertEqual(len(self.saved_state()[GH_ACME]), 5)  # all recorded as seen
        self.assertEqual(self.saved_state()[GH_ACME][-1], {"url": gh_link(1)})  # boards keep only the link

    def test_gig_work_titles_and_skipped_companies_are_left_out(self):
        self.pages[SIMPLIFY_URL] = json.dumps([
            simplify_row("Acme", "New Grad Software Engineer", url=gh_link(1)),
            simplify_row("Prolific", "AI Trainer", url="https://jobs.ashbyhq.com/prolific/0e7ecf76-1111-4a2b-9c3d-000000000001")])
        self.pages["https://api.ashbyhq.com/posting-api/job-board/prolific"] = json.dumps({"jobs": []})
        self.patch(self, "boards_entry", lambda **extra: {
            "name": "Company job boards", "mode": "boards", "tech_titles": ["software", "ai"],
            "skip_titles": ["trainer", "annotation"], "skip_companies": ["Prolific"], **extra})
        self.start((1, "New Grad Software Engineer"))
        self.assertNotIn("https://api.ashbyhq.com/posting-api/job-board/prolific", [u for u, _ in self.requests_made])
        self.age_board_check()
        self.pages[GH_ACME] = gh_board((1, "New Grad Software Engineer"), (2, "AI Trainer - Fluent Spanish"),
                                       (3, "Data Annotation Specialist - AI"), (4, "AI Software Engineer"))
        _, reported = self.run_boards()
        self.assertEqual(reported, {"Acme": ["AI Software Engineer"]})

    def test_new_grad_words_count_only_with_an_engineering_word(self):
        self.patch(self, "boards_entry", lambda **extra: {
            "name": "Company job boards", "mode": "boards", "tech_titles": ["software"], "level_titles": ["new grad", "entry level"], **extra})
        self.start((1, "New Grad Software Engineer"))
        self.age_board_check()
        self.pages[GH_ACME] = gh_board((1, "New Grad Software Engineer"), (2, "Entry-Level Geologist"), (3, "Supply Chain (New Grad)"),
                                       (4, "Build & Release Engineer - New Grad"), (5, "Software Developer"))
        _, reported = self.run_boards()
        self.assertEqual(sorted(reported["Acme"]), ["Build & Release Engineer - New Grad", "Software Developer"])

    def test_the_shipped_level_words_need_an_engineering_word(self):
        config = jm.load_config(REPO_DIR / "config_all.json")
        [entry] = [t for t in config["targets"] if t.get("mode") == "boards"]
        self.assertEqual(entry["level_titles"], ["new grad", "early career", "entry level"])
        self.assertFalse(set(entry["level_titles"]) & set(entry["tech_titles"]))

    def test_an_unchanged_board_answers_304_and_is_not_read(self):
        self.start((1, "New Grad Software Engineer"))
        sent = []
        real_get = self.fake_get

        def get(url, *args, **kwargs):
            if url == GH_ACME:
                sent.append((kwargs.get("headers") or {}).get("If-None-Match"))
                if sent[-1] == '"v1"':
                    return FakeResponse("", status_code=304)
                response = FakeResponse(self.pages[GH_ACME])
                response.headers["ETag"] = '"v1"'
                return response
            return real_get(url, *args, **kwargs)

        self.patch(jm.requests, "get", get)
        self.age_board_check()
        self.pages[GH_ACME] = gh_board((1, "New Grad Software Engineer"), (2, "Software Engineer"))
        _, reported = self.run_boards()
        self.assertEqual((reported, sent), ({"Acme": ["Software Engineer"]}, [None]))
        self.assertEqual(self.boards_meta()["etags"], {GH_ACME: '"v1"'})
        self.age_board_check()
        _, reported = self.run_boards()
        self.assertEqual((reported, sent), ({}, [None, '"v1"']))

    def test_a_board_that_is_gone_is_tried_again_a_day_later(self):
        self.start((1, "New Grad Software Engineer"))
        real_get = self.fake_get
        self.patch(jm.requests, "get", lambda url, *a, **k: FakeResponse("{}", status_code=404) if url == GH_ACME else real_get(url, *a, **k))
        self.age_board_check()
        with self.assertLogs(jm.log, "INFO") as logs:
            self.run_boards()
        self.assertIn("1 not found", "\n".join(logs.output))
        self.assertIn(GH_ACME, self.boards_meta()["missing"])
        self.age_board_check(hours=2)
        self.patch(jm.requests, "get", lambda url, *a, **k: self.fail("asked a missing board within a day") if url == GH_ACME else real_get(url, *a, **k))
        self.run_boards()
        self.assertIn(GH_ACME, self.saved_state())  # its postings are kept

    def test_a_boards_first_check_does_not_hide_a_posting_the_feed_lists_later(self):
        # The feeds list postings a day or two late: by then the board has recorded it silently.
        self.start((1, "New Grad Software Engineer"), (7, "Software Engineer II"))
        self.pages[SIMPLIFY_URL] = json.dumps([
            simplify_row("Acme", "New Grad Software Engineer", url=gh_link(1)),
            simplify_row("Acme", "Software Engineer II", url=gh_link(7))])
        _, reported = self.run_boards()
        self.assertEqual(reported, {"Acme (via SimplifyJobs New Grad)": ["Software Engineer II"]})

    def test_a_posting_is_emailed_once_whether_the_feed_or_the_board_finds_it_first(self):
        self.start((1, "New Grad Software Engineer"))
        # The board first, then the feed.
        self.age_board_check()
        self.pages[GH_ACME] = gh_board((1, "New Grad Software Engineer"), (2, "Software Engineer"))
        _, reported = self.run_boards()
        self.assertEqual(reported, {"Acme": ["Software Engineer"]})
        rows = [simplify_row("Acme", "New Grad Software Engineer", url=gh_link(1)),
                simplify_row("Acme", "Software Engineer", url=gh_link(2, "boards.greenhouse.io"))]
        self.pages[SIMPLIFY_URL] = json.dumps(rows)
        _, reported = self.run_boards()
        self.assertEqual(reported, {})
        # The feed first, then the board.
        rows.append(simplify_row("Acme", "ML Engineer", url=gh_link(3)))
        self.pages[SIMPLIFY_URL] = json.dumps(rows)
        _, reported = self.run_boards()
        self.assertEqual(reported, {"Acme (via SimplifyJobs New Grad)": ["ML Engineer"]})
        self.age_board_check()
        self.pages[GH_ACME] = gh_board((1, "New Grad Software Engineer"), (2, "Software Engineer"), (3, "ML Engineer"))
        _, reported = self.run_boards()
        self.assertEqual(reported, {})

    def test_waiting_board_postings_go_out_on_a_run_that_does_not_check_boards(self):
        self.start((1, "New Grad Software Engineer"))
        jm.save_state({}, last_email=(datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds"))
        self.age_board_check()
        self.pages[GH_ACME] = gh_board((1, "New Grad Software Engineer"), (2, "Software Engineer"))
        self.run_boards(email_every_hours=2)
        self.assertEqual(self.emails_attempted, 0)
        jm.save_state({}, last_email=(datetime.now() - timedelta(hours=3)).isoformat(timespec="seconds"))
        _, reported = self.run_boards(email_every_hours=2)  # boards not due this run
        self.assertEqual(reported, {"Acme": ["Software Engineer"]})

    def test_an_ashby_board_whose_name_has_spaces(self):
        api = "https://api.ashbyhq.com/posting-api/job-board/Alpaca%20Research"
        for link in ("https://jobs.ashbyhq.com/Alpaca%20Research/32ea17a8-756d-4873-bd8b-51bd65bac175",
                     "https://jobs.ashbyhq.com/Alpaca Research", "https://jobs.ashbyhq.com/Alpaca%20Research?utm_source=x"):
            self.assertEqual(jm.board_api_url(link), ("ashby", api), link)
        for bad in ("https://jobs.ashbyhq.com/a%2Fb", "https://jobs.ashbyhq.com/", "https://jobs.ashbyhq.com/%3Cx%3E"):
            self.assertIsNone(jm.board_api_url(bad), bad)
        self.assertEqual(jm._board_page(api), "https://jobs.ashbyhq.com/Alpaca%20Research")
        self.pages[api] = json.dumps({"jobs": [{"id": "0e7ecf76-1111-4a2b-9c3d-000000000001", "title": "Software Engineer",
                                                "jobUrl": "https://jobs.ashbyhq.com/Alpaca%20Research/0e7ecf76-1111-4a2b-9c3d-000000000001",
                                                "location": "Boise, ID", "isListed": True}]})
        self.assertEqual([j["title"] for j in jm.fetch_ashby_api_jobs(api)], ["Software Engineer"])
        known = {}
        jm.grow_board_registry(known, [("https://jobs.ashbyhq.com/Alpaca%20Research/x", "Alpaca Research")], "a list", "2026-11-21")
        self.assertEqual([(i["name"], i["url"]) for i in known.values()], [("Alpaca Research", api)])

    def test_boards_from_the_config_and_from_lists_used_only_to_find_companies(self):
        self.pages[INTERNSHIPS_URL] = json.dumps([
            {"url": "https://jobs.ashbyhq.com/zeta/0e7ecf76-1111-4a2b-9c3d-000000000001", "company_name": "Zeta"}])
        self.pages[ASHBY_ZETA] = json.dumps({"jobs": []})
        self.pages["https://boards-api.greenhouse.io/v1/boards/gamma/jobs"] = gh_board()
        targets = [self.boards_entry(
            find_companies_in=[{"name": "SimplifyJobs Internships", "url": INTERNSHIPS_URL}],
            companies=[{"name": "Gamma", "link": "https://job-boards.greenhouse.io/gamma"}])]
        self.run_monitor(targets, email=WORKING_EMAIL)
        known = {info["name"]: info["via"] for info in self.boards_meta()["known"].values()}
        self.assertEqual(known, {"Zeta": "SimplifyJobs Internships", "Gamma": "your list"})
        page = (jm.boards_page_path()).read_text()
        self.assertIn("| Zeta | [Ashby](https://jobs.ashbyhq.com/zeta) |", page)
        self.assertIn("| Gamma | [Greenhouse](https://job-boards.greenhouse.io/gamma) |", page)

    def test_a_board_the_config_already_checks_is_not_checked_twice(self):
        self.mark_known(GH_ACME)
        self.pages[GH_ACME] = gh_board((1, "Software Engineer"))
        targets = [target(name="Acme", url=GH_ACME, mode="api"),
                   self.boards_entry(companies=[{"name": "Acme", "link": "https://job-boards.greenhouse.io/acme"}])]
        _, reported = self.run_monitor(targets, email=WORKING_EMAIL)
        self.assertEqual(reported, {"Acme": ["Software Engineer"]})
        self.assertEqual(len(self.board_requests()), 1)

    def test_company_boards_are_not_part_of_the_warnings(self):
        # Plenty of startups list nothing for a while; a board that can't be read is only counted in the log.
        self.start((1, "New Grad Software Engineer"))
        self.age_board_check()
        self.pages[GH_ACME] = gh_board()  # no openings now
        self.run_boards()
        self.assertNotIn(GH_ACME, self.saved_state().get(jm.HEALTH_KEY, {}))

    def test_a_board_whose_check_fails_is_read_again_next_time(self):
        self.start((1, "New Grad Software Engineer"))
        real_get = self.fake_get

        def get(url, *args, **kwargs):
            response = real_get(url, *args, **kwargs)
            if url == GH_ACME:
                response.headers["ETag"] = '"v2"'
            return response

        self.patch(jm.requests, "get", get)
        real_check = jm.check_target
        self.patch(jm, "check_target", lambda t, *a, **k: (_ for _ in ()).throw(RuntimeError("boom")) if t.get("_board") else real_check(t, *a, **k))
        self.age_board_check()
        with self.assertLogs(jm.log, "ERROR"):
            self.run_boards()
        self.assertNotIn(GH_ACME, self.boards_meta().get("etags", {}))

    def test_the_shipped_startup_list_is_all_board_links(self):
        config = jm.load_config(REPO_DIR / "config_all.json")
        [entry] = [t for t in config["targets"] if t.get("mode") == "boards"]
        self.assertGreater(len(entry["companies"]), 40)
        self.assertEqual([c["name"] for c in entry["companies"] if not jm.board_api_url(c["link"])], [])
        self.assertTrue(jm._phrase_pattern(entry["tech_titles"]).search("Member of Technical Staff, Inference"))

    def test_a_registry_entry_of_an_unknown_kind_is_left_out(self):
        # e.g. an iCIMS site saved by a newer version: never read as if it were something else.
        self.start((1, "New Grad Software Engineer"))
        meta = self.boards_meta()
        meta["known"]["https://careers-acme.icims.com/jobs"] = {
            "url": "https://careers-acme.icims.com/jobs", "name": "Acme iCIMS", "added": "2026-11-16", "via": "x"}
        jm.save_state({}, boards=meta)
        self.age_board_check()
        with self.assertLogs(jm.log, "INFO") as logs:
            self.run_boards()
        self.assertIn("1 read, 0 unchanged since the last check, 0 not found, 0 could not be read", "\n".join(logs.output))
        self.assertNotIn("Acme iCIMS", jm.boards_page_path().read_text())

    def test_lever_eu_boards_are_read_from_the_eu_api(self):
        self.pages["https://api.eu.lever.co/v0/postings/beta"] = json.dumps(
            [{"text": "Software Engineer", "hostedUrl": "https://jobs.eu.lever.co/beta/1", "categories": {"location": "Berlin"}}])
        jobs = jm.fetch_lever_api_jobs("https://api.eu.lever.co/v0/postings/beta")
        self.assertEqual([j["title"] for j in jobs], ["Software Engineer"])

    def test_posting_dates_are_read_from_each_board(self):
        self.assertEqual(jm._posted_at("2025-01-28T18:57:29-05:00"), "2025-01-28T23:57:29+00:00")
        self.assertEqual(jm._posted_at("2026-04-07T17:12:35.753Z"), "2026-04-07T17:12:35+00:00")
        self.assertEqual(jm._posted_at(1700000000000), "2023-11-14T22:13:20+00:00")
        for unknown in (None, "", "soon", True, [], 10 ** 20):
            self.assertIsNone(jm._posted_at(unknown), unknown)
        self.pages[GH_ACME] = json.dumps({"jobs": [{"id": 4000001, "title": "Software Engineer", "absolute_url": gh_link(1),
                                                    "first_published": "2026-11-10T09:00:00-05:00"}]})
        self.pages[ASHBY_ZETA] = json.dumps({"jobs": [{"title": "Software Engineer", "jobUrl": "https://jobs.ashbyhq.com/zeta/1",
                                                       "publishedAt": "2026-11-10T14:00:00.000+00:00"}]})
        self.pages["https://api.lever.co/v0/postings/beta"] = json.dumps(
            [{"text": "Software Engineer", "hostedUrl": "https://jobs.lever.co/beta/1", "createdAt": 1700000000000}])
        self.assertEqual([jm.fetch_greenhouse_api_jobs(GH_ACME)[0]["posted"], jm.fetch_ashby_api_jobs(ASHBY_ZETA)[0]["posted"],
                          jm.fetch_lever_api_jobs("https://api.lever.co/v0/postings/beta")[0]["posted"]],
                         ["2026-11-10T14:00:00+00:00", "2026-11-10T14:00:00+00:00", "2023-11-14T22:13:20+00:00"])

    def test_a_new_companys_first_check_emails_only_its_roles_from_the_last_days(self):
        catch_up = {"catch_up_days": 3}
        self.pages[GH_ACME] = gh_dated(
            (1, "New Grad Software Engineer", 1),           # the new-grad list has it already
            (2, "Software Engineer", 1),
            (3, "ML Engineer", 10),                          # too old
            (4, "Software Engineer, Infrastructure", None),  # no date: can't tell
            (5, "Senior Software Engineer", 1),              # your role filter
            (6, "Software Engineer", 2, "London, UK"))       # outside the US
        self.run_boards(catch_up)               # the feed's first check
        _, reported = self.run_boards(catch_up)  # finds Acme's board: its first check
        self.assertEqual(reported, {"Acme": ["Software Engineer"]})
        self.assertTrue(self.boards_meta()["known"][GH_ACME]["caught_up"])
        self.assertEqual(len(self.saved_state()[GH_ACME]), 6)  # all recorded
        # Only once; after that, new postings as usual (and the board is read hourly again).
        before = len(self.board_requests())
        self.assertEqual((self.run_boards(catch_up)[1], len(self.board_requests())), ({}, before))
        self.age_board_check()
        _, reported = self.run_boards(catch_up)
        self.assertEqual(reported, {})
        self.age_board_check()
        self.pages[GH_ACME] = gh_dated((1, "New Grad Software Engineer", 1), (2, "Software Engineer", 1), (7, "ML Engineer", 30))
        _, reported = self.run_boards(catch_up)
        self.assertEqual(reported, {"Acme": ["ML Engineer"]})

    def test_boards_first_checked_before_catching_up_existed_are_caught_up_once(self):
        jobs = [(1, "New Grad Software Engineer", 1), (2, "Software Engineer", 1), (3, "ML Engineer", 2),
                (7, "Software Engineer II", 1), (8, "Software Engineer, Payments", 9)]
        sent = []
        real_get = self.fake_get

        def get(url, *args, **kwargs):
            if url == GH_ACME:
                body = gh_dated(*jobs)
                etag = '"%s"' % hashlib.sha256(json.dumps(jobs).encode()).hexdigest()[:8]
                sent.append((kwargs.get("headers") or {}).get("If-None-Match"))
                if sent[-1] == etag:
                    return FakeResponse("", status_code=304)
                response = FakeResponse(body)
                response.headers["ETag"] = etag
                return response
            return real_get(url, *args, **kwargs)

        self.patch(jm.requests, "get", get)
        self.run_boards()
        self.assertEqual(self.run_boards()[1], {})  # Acme's board: a silent first check
        self.assertNotIn("caught_up", self.boards_meta()["known"][GH_ACME])
        # Meanwhile the new-grad list emails one of its recent roles, and the board a new one.
        self.pages[SIMPLIFY_URL] = json.dumps([simplify_row("Acme", "New Grad Software Engineer", url=gh_link(1)),
                                               simplify_row("Acme", "Software Engineer II", url=gh_link(7))])
        self.assertEqual(self.run_boards()[1], {"Acme (via SimplifyJobs New Grad)": ["Software Engineer II"]})
        self.age_board_check()
        jobs.append((9, "Software Engineer, Infrastructure", 0))
        self.assertEqual(self.run_boards()[1], {"Acme": ["Software Engineer, Infrastructure"]})
        # Catching up is turned on: the board is read in full right away (not due yet, and
        # without its ETag, which would answer "unchanged"), and the recent roles not emailed
        # yet are emailed once.
        _, reported = self.run_boards({"catch_up_days": 3})
        self.assertEqual(sorted(reported["Acme"]), ["ML Engineer", "Software Engineer"])
        self.assertEqual(sent[-1], None)
        self.assertTrue(self.boards_meta()["known"][GH_ACME]["caught_up"])
        self.age_board_check()
        _, reported = self.run_boards({"catch_up_days": 3})
        self.assertEqual(reported, {})
        self.assertIsNotNone(sent[-1])  # back to asking "anything changed?"

    def test_a_bad_link_in_a_company_list_does_not_stop_the_run(self):
        self.pages[INTERNSHIPS_URL] = json.dumps([
            {"url": "https://[jobs.ashbyhq.com/acme", "company_name": "Broken"},
            {"url": "https://jobs.lever.co／acme/1", "company_name": "Broken too"},
            {"url": "https://jobs.ashbyhq.com/zeta/0e7ecf76-1111-4a2b-9c3d-000000000001", "company_name": "Zeta"}])
        self.pages[ASHBY_ZETA] = json.dumps({"jobs": []})
        self.mark_known(GH_ACME)
        self.pages[GH_ACME] = gh_board((1, "Software Engineer"))
        targets = [target(name="Acme", url=GH_ACME, mode="api"),
                   self.boards_entry(find_companies_in=[{"name": "SimplifyJobs Internships", "url": INTERNSHIPS_URL}])]
        _, reported = self.run_monitor(targets, email=WORKING_EMAIL)
        self.assertEqual(reported, {"Acme": ["Software Engineer"]})
        self.assertEqual([i["name"] for i in self.boards_meta()["known"].values()], ["Zeta"])
        # Whatever else goes wrong with the boards, everything else is still checked and emailed.
        self.patch(jm, "plan_boards", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        self.pages[GH_ACME] = gh_board((1, "Software Engineer"), (2, "ML Engineer"))
        with self.assertLogs(jm.log, "ERROR"):
            _, reported = self.run_monitor(targets, email=WORKING_EMAIL)
        self.assertEqual(reported, {"Acme": ["ML Engineer"]})

    def test_a_job_on_two_of_a_companys_boards_is_emailed_once(self):
        campus = "https://boards-api.greenhouse.io/v1/boards/acmecampus/jobs"

        def board(slug, *jobs):  # (posting id, title, Greenhouse job id)
            return json.dumps({"jobs": [{"id": pid, "title": title, "internal_job_id": job, "location": {"name": "New York, NY"},
                                         "absolute_url": f"https://job-boards.greenhouse.io/{slug}/jobs/{pid}"}
                                        for pid, title, job in jobs]})

        acme_jobs, campus_jobs = [(5000001, "Software Engineer", 900)], [(5000002, "Software Engineer", 900)]

        def post(acme=(), on_campus=()):
            acme_jobs.extend(acme)
            campus_jobs.extend(on_campus)
            self.pages[GH_ACME], self.pages[campus] = board("acme", *acme_jobs), board("acmecampus", *campus_jobs)
            if jm.STATE_PATH.exists():
                self.age_board_check()
            _, reported = self.run_monitor([self.boards_entry(companies=[
                {"name": "Acme", "link": "https://job-boards.greenhouse.io/acme"},
                {"name": "Acme Campus", "link": "https://job-boards.greenhouse.io/acmecampus"}])], email=WORKING_EMAIL)
            return sorted(t for titles in reported.values() for t in titles)

        self.assertEqual(post(), [])  # both boards' first checks
        self.assertIn("gh_job", self.saved_state()[campus][0])
        # The same job on both boards in one run: once. Two jobs that only share a title: both.
        self.assertEqual(post(acme=[(5000003, "New Grad Software Engineer", 901), (5000005, "ML Engineer", 903)],
                              on_campus=[(5000004, "New Grad Software Engineer", 901), (5000006, "ML Engineer", 904)]),
                         ["ML Engineer", "ML Engineer", "New Grad Software Engineer"])
        # On the second board a run later: not again.
        self.assertEqual(post(acme=[(5000007, "Software Engineer, Payments", 905)]), ["Software Engineer, Payments"])
        self.assertEqual(post(on_campus=[(5000008, "Software Engineer, Payments", 905)]), [])
        # A job you were never shown (recorded at a first check) still counts as new on another board.
        self.assertEqual(post(on_campus=[(5000009, "Software Engineer", 900)]), ["Software Engineer"])

    def test_the_shipped_skip_words_drop_gig_work_but_not_engineering_titles(self):
        config = jm.load_config(REPO_DIR / "config_all.json")
        [entry] = [t for t in config["targets"] if t.get("mode") == "boards"]
        tech, skip = jm._phrase_pattern(entry["tech_titles"]), jm._phrase_pattern(entry["skip_titles"])

        def kept(title):
            core = jm._title_core(title)
            return bool(tech.search(core)) and not skip.search(core)

        for title in ("Software Engineer, Annotation Systems", "Labeling Backend Software Engineer",
                      "ML Engineer, Data Labeling Platform", "AI Engineer, Evaluation & Annotation Tools"):
            self.assertTrue(kept(title), title)
        for title in ("AI Trainer (Contractor), Voice and Speech Evaluation", "Part-Time AI Research Data Annotator",
                      "Software Engineer - AI Tutor", "Python Expert - AI Training", "Freelance AI Rater"):
            self.assertFalse(kept(title), title)

    def test_a_paused_boards_entry_reads_no_board_but_still_sends_what_is_waiting(self):
        self.start((1, "New Grad Software Engineer"))
        jm.save_state({}, last_email=(datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds"))
        self.age_board_check()
        self.pages[GH_ACME] = gh_board((1, "New Grad Software Engineer"), (2, "Software Engineer"))
        self.run_boards(email_every_hours=2)  # found, waiting for the next email
        self.assertEqual(self.emails_attempted, 0)
        # Paused: no board is read, not even a new one, but the waiting posting goes out (and the
        # new-grad list is checked as usual).
        self.pages[SIMPLIFY_URL] = json.dumps([
            simplify_row("Acme", "New Grad Software Engineer", url=gh_link(1)),
            simplify_row("Zeta", "Software Engineer", url="https://jobs.ashbyhq.com/zeta/0e7ecf76-1111-4a2b-9c3d-000000000001")])
        self.pages[ASHBY_ZETA] = json.dumps({"jobs": []})
        jm.save_state({}, last_email=(datetime.now() - timedelta(hours=3)).isoformat(timespec="seconds"))
        self.age_board_check()
        before = len(self.requests_made)
        _, reported = self.run_monitor(self.targets(enabled=False), email=WORKING_EMAIL, role_filter=REPO_ROLE_FILTER,
                                       us_only=True, email_every_hours=2)
        self.assertEqual(reported, {"Acme": ["Software Engineer"], "Zeta (via SimplifyJobs New Grad)": ["Software Engineer"]})
        self.run_monitor(self.targets(enabled=False), email=WORKING_EMAIL, role_filter=REPO_ROLE_FILTER, us_only=True)
        self.assertIn("Zeta", [i["name"] for i in self.boards_meta()["known"].values()])  # the registry still grows
        self.assertEqual([u for u, _ in self.requests_made[before:] if u in (GH_ACME, ASHBY_ZETA)], [])
        self.assertNotIn(ASHBY_ZETA, self.saved_state())


class HealthTests(MonitorTestCase):
    """Telling you when one of your entries stops working (state["_health"], the emails' warning)."""

    OTHER = "https://other.example/careers"

    def setUp(self):
        super().setUp()
        self.pages[URL] = page(*jobs_named("Data Analyst"))
        self.acme_jobs = ["Data Analyst"]

    def run_both(self, *extra_targets, other=True):
        return self.run_monitor([target(), *([target(name="Beta", url=self.OTHER)] if other else []), *extra_targets],
                                email=WORKING_EMAIL)

    def new_acme_posting(self, title):
        self.acme_jobs.append(title)
        self.pages[URL] = page(*jobs_named(*self.acme_jobs))

    def health(self):
        return self.saved_state().get(jm.HEALTH_KEY, {})

    def age(self, url, hours):
        state = self.saved_state()
        state[jm.HEALTH_KEY][url]["since"] = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
        jm.save_json(jm.STATE_PATH, state)

    def refuse_other(self, status):
        real_get = self.fake_get
        self.patch(jm.requests, "get", lambda url, *a, **k: FakeResponse("", status_code=status) if url == self.OTHER
                   else real_get(url, *a, **k))

    def test_an_entry_that_keeps_failing_is_reported_after_six_hours_until_it_works(self):
        self.refuse_other(429)
        self.run_both()
        record = self.health()[self.OTHER]
        self.assertEqual((record["name"], record["kind"]), ("Beta", "failed"))
        self.assertIn("429", record["why"])
        self.new_acme_posting("ML Engineer")
        _, reported = self.run_both()
        self.assertEqual((reported, self.problems), ({"Acme": ["ML Engineer"]}, []))  # not yet: could be a blip
        self.assertEqual(self.health()[self.OTHER]["since"], record["since"])  # still failing since the first time
        self.age(self.OTHER, 7)
        self.new_acme_posting("Software Engineer")
        _, reported = self.run_both()
        self.assertEqual([p["name"] for p in self.problems], ["Beta"])
        text = jm.format_plain_report({"Acme": []}, problems=self.problems)
        since = (datetime.now() - timedelta(hours=7)).strftime("%b %d")
        self.assertIn(f"Beta: the site is refusing us (429: too many requests), since {since}", text)
        self.assertIn("aren't working", jm.format_html_report({}, problems=self.problems))
        # It works again: no more warning, and the record is gone.
        self.patch(jm.requests, "get", self.fake_get)
        self.pages[self.OTHER] = page(*jobs_named("Quant Researcher"))
        self.new_acme_posting("Backend Engineer")
        self.run_both()
        self.assertEqual((self.problems, self.health()), ([], {}))

    def test_an_entry_that_finds_nothing_is_reported_after_two_days(self):
        self.pages[self.OTHER] = page()
        self.run_both()
        self.assertEqual(self.health()[self.OTHER]["kind"], "empty")
        self.age(self.OTHER, 47)
        self.new_acme_posting("ML Engineer")
        self.run_both()
        self.assertEqual(self.problems, [])
        self.age(self.OTHER, 49)
        self.new_acme_posting("Software Engineer")
        self.run_both()
        self.assertEqual([(p["name"], p["why"]) for p in self.problems],
                         [("Beta", "it found no postings (the page may have changed)")])
        # A different problem starts the clock again.
        self.refuse_other(404)
        self.run_both()
        record = self.health()[self.OTHER]
        self.assertEqual((record["kind"], record["why"]), ("failed", "the page is gone (404)"))
        self.assertLess(jm._hours_since(record["since"]), 1)

    def test_a_job_board_api_with_no_openings_is_not_reported(self):
        # An empty list from Greenhouse/Ashby/Lever is a real answer (OpenSea: not hiring), unlike an empty page.
        board = "https://api.ashbyhq.com/posting-api/job-board/opensea"
        self.pages[board] = json.dumps({"jobs": []})
        self.run_both(target(name="OpenSea", url=board, mode="api"))
        self.assertNotIn(board, self.health())
        self.pages[board] = "{}"  # not a job list: that is a problem
        self.run_both(target(name="OpenSea", url=board, mode="api"))
        self.assertEqual(self.health()[board]["kind"], "failed")

    def test_reasons_in_plain_words(self):
        cases = {"429 Client Error: Too Many Requests for url": "429", "403 Client Error: Forbidden": "403",
                 "Failed to read x: 404 Client Error: Not Found for u": "404", "Read timed out. (read timeout=30)": "in time",
                 "502 Server Error: Bad Gateway": "5xx", "something odd": "couldn't be read"}
        for message, expected in cases.items():
            self.assertIn(expected, jm._problem_reason([message]), message)
        # A fast path that fails while a fallback works is fine.
        self.assertIsNone(jm._health_record(None, "Microsoft", 20, ["[ms-api] 429 Too Many Requests"], "cfg"))

    def test_removed_and_paused_entries_are_not_reported(self):
        self.refuse_other(429)
        self.run_both()
        self.assertIn(self.OTHER, self.health())
        self.run_both(other=False)  # Beta removed from the config
        self.assertEqual(self.health(), {})
        self.run_both(target(name="Gamma", url="https://gamma.example/careers", enabled=False))
        self.assertNotIn("https://gamma.example/careers", self.health())
        # Failing long enough to be reported, then paused (what the README says to do): the warning goes.
        self.run_both()
        self.age(self.OTHER, 7)
        self.new_acme_posting("ML Engineer")
        self.run_both()
        self.assertEqual([p["name"] for p in self.problems], ["Beta"])
        self.new_acme_posting("Software Engineer")
        self.run_monitor([target(), target(name="Beta", url=self.OTHER, enabled=False)], email=WORKING_EMAIL)
        self.assertEqual((self.problems, self.health()), ([], {}))

    def test_another_configs_records_are_kept(self):
        jm.save_state({}, health={"https://elsewhere.example": {"name": "X", "kind": "failed", "why": "it couldn't be read",
                                                                 "since": "2026-01-01T00:00:00", "config": "another"}})
        self.refuse_other(429)
        self.run_both()
        self.assertEqual(sorted(self.health()), ["https://elsewhere.example", self.OTHER])
        self.assertEqual(jm.broken_checks(self.health(), {URL, self.OTHER}), [])  # not this config's, and Beta is new


def wk_link(code, account="bask-health-1"):
    """A list's link to a Workable posting."""
    return f"https://apply.workable.com/{account}/j/{code}/apply"


def wk_file(*rows):
    """Workable's job file; each row is (code, title, company, country, posted days ago[, city])."""
    now = datetime.now(timezone.utc)
    jobs = "".join(
        f"<job><title><![CDATA[{title}]]></title>"
        f"<date><![CDATA[{(now - timedelta(days=days)).strftime('%a, %d %b %Y %H:%M:%S UTC')}]]></date>"
        f"<referencenumber><![CDATA[{code}]]></referencenumber><url><![CDATA[https://apply.workable.com/j/{code}]]></url>"
        f"<company><![CDATA[{company}]]></company><city><![CDATA[{rest[0] if rest else ''}]]></city><state><![CDATA[]]></state>"
        f"<country><![CDATA[{country}]]></country><website><![CDATA[https://{company.lower().replace(' ', '')}.example]]></website>"
        f"<description><![CDATA[<p>About the role</p>]]></description></job>"
        for code, title, company, country, days, *rest in rows)
    return f'<?xml version="1.0" encoding="UTF-8"?><source><publisher>Workable</publisher>{jobs}</source>'


class WorkableTests(MonitorTestCase):
    """Workable companies, read from Workable's hourly file of all its customers' jobs."""

    BASK = "https://apply.workable.com/bask-health-1/"
    BASK_KEY = "https://apply.workable.com/bask-health-1"

    def setUp(self):
        super().setUp()
        self.file = wk_file()
        self.file_status = 200
        self.file_requests = []
        self.accounts = {}   # account: (status, answer) for Workable's account lookups (none: "no such account")
        self.lookups = []    # (account, User-Agent) asked about
        self.pages[SIMPLIFY_URL] = json.dumps([simplify_row("Bask Health", "Backend Engineer, New Grad", url=wk_link("472CF47F60"))])
        self.patch(jm.workable, "LOOKUP_DELAY_SECONDS", 0)
        real_get = self.fake_get

        def get(url, *args, **kwargs):
            if url.startswith(jm.workable.ACCOUNT_API.format("")):
                account = url[len(jm.workable.ACCOUNT_API.format("")):]
                self.lookups.append((account, (kwargs.get("headers") or {}).get("User-Agent")))
                status, answer = self.accounts.get(account, (404, ""))
                return FakeResponse(answer if isinstance(answer, str) else json.dumps(answer), status_code=status)
            if url != jm.workable.FEED_URL:
                return real_get(url, *args, **kwargs)
            headers = kwargs.get("headers") or {}
            self.file_requests.append(headers.get("If-Modified-Since"))
            if self.file_status != 200:
                return FakeResponse(self.file, status_code=self.file_status)  # (an error page could even look like the file)
            if headers.get("If-Modified-Since") == f"version {len(self.file)}":
                return FakeResponse("", status_code=304)
            response = FakeResponse(self.file, status_code=self.file_status)
            response.headers["Last-Modified"] = f"version {len(self.file)}"
            return response

        self.patch(jm.requests, "get", get)

    def run_wk(self, **entry_extra):
        entry = {"name": "Company job boards", "mode": "boards", "every_hours": 1, "catch_up_days": 3,
                 "platforms": ["greenhouse", "ashby", "lever", "workable"],
                 "tech_titles": ["software", "ml", "infrastructure", "new grad"], **entry_extra}
        targets = [target(name="SimplifyJobs New Grad", url=SIMPLIFY_URL, mode="feed", format="simplify",
                          categories=["Software"], max_age_days=7), entry]
        return self.run_monitor(targets, email=WORKING_EMAIL, role_filter=REPO_ROLE_FILTER, us_only=True)[1]

    def meta(self):
        return self.saved_state().get(jm.BOARDS_KEY, {})

    def next_hour(self):
        meta = self.meta()
        meta["checked_at"] = (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds")
        jm.save_state({}, boards=meta)

    def test_workable_links_are_recognised(self):
        for link in (wk_link("472CF47F60"), "https://apply.workable.com/bask-health-1/j/472CF47F60/",
                     "https://apply.workable.com/Bask-Health-1/j/472CF47F60/apply#jobs"):
            self.assertEqual(jm.board_api_url(link), ("workable", self.BASK), link)
            self.assertEqual(jm.workable.job_code(link), "472CF47F60", link)
        for link in ("https://apply.workable.com/j/472CF47F60", "https://apply.workable.com/api/v1/widget/accounts/x"):
            self.assertIsNone(jm.board_api_url(link), link)
        # The list's link and the file's link to one posting are recognised as the same posting.
        self.assertTrue(jm.job_keys(wk_link("472CF47F60")) & jm.job_keys("https://apply.workable.com/j/472CF47F60"))
        self.assertFalse(jm.job_keys(wk_link("472CF47F60")) & jm.job_keys("https://apply.workable.com/j/472CF47F61"))

    def test_a_workable_company_is_matched_through_a_job_code_and_checked_like_a_board(self):
        self.file = wk_file(
            ("472CF47F60", "Backend Engineer, New Grad", "Bask Health", "US", 1),  # the list has it already
            ("A00000000A", "Software Engineer", "Bask Health", "US", 1, "New York"),
            ("A00000000A", "Software Engineer", "Bask Health", "US", 1, "San Francisco"),
            ("B00000000B", "ML Engineer", "Bask Health", "US", 10),             # too old to catch up
            ("C00000000C", "Software Engineer", "Bask Health", "GB", 1),        # outside the US
            ("D00000000D", "Account Executive", "Bask Health", "US", 1),        # not a tech role
            ("E00000000E", "Software Engineer", "Other Co", "US", 1))           # another company
        self.assertEqual(self.run_wk(), {})   # the list's first check
        self.next_hour()
        self.assertEqual(self.run_wk(), {"Bask Health": ["Software Engineer"]})  # the account's first check
        info = self.meta()["known"][self.BASK_KEY]
        self.assertEqual((info["tenant"], info["codes"], bool(info["caught_up"])),
                         (["Bask Health", "https://baskhealth.example"], ["472CF47F60"], True))
        self.assertIn("https://apply.workable.com/j/A00000000A", [r["url"] for r in self.saved_state()[self.BASK]])
        self.assertIn("1 Workable, 0 SmartRecruiters.", jm.boards_page_path().read_text())
        # An hour later the file hasn't changed: asked "anything new since?", answered "no".
        self.next_hour()
        self.assertEqual(self.run_wk(), {})
        self.assertEqual(self.file_requests[-1], f"version {len(self.file)}")
        # Then a new posting appears in it.
        self.file = self.file.replace("</source>", wk_file(("F00000000F", "Software Engineer, Infrastructure", "Bask Health", "US", 0))
                                      .split("<publisher>Workable</publisher>")[1])
        self.next_hour()
        self.assertEqual(self.run_wk(), {"Bask Health": ["Software Engineer, Infrastructure"]})

    def test_the_file_is_read_only_when_the_boards_are_due(self):
        self.file = wk_file(("472CF47F60", "Backend Engineer, New Grad", "Bask Health", "US", 1))
        self.run_wk()
        self.next_hour()
        self.run_wk()
        before = len(self.file_requests)
        # Within the hour, even a newly found account waits for the next check (the file changes hourly).
        self.pages[SIMPLIFY_URL] = json.dumps([simplify_row("Bask Health", "Backend Engineer, New Grad", url=wk_link("472CF47F60")),
                                               simplify_row("Raydar", "Software Engineer", url=wk_link("ED5C9139BB", "raydar"))])
        self.run_wk()
        self.run_wk()
        self.assertEqual(len(self.file_requests), before)

    def test_workable_and_other_boards_are_checked_in_the_same_run(self):
        self.pages[SIMPLIFY_URL] = json.dumps([simplify_row("Bask Health", "Backend Engineer, New Grad", url=wk_link("472CF47F60")),
                                               simplify_row("Acme", "New Grad Software Engineer", url=gh_link(1))])
        self.pages[GH_ACME] = gh_board((1, "New Grad Software Engineer"))
        self.file = wk_file(("472CF47F60", "Backend Engineer, New Grad", "Bask Health", "US", 1))
        self.run_wk()
        self.next_hour()
        self.run_wk()  # both first checks
        self.pages[GH_ACME] = gh_board((1, "New Grad Software Engineer"), (2, "Software Engineer"))
        self.file = wk_file(("472CF47F60", "Backend Engineer, New Grad", "Bask Health", "US", 1),
                            ("A00000000A", "ML Engineer", "Bask Health", "US", 0))
        self.next_hour()
        self.assertEqual(self.run_wk(), {"Acme": ["Software Engineer"], "Bask Health": ["ML Engineer"]})

    def test_an_account_not_matched_yet_waits_and_is_caught_up_once_matched(self):
        # The list's posting has closed, and the file names the company differently (no name match).
        self.file = wk_file(("A00000000A", "Software Engineer", "Bask Telehealth", "US", 1))
        self.run_wk()
        self.next_hour()
        self.assertEqual(self.run_wk(), {})
        self.assertNotIn(self.BASK, self.saved_state())       # not checked (an empty first check would flood later)
        self.assertNotIn("tenant", self.meta()["known"][self.BASK_KEY])
        # The list links a current posting of the account: matched, first check with the 3-day catch-up.
        self.pages[SIMPLIFY_URL] = json.dumps([simplify_row("Bask Health", "Backend Engineer, New Grad", url=wk_link("472CF47F60")),
                                               simplify_row("Bask Health", "ML Engineer", url=wk_link("B00000000B"))])
        self.run_wk()  # (the list's new row; boards not due)
        self.file = wk_file(("A00000000A", "Software Engineer", "Bask Telehealth", "US", 1),
                            ("B00000000B", "ML Engineer", "Bask Telehealth", "US", 0))
        self.next_hour()
        self.assertEqual(self.run_wk(), {"Bask Health": ["Software Engineer"]})  # ML Engineer came through the list

    def test_a_refused_or_cut_off_file_changes_nothing(self):
        good = wk_file(*[(f"A0000000{i:02d}", "Software Engineer", "Bask Health", "US", 5) for i in range(10)],
                       ("472CF47F60", "Backend Engineer, New Grad", "Bask Health", "US", 1))
        self.file = good
        self.run_wk()
        self.next_hour()
        self.run_wk()
        saved = self.saved_state()[self.BASK]
        for broken, status in ((good, 429), (good[: len(good) // 2] + "  ", 200),
                               (wk_file(("472CF47F60", "Backend Engineer, New Grad", "Bask Health", "US", 1)), 200)):
            self.file, self.file_status = broken, status
            self.next_hour()
            with self.assertLogs(jm.log, "WARNING"):
                self.assertEqual(self.run_wk(), {})
            self.assertEqual(self.saved_state()[self.BASK], saved)
            self.assertEqual(self.meta()["workable"]["rows"], 11)

    def matched(self, *rows):
        """Bask Health registered, matched and caught up, its file listing these rows."""
        self.file = wk_file(("472CF47F60", "Backend Engineer, New Grad", "Bask Health", "US", 1), *rows)
        self.run_wk()
        self.next_hour()
        self.run_wk()
        self.assertTrue(self.meta()["known"][self.BASK_KEY].get("caught_up"))

    def test_a_company_that_renames_itself_in_workable_stays_checked(self):
        # (ColdQuanta became Infleqtion: same account and job codes, new name and website in the file)
        # (A is recent: emailed at the first check, and not again after the rename.)
        self.matched(("A00000000A", "Software Engineer", "Bask Health", "US", 1))
        self.file = (wk_file(("A00000000A", "Software Engineer", "Bask Health", "US", 1),
                             ("F00000000F", "Software Engineer, Infrastructure", "Bask Health", "US", 0))
                     .replace("Bask Health", "Bask").replace("https://baskhealth.example", "https://bask.example"))
        self.next_hour()
        self.assertEqual(self.run_wk(), {"Bask Health": ["Software Engineer, Infrastructure"]})  # (the list's job closed meanwhile)
        self.assertEqual(self.meta()["known"][self.BASK_KEY]["tenant"], ["Bask", "https://bask.example"])

    def test_a_lastingly_smaller_file_becomes_the_new_normal(self):
        self.matched(*[(f"A0000000{i:02d}", "Data Analyst", "Bask Health", "US", 5) for i in range(10)])
        self.file = wk_file(("472CF47F60", "Backend Engineer, New Grad", "Bask Health", "US", 1),
                            ("F00000000F", "Software Engineer", "Bask Health", "US", 0))
        self.next_hour()
        with self.assertLogs(jm.log, "WARNING"):
            self.assertEqual(self.run_wk(), {})       # a sudden drop: skipped for now
        meta = self.meta()
        meta["workable"]["small_since"] = (datetime.now() - timedelta(hours=7)).isoformat(timespec="seconds")
        meta["checked_at"] = (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds")
        jm.save_state({}, boards=meta)
        self.assertEqual(self.run_wk(), {"Bask Health": ["Software Engineer"]})  # still that small hours later: accepted
        self.assertEqual((self.meta()["workable"]["rows"], "small_since" in self.meta()["workable"]), (2, False))

    def test_the_internship_lists_newest_job_codes_are_kept(self):
        # listings.json adds new rows at the end
        self.pages[INTERNSHIPS_URL] = json.dumps([{"url": wk_link(f"{i:010X}", "altom-transport"), "company_name": "Altom"}
                                                  for i in range(30)])
        self.run_wk(find_companies_in=[{"name": "SimplifyJobs Internships", "url": INTERNSHIPS_URL}])
        codes = self.meta()["known"]["https://apply.workable.com/altom-transport"]["codes"]
        self.assertEqual((len(codes), codes[0]), (jm.workable.CODES_KEPT, f"{29:010X}"))

    def test_any_workable_link_in_the_lists_helps_match_an_account(self):
        # Bask's recent list row points to a closed job; an older row (outside the list's 7 days) to an open one.
        self.pages[SIMPLIFY_URL] = json.dumps([simplify_row("Bask Health", "Backend Engineer, New Grad", url=wk_link("472CF47F60")),
                                               simplify_row("Bask Health", "ML Engineer", days_ago=40, url=wk_link("B00000000B"))])
        self.file = wk_file(("B00000000B", "ML Engineer", "Bask Health", "US", 40), ("A00000000A", "Software Engineer", "Bask Health", "US", 1))
        self.run_wk()
        self.run_wk()  # (the account is added from the list's saved rows; boards not due)
        self.assertEqual(set(self.meta()["known"][self.BASK_KEY]["codes"]), {"472CF47F60", "B00000000B"})
        self.next_hour()
        self.assertEqual(self.run_wk(), {"Bask Health": ["Software Engineer"]})

    def test_an_account_matched_late_catches_up_from_the_day_it_was_added(self):
        self.file = wk_file(("A00000000A", "Software Engineer", "Bask Telehealth", "US", 1))  # the list's job is closed
        self.run_wk()
        self.next_hour()
        self.run_wk()
        meta = self.meta()
        meta["known"][self.BASK_KEY]["added"] = (datetime.now() - timedelta(days=10)).date().isoformat()
        jm.save_state({}, boards=meta)
        self.pages[SIMPLIFY_URL] = json.dumps([simplify_row("Bask Health", "Backend Engineer, New Grad", url=wk_link("472CF47F60")),
                                               simplify_row("Bask Health", "Data Analyst", url=wk_link("C00000000C"))])
        self.run_wk()
        self.file = wk_file(("A00000000A", "Software Engineer", "Bask Telehealth", "US", 1),
                            ("D00000000D", "ML Engineer", "Bask Telehealth", "US", 8),            # posted while it waited
                            ("E00000000E", "Software Engineer, Payments", "Bask Telehealth", "US", 12),  # before it was added
                            ("C00000000C", "Data Analyst", "Bask Telehealth", "US", 0))
        self.next_hour()
        self.assertEqual(sorted(self.run_wk()["Bask Health"]), ["ML Engineer", "Software Engineer"])

    def test_boards_md_marks_accounts_not_matched_yet(self):
        self.file = wk_file(("A00000000A", "Software Engineer", "Bask Telehealth", "US", 1))
        self.run_wk()
        self.next_hour()
        self.run_wk()
        page = jm.boards_page_path().read_text()
        self.assertIn("1 Workable (1 not matched in Workable's file yet, so not checked yet)", page)
        self.assertIn("(not matched in Workable's file yet: no current job of it found there)", page)

    def test_an_account_is_matched_through_the_jobs_workable_lists_for_it(self):
        # The list's job has closed and the file names the company differently; Workable, asked, lists its jobs.
        self.file = wk_file(("A00000000A", "Software Engineer", "Bask Telehealth", "US", 1),
                            ("B00000000B", "ML Engineer", "Bask Telehealth", "US", 9))  # too old to catch up
        self.accounts["bask-health-1"] = (200, {"name": "Bask Telehealth", "jobs": [{"title": "Software Engineer", "shortcode": "a00000000a"}]})
        self.run_wk()
        self.next_hour()
        self.assertEqual(self.run_wk(), {"Bask Health": ["Software Engineer"]})
        info = self.meta()["known"][self.BASK_KEY]
        self.assertEqual((info["tenant"], info["workable_name"], "matched_by" in info),
                         (["Bask Telehealth", "https://basktelehealth.example"], "Bask Telehealth", False))
        self.assertEqual(self.lookups, [("bask-health-1", jm.MONITOR_USER_AGENT)])  # under the monitor's own name
        self.next_hour()
        self.run_wk()
        self.assertEqual(len(self.lookups), 1)  # matched by a code: not asked again

    def test_lookups_are_few_paced_daily_and_stop_at_a_refusal(self):
        accounts = ["acct-a", "acct-b", "acct-c", "acct-d", "acct-e"]
        self.pages[SIMPLIFY_URL] = json.dumps([simplify_row(f"Company {a}", "Software Engineer", url=wk_link(f"{i:010X}", a))
                                               for i, a in enumerate(accounts)])
        self.patch(jm.workable, "LOOKUPS_PER_RUN", 3)
        self.patch(jm.workable, "LOOKUP_DELAY_SECONDS", 2.5)
        sleeps = []
        self.patch(jm.workable.time, "sleep", sleeps.append)
        self.accounts.update({"acct-a": (200, {"name": "A", "jobs": []}), "acct-b": (429, "Too Many Requests")})
        self.run_wk()
        self.next_hour()
        with self.assertLogs(jm.log, "WARNING") as logs:
            self.run_wk()
        self.assertEqual([a for a, _ in self.lookups], ["acct-a", "acct-b"])  # stopped at the refusal
        self.assertEqual(sleeps, [2.5])
        self.assertIn("Workable answered 429", "\n".join(logs.output))
        known = self.meta()["known"]
        self.assertEqual(known["https://apply.workable.com/acct-a"]["workable_jobs"], 0)
        self.assertIn("looked_up", known["https://apply.workable.com/acct-b"])  # asked: not again today, whatever the answer
        self.next_hour()
        self.run_wk()
        # The ones never asked; at most 3; acct-a and acct-b not again the same day.
        self.assertEqual([a for a, _ in self.lookups][2:], ["acct-c", "acct-d", "acct-e"])
        self.assertEqual(sleeps, [2.5, 2.5, 2.5])
        self.next_hour()
        self.run_wk()
        self.assertEqual(len(self.lookups), 5)
        # A day later, the ones still not matched are asked again (longest waiting first), 3 at most.
        self.accounts["acct-b"] = (200, {"name": "B", "jobs": []})
        meta = self.meta()
        for info in meta["known"].values():
            info["looked_up"] = (datetime.now() - timedelta(hours=25)).isoformat(timespec="seconds")
        meta["checked_at"] = (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds")
        jm.save_state({}, boards=meta)
        self.run_wk()
        self.assertEqual(len(self.lookups), 8)

    def test_no_answer_stops_the_lookups(self):
        self.pages[SIMPLIFY_URL] = json.dumps([simplify_row(f"Company {a}", "Software Engineer", url=wk_link(f"{i:010X}", a))
                                               for i, a in enumerate(["acct-a", "acct-b"])])
        for status in (503, None):  # Workable failing, or not answering in time
            with self.subTest(status=status):
                self.setUp()
                self.pages[SIMPLIFY_URL] = json.dumps([simplify_row(f"Company {a}", "Software Engineer", url=wk_link(f"{i:010X}", a))
                                                       for i, a in enumerate(["acct-a", "acct-b"])])
                if status:
                    self.accounts["acct-a"] = (status, "Service Unavailable")
                else:
                    real_get = jm.requests.get
                    def get(url, *args, _real=real_get, **kwargs):
                        if url == jm.workable.ACCOUNT_API.format("acct-a"):
                            self.lookups.append(("acct-a", None))
                            raise jm.requests.Timeout("read timed out")
                        return _real(url, *args, **kwargs)
                    self.patch(jm.requests, "get", get)
                self.run_wk()
                self.next_hour()
                with self.assertLogs(jm.log, "WARNING"):
                    self.run_wk()
                self.assertEqual([a for a, _ in self.lookups], ["acct-a"])
                self.next_hour()
                self.run_wk()
                self.assertEqual([a for a, _ in self.lookups], ["acct-a", "acct-b"])  # the next one: none blocks the others

    def test_a_page_that_is_not_the_job_list_stops_the_lookups(self):
        self.pages[SIMPLIFY_URL] = json.dumps([simplify_row(f"Company {a}", "Software Engineer", url=wk_link(f"{i:010X}", a))
                                               for i, a in enumerate(["acct-a", "acct-b"])])
        self.accounts["acct-a"] = (200, "<html>Checking your browser...</html>")
        self.run_wk()
        self.next_hour()
        with self.assertLogs(jm.log, "WARNING"):
            self.run_wk()
        self.assertEqual([a for a, _ in self.lookups], ["acct-a"])
        self.assertNotIn("workable_name", self.meta()["known"]["https://apply.workable.com/acct-a"])

    def test_an_account_is_matched_by_name_when_one_company_in_the_file_has_it(self):
        # The list's job has closed and Workable refuses to be asked; one company in the file has the name.
        self.accounts["bask-health-1"] = (429, "Too Many Requests")
        rows = [("A00000000A", "Software Engineer", "Bask Health, Inc.", "US", 1),
                ("B00000000B", "ML Engineer", "Bask Health, Inc.", "US", 9),     # too old to catch up
                ("C00000000C", "Software Engineer", "Bask Clinics", "US", 1)]    # another company
        self.file = wk_file(*rows)
        self.run_wk()
        self.next_hour()
        with self.assertLogs(jm.log, "WARNING"):
            self.assertEqual(self.run_wk(), {"Bask Health": ["Software Engineer"]})
        info = self.meta()["known"][self.BASK_KEY]
        self.assertEqual((info["tenant"][0], info["matched_by"]), ("Bask Health, Inc.", "name"))
        self.assertIn("(matched by name in Workable's file)", jm.boards_page_path().read_text())
        self.file = wk_file(*rows, ("F00000000F", "Software Engineer, Infrastructure", "Bask Health, Inc.", "US", 0))
        self.next_hour()
        self.assertEqual(self.run_wk(), {"Bask Health": ["Software Engineer, Infrastructure"]})
        self.assertEqual(len(self.lookups), 1)  # (refused: not asked again the same day)

    def test_the_name_workable_gives_the_account_is_used_first(self):
        self.accounts["bask-health-1"] = (200, {"name": "Bask Telehealth", "jobs": []})
        self.file = wk_file(("A00000000A", "Software Engineer", "Bask Telehealth", "US", 1))
        self.run_wk()
        self.next_hour()
        self.assertEqual(self.run_wk(), {"Bask Health": ["Software Engineer"]})
        self.assertEqual(self.meta()["known"][self.BASK_KEY]["matched_by"], "name")

    def test_no_name_match_when_the_name_is_not_unique(self):
        # Two companies in the file with the name...
        self.file = wk_file(("A00000000A", "Software Engineer", "Bask Health", "US", 1),
                            ("B00000000B", "Software Engineer", "Bask Health LLC", "US", 1))
        self.run_wk()
        self.next_hour()
        self.assertEqual(self.run_wk(), {})
        self.assertNotIn("tenant", self.meta()["known"][self.BASK_KEY])
        # ...or two accounts with it.
        self.pages[SIMPLIFY_URL] = json.dumps([simplify_row("Bask Health", "Backend Engineer, New Grad", url=wk_link("472CF47F60")),
                                               simplify_row("Bask Health", "Software Engineer", url=wk_link("9999999999", "bask-health-2"))])
        self.file = wk_file(("A00000000A", "Software Engineer", "Bask Health", "US", 1))
        self.run_wk()
        self.next_hour()
        self.assertEqual(self.run_wk(), {})
        known = self.meta()["known"]
        self.assertNotIn("tenant", known[self.BASK_KEY])
        self.assertNotIn("tenant", known["https://apply.workable.com/bask-health-2"])

    def test_a_company_matched_by_a_code_is_not_given_to_another_account_by_name(self):
        self.pages[SIMPLIFY_URL] = json.dumps([simplify_row("Bask Health", "Backend Engineer, New Grad", url=wk_link("472CF47F60")),
                                               simplify_row("Bask Health", "Software Engineer", url=wk_link("9999999999", "bask-health-2"))])
        self.file = wk_file(("472CF47F60", "Backend Engineer, New Grad", "Bask Health", "US", 1),
                            ("A00000000A", "Software Engineer", "Bask Health", "US", 1))
        self.run_wk()
        self.next_hour()
        self.assertEqual(self.run_wk(), {"Bask Health": ["Software Engineer"]})  # once, through bask-health-1
        known = self.meta()["known"]
        self.assertEqual(known[self.BASK_KEY]["tenant"][0], "Bask Health")
        self.assertNotIn("tenant", known["https://apply.workable.com/bask-health-2"])

    def test_a_name_match_is_corrected_by_a_job_code(self):
        rows = [("A00000000A", "Data Analyst", "Bask Health", "US", 5),            # the same name, another company
                ("B00000000B", "Software Engineer", "Bask Clinics", "US", 1)]    # the account's company
        self.file = wk_file(*rows)
        self.run_wk()
        self.next_hour()
        self.run_wk()
        self.assertEqual(self.meta()["known"][self.BASK_KEY]["tenant"][0], "Bask Health")
        # A list links one of the account's jobs: remembered, though it's matched (by name)...
        self.pages[SIMPLIFY_URL] = json.dumps([simplify_row("Bask Health", "Backend Engineer, New Grad", url=wk_link("472CF47F60")),
                                               simplify_row("Bask Health", "Software Engineer", url=wk_link("B00000000B"))])
        self.run_wk()
        self.assertIn("B00000000B", self.meta()["known"][self.BASK_KEY]["codes"])
        # ...and at the next check it wins over the name (and the postings recorded from the wrong company).
        self.file = wk_file(*rows, ("C00000000C", "ML Engineer", "Bask Clinics", "US", 0))
        self.next_hour()
        self.assertEqual(self.run_wk(), {"Bask Health": ["ML Engineer"]})
        info = self.meta()["known"][self.BASK_KEY]
        self.assertEqual((info["tenant"][0], "matched_by" in info), ("Bask Clinics", False))

    def test_a_corrected_name_match_emails_only_recent_roles_and_stays_corrected(self):
        wrong = [("A00000000A", "Data Analyst", "Bask Health", "US", 5)]                  # same name, another company
        right = [("P00000000P", "Software Engineer, Platform", "Bask Clinics", "US", 60),  # the account's company
                 ("B00000000B", "Data Analyst", "Bask Clinics", "US", 1)]
        self.file = wk_file(*wrong, *right)
        self.run_wk()
        self.next_hour()
        self.run_wk()
        self.assertEqual(self.meta()["known"][self.BASK_KEY]["tenant"][0], "Bask Health")  # by name: the wrong one
        self.pages[SIMPLIFY_URL] = json.dumps([simplify_row("Bask Health", "Backend Engineer, New Grad", url=wk_link("472CF47F60")),
                                               simplify_row("Bask Health", "Data Analyst", url=wk_link("B00000000B"))])
        self.run_wk()
        right.append(("C00000000C", "ML Engineer", "Bask Clinics", "US", 0))
        self.file = wk_file(*wrong, *right)
        self.next_hour()
        # Corrected by the code: only its recent roles are new (not the 60-day-old one).
        self.assertEqual(self.run_wk(), {"Bask Health": ["ML Engineer"]})
        self.assertEqual(self.meta()["known"][self.BASK_KEY]["tenant"][0], "Bask Clinics")
        # The linked job closes; the wrong company posts a role (listed first in the file): it stays corrected.
        self.file = wk_file(*wrong, ("W00000000W", "Software Engineer II", "Bask Health", "US", 0), *right[:1], *right[2:])
        self.next_hour()
        self.assertEqual(self.run_wk(), {})
        self.file = wk_file(*wrong, *right[:1], *right[2:], ("D00000000D", "Software Engineer", "Bask Clinics", "US", 0))
        self.next_hour()
        self.assertEqual(self.run_wk(), {"Bask Health": ["Software Engineer"]})
        info = self.meta()["known"][self.BASK_KEY]
        self.assertEqual((info["tenant"][0], "matched_by" in info), ("Bask Clinics", False))

    def test_a_rename_found_through_a_still_open_job_code_emails_only_new_roles(self):
        for days in (3, 0):  # (catch_up_days 0: nothing recent to catch up, and the new role still comes)
            with self.subTest(catch_up_days=days):
                self.setUp()
                self.file = wk_file(("472CF47F60", "Backend Engineer, New Grad", "Bask Health", "US", 1),
                                    ("A00000000A", "Software Engineer", "Bask Health", "US", 1))
                self.run_wk(catch_up_days=days)
                self.next_hour()
                self.run_wk(catch_up_days=days)
                # Renamed, with a new website; the list's job is still open.
                self.file = wk_file(("472CF47F60", "Backend Engineer, New Grad", "Bask", "US", 1),
                                    ("A00000000A", "Software Engineer", "Bask", "US", 1),
                                    ("F00000000F", "Software Engineer, Infrastructure", "Bask", "US", 0))
                self.next_hour()
                self.assertEqual(self.run_wk(catch_up_days=days), {"Bask Health": ["Software Engineer, Infrastructure"]})
                self.assertEqual(self.meta()["known"][self.BASK_KEY]["tenant"], ["Bask", "https://bask.example"])

    def test_roles_emailed_for_the_earlier_company_are_not_emailed_again(self):
        # bask-health-1 is matched by name to the wrong "Bask Health" and emails its role...
        wrong = [("A00000000A", "Software Engineer", "Bask Health", "US", 1)]
        right = [("B00000000B", "Data Analyst", "Bask Clinics", "US", 1)]
        self.file = wk_file(*wrong, *right)
        self.run_wk()
        self.next_hour()
        self.assertEqual(self.run_wk(), {"Bask Health": ["Software Engineer"]})
        # ...then a list links its real company's job, and another account named "Bask Health".
        self.pages[SIMPLIFY_URL] = json.dumps([simplify_row("Bask Health", "Backend Engineer, New Grad", url=wk_link("472CF47F60")),
                                               simplify_row("Bask Health", "Data Analyst", url=wk_link("B00000000B")),
                                               simplify_row("Bask Health", "QA Analyst", url=wk_link("9999999999", "bask-health-2"))])
        self.run_wk()
        self.file = wk_file(*wrong, *right, ("C00000000C", "ML Engineer", "Bask Clinics", "US", 0))
        self.next_hour()
        # bask-health-1 moves to Bask Clinics, bask-health-2 gets "Bask Health" by name: A isn't emailed again.
        self.assertEqual(self.run_wk(), {"Bask Health": ["ML Engineer"]})
        known = self.meta()["known"]
        self.assertEqual((known[self.BASK_KEY]["tenant"][0], known["https://apply.workable.com/bask-health-2"]["tenant"][0]),
                         ("Bask Clinics", "Bask Health"))
        # The earlier company's records stay (marked), but don't steer the account back to it.
        records = self.saved_state()[self.BASK]
        self.assertTrue(any(r.get(jm.EARLIER_COMPANY_MARK) and r["url"].endswith("A00000000A") for r in records))

    def test_the_earlier_companys_records_never_steer_the_account_back(self):
        wrong = [("A00000000A", "Software Engineer", "Bask Health", "US", 1)]   # emailed while matched by name
        self.file = wk_file(*wrong, ("B00000000B", "Data Analyst", "Bask Clinics", "US", 1))
        self.run_wk()
        self.next_hour()
        self.assertEqual(self.run_wk(), {"Bask Health": ["Software Engineer"]})
        self.pages[SIMPLIFY_URL] = json.dumps([simplify_row("Bask Health", "Backend Engineer, New Grad", url=wk_link("472CF47F60")),
                                               simplify_row("Bask Health", "Data Analyst", url=wk_link("B00000000B"))])
        self.run_wk()
        self.file = wk_file(*wrong, ("B00000000B", "Data Analyst", "Bask Clinics", "US", 1), ("C00000000C", "QA Analyst", "Bask Clinics", "US", 0))
        self.next_hour()
        self.run_wk()
        self.assertEqual(self.meta()["known"][self.BASK_KEY]["tenant"][0], "Bask Clinics")
        # Bask Clinics' jobs all close; the wrong company posts a role: not followed there.
        self.file = wk_file(*wrong, ("D00000000D", "Software Engineer II", "Bask Health", "US", 0))
        self.next_hour()
        self.assertEqual(self.run_wk(), {})
        self.assertEqual(self.meta()["known"][self.BASK_KEY]["tenant"][0], "Bask Clinics")

    def test_a_name_match_is_undone_when_workables_later_answer_contradicts_it(self):
        # Workable can't be asked at first: matched by the lists' name, to the wrong company.
        self.accounts["bask-health-1"] = (429, "Too Many Requests")
        wrong = [("A00000000A", "Data Analyst", "Bask Health", "US", 5)]
        right = [("P0000000AA", "Software Engineer, Platform", "Bask Telehealth", "US", 60),
                 ("C00000000C", "ML Engineer", "Bask Telehealth", "US", 0)]
        self.file = wk_file(*wrong, *right)
        self.run_wk()
        self.next_hour()
        with self.assertLogs(jm.log, "WARNING"):
            self.run_wk()
        self.assertEqual(self.meta()["known"][self.BASK_KEY]["tenant"][0], "Bask Health")
        # A day later Workable answers with the account's own name: matched to that company; only its recent roles.
        self.accounts["bask-health-1"] = (200, {"name": "Bask Telehealth", "jobs": []})
        meta = self.meta()
        meta["known"][self.BASK_KEY]["looked_up"] = (datetime.now() - timedelta(hours=25)).isoformat(timespec="seconds")
        meta["checked_at"] = (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds")
        meta["workable"].pop("last_modified", None)
        jm.save_state({}, boards=meta)
        self.assertEqual(self.run_wk(), {"Bask Health": ["ML Engineer"]})
        info = self.meta()["known"][self.BASK_KEY]
        self.assertEqual((info["tenant"][0], info["matched_by"]), ("Bask Telehealth", "name"))
        # Workable lists jobs for it that the file lacks: no name match at all.
        self.accounts["bask-health-1"] = (200, {"name": "Bask Telehealth", "jobs": [{"shortcode": "ABCDEF0123"}]})
        meta = self.meta()
        meta["known"][self.BASK_KEY]["looked_up"] = (datetime.now() - timedelta(hours=25)).isoformat(timespec="seconds")
        meta["checked_at"] = (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds")
        meta["workable"].pop("last_modified", None)
        jm.save_state({}, boards=meta)
        self.run_wk()
        self.assertNotIn("tenant", self.meta()["known"][self.BASK_KEY])

    def test_an_account_that_lost_its_name_match_doesnt_stop_the_holder_following_a_rename(self):
        # bask-health-2 holds "Bask Health" by name and records its roles; then bask-health-1 gets it by a code.
        self.pages[SIMPLIFY_URL] = json.dumps([simplify_row("Bask Health", "QA Analyst", url=wk_link("9999999999", "bask-health-2"))])
        rows = [("A00000000A", "Data Analyst", "Bask Health", "US", 5), ("B00000000B", "Data Analyst", "Bask Health", "US", 5)]
        self.file = wk_file(*rows)
        self.run_wk()
        self.next_hour()
        self.run_wk()
        self.pages[SIMPLIFY_URL] = json.dumps([simplify_row("Bask Health", "QA Analyst", url=wk_link("9999999999", "bask-health-2")),
                                               simplify_row("Bask Health", "Backend Engineer, New Grad", url=wk_link("472CF47F60"))])
        self.run_wk()
        self.file = wk_file(*rows, ("472CF47F60", "Backend Engineer, New Grad", "Bask Health", "US", 0))
        self.next_hour()
        self.run_wk()
        known = self.meta()["known"]
        self.assertEqual((known[self.BASK_KEY]["tenant"][0], "tenant" in known["https://apply.workable.com/bask-health-2"]),
                         ("Bask Health", False))
        # The linked job closes, and the company renames itself: bask-health-1 follows it.
        self.file = wk_file(*[(c, t, "Bask", n, d) for c, t, _, n, d in rows], ("F00000000F", "ML Engineer", "Bask", "US", 0))
        self.next_hour()
        self.assertEqual(self.run_wk(), {"Bask Health": ["ML Engineer"]})
        self.assertEqual(self.meta()["known"][self.BASK_KEY]["tenant"], ["Bask", "https://bask.example"])

    def test_a_name_match_undone_and_made_again_with_the_same_company_isnt_a_move(self):
        # (catch_up_days 0: a move would record the new role as seen, and it would never be emailed)
        self.accounts["bask-health-1"] = (429, "Too Many Requests")
        self.file = wk_file(("A00000000A", "Software Engineer", "Bask Health", "US", 1))
        self.run_wk(catch_up_days=0)
        self.next_hour()
        with self.assertLogs(jm.log, "WARNING"):
            self.run_wk(catch_up_days=0)
        # Workable lists a new job the hourly file doesn't have yet: the name match is undone...
        self.accounts["bask-health-1"] = (200, {"name": "Bask Health", "jobs": [{"shortcode": "F00000000F"}]})
        meta = self.meta()
        meta["known"][self.BASK_KEY]["looked_up"] = (datetime.now() - timedelta(hours=25)).isoformat(timespec="seconds")
        meta["checked_at"] = (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds")
        meta["workable"].pop("last_modified", None)
        jm.save_state({}, boards=meta)
        self.assertEqual(self.run_wk(catch_up_days=0), {})
        self.assertNotIn("tenant", self.meta()["known"][self.BASK_KEY])
        # ...and an hour later the file has it: the same company again, through the code. The new role comes.
        self.file = wk_file(("F00000000F", "ML Engineer", "Bask Health", "US", 0))
        self.next_hour()
        self.assertEqual(self.run_wk(catch_up_days=0), {"Bask Health": ["ML Engineer"]})

    def test_a_company_with_no_open_job_that_comes_back_renamed_is_found_again(self):
        self.matched(("A00000000A", "Data Analyst", "Bask Health", "US", 5))
        others = [("E0000000E1", "Nurse", "Other Co", "US", 5), ("E0000000E2", "Nurse", "Other Co", "US", 5)]
        self.file = wk_file(*others)  # all its jobs closed
        self.next_hour()
        self.run_wk()
        self.assertTrue(self.meta()["known"][self.BASK_KEY].get("tenant_gone"))
        # It comes back under a new name: Workable, asked (once a day), lists its new job; matched through it.
        self.accounts["bask-health-1"] = (200, {"name": "Bask", "jobs": [{"shortcode": "F00000000F"}]})
        self.file = wk_file(*others, ("P0000000AA", "Software Engineer, Platform", "Bask", "US", 40),
                            ("F00000000F", "Software Engineer II", "Bask", "US", 0))
        meta = self.meta()
        meta["known"][self.BASK_KEY]["looked_up"] = (datetime.now() - timedelta(hours=25)).isoformat(timespec="seconds")
        meta["checked_at"] = (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds")
        jm.save_state({}, boards=meta)
        self.assertEqual(self.run_wk(), {"Bask Health": ["Software Engineer II"]})  # (its recent roles only)
        info = self.meta()["known"][self.BASK_KEY]
        self.assertEqual((info["tenant"][0], "tenant_gone" in info), ("Bask", False))

    def test_a_rename_isnt_undone_by_workables_answer_from_before_it(self):
        self.accounts["bask-health-1"] = (200, {"name": "Bask Health", "jobs": []})
        self.file = wk_file(("A00000000A", "Software Engineer", "Bask Health", "US", 1))
        self.run_wk()
        self.next_hour()
        self.assertEqual(self.run_wk(), {"Bask Health": ["Software Engineer"]})  # matched by Workable's name for it
        self.file = wk_file(("A00000000A", "Software Engineer", "Bask", "US", 1), ("F00000000F", "ML Engineer", "Bask", "US", 0))
        self.next_hour()
        self.assertEqual(self.run_wk(), {"Bask Health": ["ML Engineer"]})
        self.file = wk_file(("A00000000A", "Software Engineer", "Bask", "US", 1), ("F00000000F", "ML Engineer", "Bask", "US", 0),
                            ("G00000000G", "Software Engineer II", "Bask", "US", 0))
        self.next_hour()
        self.assertEqual(self.run_wk(), {"Bask Health": ["Software Engineer II"]})
        self.assertEqual(self.meta()["known"][self.BASK_KEY]["tenant"][0], "Bask")

    def test_recorded_postings_only_follow_a_company_that_left_the_file(self):
        self.matched(("A00000000A", "Software Engineer", "Bask Health", "US", 5))
        # A stray record of another company's posting (as an older version could leave), listed first in the file.
        state = self.saved_state()
        state[self.BASK].insert(0, {"url": "https://apply.workable.com/j/EEEEEEEEE1"})
        jm.save_json(jm.STATE_PATH, state)
        self.file = wk_file(("EEEEEEEEE1", "Data Analyst", "Other Co", "US", 5),  # (the list's job has closed)
                            ("A00000000A", "Software Engineer", "Bask Health", "US", 5),
                            ("F00000000F", "ML Engineer", "Bask Health", "US", 0))
        self.next_hour()
        self.assertEqual(self.run_wk(), {"Bask Health": ["ML Engineer"]})
        self.assertEqual(self.meta()["known"][self.BASK_KEY]["tenant"][0], "Bask Health")

    def test_a_company_matched_by_a_code_is_taken_from_an_account_holding_it_by_name(self):
        # bask-health-2 holds "Bask Health" by name; then a list links a job of it under bask-health-1.
        self.pages[SIMPLIFY_URL] = json.dumps([simplify_row("Bask Health", "Software Engineer", url=wk_link("9999999999", "bask-health-2"))])
        rows = [("A00000000A", "Software Engineer", "Bask Health", "US", 1)]
        self.file = wk_file(*rows)
        self.run_wk()
        self.next_hour()
        self.run_wk()
        known = self.meta()["known"]
        self.assertEqual(known["https://apply.workable.com/bask-health-2"]["matched_by"], "name")
        self.pages[SIMPLIFY_URL] = json.dumps([simplify_row("Bask Health", "Software Engineer", url=wk_link("9999999999", "bask-health-2")),
                                               simplify_row("Bask Health", "Backend Engineer, New Grad", url=wk_link("472CF47F60"))])
        self.run_wk()
        self.file = wk_file(*rows, ("472CF47F60", "Backend Engineer, New Grad", "Bask Health", "US", 0),
                            ("E00000000E", "ML Engineer", "Bask Health", "US", 0))
        self.next_hour()
        self.assertEqual(self.run_wk(), {"Bask Health": ["ML Engineer"]})  # once (through bask-health-1)
        known = self.meta()["known"]
        self.assertEqual((known[self.BASK_KEY]["tenant"][0], "matched_by" in known[self.BASK_KEY]), ("Bask Health", False))
        self.assertNotIn("tenant", known["https://apply.workable.com/bask-health-2"])

    def test_workables_own_answer_decides_the_name_match(self):
        # Workable names the account differently: the lists' name isn't tried.
        self.accounts["bask-health-1"] = (200, {"name": "Bask Telehealth", "jobs": []})
        self.file = wk_file(("A00000000A", "Software Engineer", "Bask Health", "US", 1))
        self.run_wk()
        self.next_hour()
        self.assertEqual(self.run_wk(), {})
        self.assertNotIn("tenant", self.meta()["known"][self.BASK_KEY])
        # Workable lists jobs for it that the file lacks: a company in the file with its name isn't it.
        self.setUp()
        self.accounts["bask-health-1"] = (200, {"name": "Bask Health", "jobs": [{"shortcode": "Z00000000Z"}]})
        self.file = wk_file(("A00000000A", "Software Engineer", "Bask Health", "US", 1))
        self.run_wk()
        self.next_hour()
        self.assertEqual(self.run_wk(), {})
        self.assertNotIn("tenant", self.meta()["known"][self.BASK_KEY])

    def test_job_codes_change_only_while_unmatched_and_only_as_a_set(self):
        known = {}
        jm.grow_board_registry(known, [(wk_link("AAAAAAAAAA"), "Bask"), (wk_link("BBBBBBBBBB"), "Bask")], "list", "2026-11-18")
        before = copy.deepcopy(known)
        jm.grow_board_registry(known, [(wk_link("BBBBBBBBBB"), "Bask"), (wk_link("AAAAAAAAAA"), "Bask")], "list", "2026-11-18")
        self.assertEqual(known, before)  # a reorder isn't a change (nothing to save)
        known[self.BASK_KEY]["tenant"] = ["Bask", "https://bask.example"]
        jm.grow_board_registry(known, [(wk_link("CCCCCCCCCC"), "Bask")], "list", "2026-11-18")
        self.assertEqual(known[self.BASK_KEY]["codes"], before[self.BASK_KEY]["codes"])  # matched: matched again by its own jobs

    def test_the_registry_keeps_the_latest_job_codes_of_an_account(self):
        known = {}
        jm.grow_board_registry(known, [(wk_link("AAAAAAAAAA"), "Bask Health")], "list", "2026-11-18")
        jm.grow_board_registry(known, [(wk_link(f"{i:010X}"), "Bask Health") for i in range(30)], "list", "2026-11-19")
        codes = known[self.BASK_KEY]["codes"]
        self.assertEqual((len(codes), codes[0]), (jm.workable.CODES_KEPT, "0000000000"))

    def test_one_posting_per_job_code_with_every_location_and_its_earliest_date(self):
        rows = [("Software Engineer", "https://apply.workable.com/j/A00000000A", "A00000000A", "Wed, 11 Nov 2026 18:59:04 UTC", "New York", "NY", "US"),
                ("Software Engineer", "https://apply.workable.com/j/A00000000A", "A00000000A", "Tue, 10 Nov 2026 08:00:00 UTC", "Toronto", "ON", "CA"),
                ("ML Engineer", "https://apply.workable.com/j/B00000000B", "B00000000B", "not a date", "", "", "US")]
        jobs = jm.workable.jobs_from_rows(rows)
        self.assertEqual(jobs, [
            {"title": "Software Engineer", "url": "https://apply.workable.com/j/A00000000A", "location": "New York, NY, US; Toronto, ON, CA",
             "countries": ["US", "CA"], "posted": "2026-11-10T08:00:00+00:00"},
            {"title": "ML Engineer", "url": "https://apply.workable.com/j/B00000000B", "location": "US", "countries": ["US"]}])

    def test_the_shipped_boards_entry_reads_workable(self):
        config = jm.load_config(REPO_DIR / "config_all.json")
        [entry] = [t for t in config["targets"] if t.get("mode") == "boards"]
        self.assertIn("workable", entry["platforms"])
        self.assertIn("Mindrift", entry["skip_companies"])


SR_API = "https://api.smartrecruiters.com/v1/companies/Intuitive/postings"


def sr_link(pid, company="Intuitive", title="software-engineer"):
    """A list's link to a SmartRecruiters posting (speedyapply's form)."""
    return f"https://jobs.smartrecruiters.com/{company}/{pid}-{title}?oga=true"


def sr_page(*posts, company="Intuitive"):
    """A Posting API answer; each post is (id, title, posted days ago[, country])."""
    now = datetime.now(timezone.utc)
    return json.dumps({"offset": 0, "limit": 100, "totalFound": len(posts), "content": [
        {"id": str(pid), "name": title, "releasedDate": (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
         "company": {"identifier": company, "name": company},
         "location": {"city": "Sunnyvale", "region": "CA", "country": rest[0] if rest else "us",
                      "fullLocation": "Sunnyvale, CA, United States"}}
        for pid, title, days, *rest in posts]})


class SmartRecruitersTests(MonitorTestCase):
    """SmartRecruiters companies, read through its public Posting API, gently."""

    def setUp(self):
        super().setUp()
        self.patch(jm, "SMARTRECRUITERS_DELAY_SECONDS", 0)
        self.patch(jm, "LEVER_DELAY_SECONDS", 0)
        self.pages[SIMPLIFY_URL] = json.dumps([simplify_row("Intuitive", "New Grad Software Engineer", url=sr_link(744000000001))])

    def run_sr(self, **entry_extra):
        entry = {"name": "Company job boards", "mode": "boards", "every_hours": 1, "catch_up_days": 3,
                 "platforms": ["greenhouse", "ashby", "lever", "workable", "smartrecruiters"],
                 "tech_titles": ["software", "ml", "infrastructure", "new grad"], **entry_extra}
        targets = [target(name="SimplifyJobs New Grad", url=SIMPLIFY_URL, mode="feed", format="simplify",
                          categories=["Software"], max_age_days=7), entry]
        return self.run_monitor(targets, email=WORKING_EMAIL, role_filter=REPO_ROLE_FILTER, us_only=True)[1]

    def next_hour(self):
        meta = self.saved_state()[jm.BOARDS_KEY]
        meta["checked_at"] = (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds")
        jm.save_state({}, boards=meta)

    def test_smartrecruiters_links_are_recognised(self):
        for link in (sr_link(744000161528139), "https://jobs.smartrecruiters.com/Intuitive/744000161528139",
                     "https://careers.smartrecruiters.com/Intuitive"):
            self.assertEqual(jm.board_api_url(link), ("smartrecruiters", SR_API), link)
        self.assertIsNone(jm.board_api_url("https://jobs.smartrecruiters.com/external-referrals/company/Intuitive/publication/x"))
        self.assertEqual(jm._board_page(SR_API), "https://careers.smartrecruiters.com/Intuitive")
        # The lists' links and the API's links to one posting match, and nothing else does.
        self.assertEqual(jm.job_keys(sr_link(744000161528139)), {("sr", "744000161528139")})
        self.assertEqual(jm.job_keys("https://jobs.smartrecruiters.com/Intuitive/744000161528139"), {("sr", "744000161528139")})
        # A company id with digits in it (a renamed company) doesn't make all its postings look alike.
        self.assertEqual(jm.job_keys("https://jobs.smartrecruiters.com/TBD20261012CrownInnovationsInc/744000144158449"),
                         {("sr", "744000144158449")})
        self.assertEqual(jm.job_keys("https://jobs.smartrecruiters.com/TBD20261012CrownInnovationsInc"), set())

    def test_the_newest_us_postings_are_read_and_more_pages_only_while_all_recent(self):
        asked = []

        def get_json(url, params=None):
            self.assertEqual((params["country"], params["limit"]), ("us", 100))  # only US postings, 100 at a time
            asked.append(params["offset"])
            start = params["offset"]
            return json.loads(sr_page(*[(744000000000 + i, "Software Engineer", 1 if i < 150 else 5) for i in range(start, min(start + 100, 260))]))

        jobs = jm.fetch_smartrecruiters_jobs(SR_API, get_json)
        self.assertEqual((asked, len(jobs)), ([0, 100], 200))  # the second page reaches 5-day-old postings
        self.assertEqual(jobs[0], {"title": "Software Engineer", "url": "https://jobs.smartrecruiters.com/Intuitive/744000000000",
                                   "location": "Sunnyvale, CA, United States", "countries": ["US"], "posted": jobs[0]["posted"]})
        self.assertTrue(jm._posted_since(jobs[0], datetime.now(timezone.utc) - timedelta(days=2)))
        asked.clear()
        all_recent = lambda url, params=None: (asked.append(params["offset"]),
                                               json.loads(sr_page(*[(params["offset"] + i, "SWE", 0) for i in range(100)])))[1]
        self.assertEqual((len(jm.fetch_smartrecruiters_jobs(SR_API, all_recent)), asked), (300, [0, 100, 200]))
        self.assertIsNone(jm.fetch_smartrecruiters_jobs(SR_API, lambda url, params=None: None))

    def test_a_company_is_checked_like_a_board_under_the_monitors_own_name(self):
        agents, etags = [], []
        real_get = self.fake_get

        def get(url, *args, **kwargs):
            if url == SR_API:
                headers = kwargs.get("headers") or {}
                agents.append(headers.get("User-Agent"))
                etags.append(headers.get("If-None-Match"))
                if headers.get("If-None-Match") == f'W/"{len(self.pages[SR_API])}"':
                    return FakeResponse("", status_code=304)
                response = real_get(url, *args, **kwargs)
                response.headers["ETag"] = f'W/"{len(self.pages[SR_API])}"'
                return response
            return real_get(url, *args, **kwargs)

        self.patch(jm.requests, "get", get)
        self.pages[SR_API] = sr_page((744000000001, "New Grad Software Engineer", 1),   # the list has it already
                                     (744000000002, "Software Engineer", 1),
                                     (744000000003, "ML Engineer", 9))                  # too old to catch up
        self.assertEqual(self.run_sr(), {})   # the list's first check
        self.assertEqual(self.run_sr(), {"Intuitive": ["Software Engineer"]})  # the company's first check
        self.assertEqual(set(agents), {jm.MONITOR_USER_AGENT})
        self.next_hour()
        self.assertEqual(self.run_sr(), {})   # "not modified"
        self.assertEqual(etags[-1], f'W/"{len(self.pages[SR_API])}"')
        self.pages[SR_API] = sr_page((744000000004, "Software Engineer, Infrastructure", 0), (744000000001, "New Grad Software Engineer", 1),
                                     (744000000002, "Software Engineer", 1), (744000000003, "ML Engineer", 9))
        self.next_hour()
        self.assertEqual(self.run_sr(), {"Intuitive": ["Software Engineer, Infrastructure"]})
        self.assertIn("1 SmartRecruiters.", jm.boards_page_path().read_text())

    def test_the_first_too_many_requests_stops_smartrecruiters_for_the_run(self):
        self.patch(jm, "SMARTRECRUITERS_WORKERS", 1)
        apis = [f"https://api.smartrecruiters.com/v1/companies/Co{i}/postings" for i in range(3)]
        asked = []
        real_get = self.fake_get

        def get(url, *args, **kwargs):
            if url in apis:
                asked.append(url)
                return FakeResponse("", status_code=429)
            return real_get(url, *args, **kwargs)

        self.patch(jm.requests, "get", get)
        self.pages[GH_ACME] = gh_board((1, "Software Engineer"))
        boards = [{"name": f"Co{i}", "url": u} for i, u in enumerate(apis)] + [{"name": "Acme", "url": GH_ACME}]
        with self.assertLogs(jm.log, "WARNING"):
            results = jm.fetch_boards(boards, {})
        self.assertEqual(asked, apis[:1])
        self.assertEqual([results[u][0] for u in apis], ["failed"] * 3)
        self.assertEqual(results[GH_ACME][0], "ok")  # other boards aren't affected

    def test_the_first_pages_etag_is_kept_when_more_pages_are_read(self):
        # (the next check asks "has page 1 changed?")
        real_get = self.fake_get

        def get(url, *args, **kwargs):
            if url != SR_API:
                return real_get(url, *args, **kwargs)
            offset = kwargs["params"]["offset"]
            response = FakeResponse(sr_page(*[(744000000000 + offset + i, "Software Engineer", 0) for i in range(100 if offset == 0 else 3)]))
            response.headers["ETag"] = f'W/"page-{offset}"'
            return response

        self.patch(jm.requests, "get", get)
        status, jobs, etag = jm.fetch_boards([{"name": "Intuitive", "url": SR_API}], {})[SR_API]
        self.assertEqual((status, len(jobs), etag), ("ok", 103, 'W/"page-0"'))

    def test_other_boards_keep_the_browser_like_name_and_their_etag(self):
        sent = []
        real_get = self.fake_get
        self.patch(jm.requests, "get", lambda url, *a, **k: sent.append(((k.get("headers") or {}).get("User-Agent"))) or real_get(url, *a, **k))
        self.pages[GH_ACME] = gh_board((1, "Software Engineer"))
        jm.fetch_boards([{"name": "Acme", "url": GH_ACME}], {})
        self.assertEqual(sent, [jm.HEADERS["User-Agent"]])

    def test_the_shipped_boards_entry_reads_smartrecruiters(self):
        config = jm.load_config(REPO_DIR / "config_all.json")
        [entry] = [t for t in config["targets"] if t.get("mode") == "boards"]
        self.assertIn("smartrecruiters", entry["platforms"])


WD_SITE = "https://acme.wd5.myworkdayjobs.com/External"
WD_API = "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External/jobs"
WD_BETA_API = "https://beta.wd1.myworkdayjobs.com/wday/cxs/beta/Careers/jobs"


def wd_link(rid, title="Software Engineer", site="acme.wd5.myworkdayjobs.com/en-US/External"):
    """A link to a posting on a Workday site (Acme's by default), as the new-grad lists give it."""
    return f"https://{site}/job/New-York-NY/{title.replace(' ', '-')}_R{rid}"


class _FakeWait:
    """page.expect_event("requestfinished"): the request the page made (its answer in full) that the
    predicate wanted, or a timeout."""

    def __init__(self, predicate):
        self.predicate, self.request = predicate, None

    def __enter__(self):
        return self

    def __exit__(self, kind, exc, tb):
        if kind is None and self.request is None:
            raise TimeoutError("Timeout 15000ms exceeded.")  # (Playwright's is also named TimeoutError)
        return False

    @property
    def value(self):
        return self.request


class _FakeRequest:
    def __init__(self, url, body):
        self.url, self.method, self.post_data, self.answer = url, "POST", json.dumps(body), None
        self.headers = {"content-type": "application/json", "accept": "application/json", "x-calypso-csrf-token": "t0k",
                        "user-agent": "Mozilla/5.0", "referer": "https://example.invalid/"}

    def response(self):
        return self.answer


class _FakeJobsResponse:
    def __init__(self, request, status, text, hangs=None):
        self.url, self.status, self._text, self.request, self._hangs = request.url, status, text, request, hangs

    def text(self):
        if self._hangs:
            self._hangs()  # (a page whose script is stuck: Chromium can't hand over the body)
        return self._text


class _FakeDoc:
    def __init__(self, status, headers=None):
        self.status, self.headers = status, headers or {}


class _FakeHandle:
    def __init__(self, value):
        self.value = value

    def json_value(self):
        return self.value


class _FakeTab:
    """A browser tab on a fake Workday site: opening a site's search page makes the page ask the
    site's jobs API (test.wd) for its first page, with the filters in the page's address, as the
    real page does. test.browser_answers[api] makes it a bot check ("challenge"), a page that
    never asks ("timeout"), a 404 ("missing"), a server error ("unavailable": 500, as Workday
    answers for a company name it doesn't have), a page that goes on to another site and never
    asks ("elsewhere"), a page that ignores the filters, a crash ("boom"), or a page whose script
    gets stuck once its job list is in ("stuck") or when asked for more ("stuck asking for more"):
    Chromium then doesn't answer until the watchdog kills it."""

    def __init__(self, test):
        self.test, self.closed, self._waiting, self.waited_for, self.url = test, False, None, [], "about:blank"

    def expect_event(self, event, predicate, timeout=None):
        self.waited_for.append(event)
        self._waiting = _FakeWait(predicate)
        return self._waiting

    def goto(self, url, wait_until=None, timeout=None):
        test = self.test
        test.loads.append(url)
        self.url = url
        api = jm.workday.api_of(jm.workday.site_of(url))[0]
        how = test.browser_answers.get(api)
        if how == "boom":
            raise RuntimeError("Target page, context or browser has been closed")
        if how == "challenge":
            return _FakeDoc(403, {"cf-mitigated": "challenge"})
        if how == "missing":
            return _FakeDoc(404)
        if how == "unavailable":
            return _FakeDoc(500)
        if how == "elsewhere":
            self.url = "https://community.workday.com/maintenance-page"
        elif how != "timeout":
            facets = {} if how == "ignores filters" else {k: v for k, v in jm.parse_qs(jm.urlparse(url).query).items()}
            body = {"appliedFacets": facets, "limit": 20, "offset": 0, "searchText": ""}
            status, text = test.wd_answer(api, body, "page")
            request = _FakeRequest(api, body)
            request.answer = _FakeJobsResponse(request, status, text, test.until_killed if how == "stuck" else None)
            if self._waiting and self._waiting.predicate(request) and self._waiting.request is None:
                self._waiting.request = request
        return _FakeDoc(200)

    def wait_for_function(self, script, arg=None, polling=None, timeout=None):
        url, body, headers, _, _ = arg
        self.test.fetch_headers.append(headers)
        if self.test.browser_answers.get(url) == "stuck asking for more":
            self.test.until_killed()
        if body is None:  # (a GET: a posting's own page, for its place)
            return _FakeHandle(json.dumps(dict(zip(("status", "text"), self.test.wd_detail(url, "fetch")))))
        status, text = self.test.wd_answer(url, body, "fetch")
        return _FakeHandle(json.dumps({"status": status, "text": text}))

    def close(self):
        self.closed = True


class FakePlaywright:
    """Stands in for a started Playwright (boards/browser.py): one Chromium, its tabs on fake Workday sites."""

    def __init__(self, test):
        self.test, self.chromium, self.stopped, self.launched, self.closed, self.tabs = test, self, False, 0, False, []

    def launch(self, **kwargs):
        if self.test.browser_broken:
            raise RuntimeError("Executable doesn't exist at /ms-playwright/chromium\nRun playwright install")
        self.launched += 1
        self.test.chromiums.add(1000 + len(self.test.chromiums))  # (its process, for the watchdog)
        return self

    def new_context(self, **kwargs):
        self.context_options = kwargs
        return self

    def new_page(self):
        tab = _FakeTab(self.test)
        self.tabs.append(tab)
        return tab

    def close(self):
        self.closed = True

    def stop(self):
        self.stopped = True


class WorkdayTestCase(MonitorTestCase):
    """Fake Workday sites (self.wd: each site's jobs API -> its postings, newest first, as (id, title,
    postedOn[, location]), or an HTTP status), read by plain requests (refused when
    self.api_refuses, as from GitHub's machines since Nov 2026) or through a fake browser."""

    def setUp(self):
        super().setUp()
        self.slept = []
        self.patch(jm.workday, "_sleep", self.slept.append)
        self.wd = {WD_API: []}
        self.posts = []            # (API URL, offset, how: "api", "page" (the page's own request) or "fetch" (asked from the page))
        self.loads = []            # pages opened in the browser
        self.fetch_headers = []
        self.bodies = []           # what each request for a page of jobs asked
        self.fail_offsets = set()  # pages of the job list that fail (500)
        self.api_refuses = False
        self.browser_answers = {}
        self.browser_broken = False
        self.playwrights = []      # one per run that started the browser
        self.chromiums = set()     # the fake Chromium processes started (one per launch)
        self.killed = []           # those the watchdog killed
        self.chromium_killed = threading.Event()
        self.details = {}          # a posting's number -> what Workday's page for it says ("jobPostingInfo")
        self.detail_asks = []      # (posting page asked, how)

        def start():
            self.playwrights.append(FakePlaywright(self))
            return self.playwrights[-1]

        self.patch(jm.workday, "Browser", lambda: RealBrowser(start=start))
        self.patch(browser_module, "_chromiums", lambda: set(self.chromiums))
        self.patch(browser_module, "_kill", self.fake_kill)
        self.patch(jm.requests, "post", self.fake_wd_post)
        self.patch(jm, "fetch_browser", mock.Mock(side_effect=AssertionError("Workday isn't read with fetch_browser")))
        self.pages[SIMPLIFY_URL] = json.dumps([simplify_row("Acme", "New Grad Software Engineer", url=wd_link(1, "New Grad Software Engineer"))])

    def wd_answer(self, api, body, how):
        self.posts.append((api, body.get("offset"), how))
        self.bodies.append(body)
        site = self.wd.get(api)
        if body.get("offset") in self.fail_offsets:
            return 500, ""
        if site is None:
            return 404, ""
        if isinstance(site, int):
            return site, ""
        start, limit = body["offset"], body["limit"]
        return 200, json.dumps({"total": len(site), "jobPostings": [
            {"title": title, "externalPath": f"/job/New-York-NY/{title.replace(' ', '-')}_R{rid}",
             "postedOn": posted, "locationsText": rest[0] if rest else "New York, NY"}
            for rid, title, posted, *rest in site[start:start + limit]]})

    def wd_detail(self, url, how):
        """A posting's own page in Workday's API (/wday/cxs/<tenant>/<site>/job/...): what
        self.details has for its number, or 404."""
        self.detail_asks.append((url, how))
        number = jm.re.search(r"_R(\d+)$", url)
        info = self.details.get(int(number.group(1))) if number else None
        return (200, json.dumps({"jobPostingInfo": info})) if info is not None else (404, "")

    def fake_kill(self, pids):
        self.killed += sorted(pids)
        self.chromium_killed.set()

    def until_killed(self):
        """A call into a stuck page: it doesn't come back until the watchdog kills Chromium (then it fails)."""
        if not self.chromium_killed.wait(10):
            raise AssertionError("the watchdog didn't stop the browser")
        self.chromium_killed.clear()
        raise RuntimeError("Target page, context or browser has been closed")

    def fake_wd_post(self, url, *args, **kwargs):
        if self.api_refuses:
            self.posts.append((url, kwargs["json"].get("offset"), "api"))
            refused = FakeResponse("<html>Just a moment...</html>", status_code=429)
            refused.headers["Cf-Mitigated"] = "challenge"
            return refused
        status, text = self.wd_answer(url, kwargs["json"], "api")
        return FakeResponse(text, status_code=status)

    def asked(self, how=None):
        return [(api, offset) for api, offset, via in self.posts if how is None or via == how]

    def entry(self, **extra):
        return {"name": "Company job boards", "mode": "boards", "every_hours": 1, "catch_up_days": 3,
                "platforms": ["greenhouse", "ashby", "lever", "workday"],
                "tech_titles": ["software", "ml", "infrastructure", "new grad"], **extra}

    def run_wd(self, *extra_targets, boards=True, **entry_extra):
        targets = [target(name="SimplifyJobs New Grad", url=SIMPLIFY_URL, mode="feed", format="simplify",
                          categories=["Software"], max_age_days=7), *extra_targets] + ([self.entry(**entry_extra)] if boards else [])
        return self.run_monitor(targets, email=WORKING_EMAIL, role_filter=REPO_ROLE_FILTER, us_only=True)[1]

    def known(self):
        return self.saved_state().get(jm.BOARDS_KEY, {}).get("known", {})

    def two_sites(self, *more):
        """Acme and Beta (and these other companies) on Workday, in the feed."""
        self.wd[WD_BETA_API] = [(9, "Software Engineer", "Posted Today")]
        rows = [simplify_row("Acme", "New Grad Software Engineer", url=wd_link(1)),
                simplify_row("Beta", "Software Engineer", url="https://beta.wd1.myworkdayjobs.com/Careers/job/X/SWE_R99")]
        for name in more:
            self.wd[f"https://{name.lower()}.wd1.myworkdayjobs.com/wday/cxs/{name.lower()}/Careers/jobs"] = [(1, "Software Engineer", "Posted Today")]
            rows.append(simplify_row(name, "Software Engineer", url=f"https://{name.lower()}.wd1.myworkdayjobs.com/Careers/job/X/SWE_R1"))
        self.pages[SIMPLIFY_URL] = json.dumps(rows)


class WorkdayBoardTests(WorkdayTestCase):
    """Company Workday sites found through the lists (the "boards" entry with "workday")."""

    def test_workday_links_are_recognised(self):
        cases = {
            wd_link(5): WD_SITE,
            "https://acme.wd5.myworkdayjobs.com/External/job/Remote/SWE_R5/apply": WD_SITE,
            "https://ebay.wd5.myworkdayjobs.com/apply/job/Toronto/Engineer_R0076353": "https://ebay.wd5.myworkdayjobs.com/apply",
            "https://acme.wd5.myworkdayjobs.com/en-us/External": WD_SITE,
            "https://wd5.myworkdaysite.com/en-US/recruiting/chewy/CHEWY/job/Boston/SWE_R1":
                "https://wd5.myworkdaysite.com/recruiting/chewy/CHEWY",
        }
        for link, site in cases.items():
            self.assertEqual(jm.board_api_url(link), ("workday", site), link)
        for link in ("https://acme.wd5.myworkdayjobs.com/", "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External/jobs",
                     "https://wd5.myworkdaysite.com/recruiting/chewy", "https://acme.myworkdayjobs.com/External"):
            self.assertIsNone(jm.board_api_url(link), link)
        self.assertEqual(jm.workday.api_of("https://wd5.myworkdaysite.com/recruiting/chewy/CHEWY"),
                         ("https://wd5.myworkdaysite.com/wday/cxs/chewy/CHEWY/jobs",
                          "https://wd5.myworkdaysite.com/en-US/recruiting/chewy/CHEWY"))
        self.assertEqual(jm._board_platform(WD_SITE), "workday")
        self.assertEqual(jm._board_page(WD_SITE), WD_SITE)
        # One site whichever form of its address a link uses; a posting is its company's job number.
        other_form = "https://wd5.myworkdaysite.com/en-US/recruiting/acme/External/job/Boston/SWE_R5?utm_source=Simplify"
        self.assertEqual(jm.workday.site_key(other_form), jm.workday.site_key(wd_link(5)))
        self.assertEqual(jm.workday.site_key(wd_link(5)), jm._url_key(WD_SITE))
        self.assertEqual(jm.job_keys(other_form), jm.job_keys(wd_link(5)))
        # (two companies on the same wdN.myworkdaysite.com host don't share job numbers)
        self.assertNotEqual(jm.job_keys(other_form), jm.job_keys(other_form.replace("/acme/", "/beta/")))

    def test_only_the_newest_pages_of_a_workday_site_are_read(self):
        reader = jm.workday.Reader(pause=0)
        self.wd[WD_API] = [(i, f"Software Engineer {i}", "Posted Today") for i in range(1, 46)]
        status, jobs = reader.read(WD_SITE)
        self.assertEqual((status, len(jobs), [o for _, o in self.asked()]), ("ok", 45, [0, 20, 40]))  # to the end of the list
        self.assertEqual((jobs[0]["url"], jobs[0]["location"]), (wd_link(1, "Software Engineer 1"), "New York, NY"))
        self.assertTrue(jm._posted_since(jobs[0], datetime.now(timezone.utc) - timedelta(minutes=5)))
        # Up to the first page with a posting seen before...
        self.posts.clear()
        seen = {jm.workday.posting_key(wd_link(25, "Software Engineer 25"))}
        self.assertEqual((len(reader.read(WD_SITE, seen)[1]), len(self.posts)), (40, 2))
        # ...or one older than a week, and never more than MAX_PAGES.
        self.wd[WD_API] = [(i, "Software Engineer", "Posted 2 Days Ago") for i in range(1, 30)] + \
                          [(i, "Software Engineer", "Posted 8 Days Ago") for i in range(30, 200)]
        self.assertEqual(len(reader.read(WD_SITE)[1]), 40)
        self.wd[WD_API] = [(i, "Software Engineer", "Posted Yesterday") for i in range(1, 200)]
        self.assertEqual(len(reader.read(WD_SITE)[1]), 20 * jm.workday.MAX_PAGES)
        self.assertEqual(self.loads, [])  # (Workday answered plain requests: no browser)

    def test_a_workday_site_found_through_a_list_is_checked_like_a_board(self):
        self.wd[WD_API] = [
            (2, "Software Engineer", "Posted Today"),
            (1, "New Grad Software Engineer", "Posted Yesterday"),   # the new-grad list has it already
            (4, "Software Engineer", "Posted 2 Days Ago", "Bengaluru, India"),
            (5, "Account Executive", "Posted 2 Days Ago"),
            (3, "ML Engineer", "Posted 30+ Days Ago"),               # too old to catch up
        ]
        self.assertEqual(self.run_wd(), {})  # the list's first check
        # The next run finds Acme's Workday site: its first check emails its recent tech roles.
        self.assertEqual(self.run_wd(), {"Acme": ["Software Engineer"]})
        info = self.known()[jm._url_key(WD_SITE)]
        self.assertEqual((info["name"], bool(info["caught_up"]), bool(info["tried_at"])), ("Acme", True, True))
        page = jm.boards_page_path().read_text()
        self.assertIn("1 Workday.", page)
        self.assertIn("1 read, 0 couldn't be read (tried again later), 0 left for a later run.", page)
        self.assertIn("so each is read only every several hours. Last run", page)  # (all read: no pace to tell)
        self.assertEqual(self.saved_state()[WD_SITE][0], {"url": wd_link(2), "reported": "match"})  # (the link only, like other boards)
        # Next run: a new posting is emailed; an old one we hadn't seen (it moved up the list) isn't.
        self.wd[WD_API][:0] = [(7, "Software Engineer, Infrastructure", "Posted Today"), (6, "Software Engineer II", "Posted 30+ Days Ago")]
        self.posts.clear()
        self.assertEqual(self.run_wd(), {"Acme": ["Software Engineer, Infrastructure"]})
        self.assertEqual(self.asked(), [(WD_API, 0)])  # its first page had postings seen before
        self.assertEqual(self.run_wd(), {})

    def test_workday_sites_are_read_one_request_at_a_time_after_everything_else(self):
        self.two_sites()
        self.wd[WD_API] = [(i, "Software Engineer", "Posted Today") for i in range(30, 0, -1)]
        order = []
        real_get, real_post = jm.requests.get, jm.requests.post
        self.patch(jm.requests, "get", lambda url, *a, **k: order.append(url) or real_get(url, *a, **k))
        self.patch(jm.requests, "post", lambda url, *a, **k: order.append(url) or real_post(url, *a, **k))
        self.mark_known(GH_ACME)
        self.pages[GH_ACME] = gh_board((1, "Software Engineer"))
        mine = target(name="Zed", url=GH_ACME, mode="api")  # (after the Workday sites by name)
        self.run_wd(mine)
        order.clear()
        self.slept.clear()
        self.run_wd(mine, workday_seconds_between_requests=3)
        workday = [i for i, u in enumerate(order) if "myworkday" in u]
        self.assertLess(order.index(GH_ACME), workday[0])
        # Two pages of Acme's (30 postings), one of Beta's: a pause before each request but the first.
        self.assertEqual([order[i] for i in workday], [WD_API, WD_API, WD_BETA_API])
        self.assertEqual(len(self.slept), 2)
        self.assertTrue(all(2.9 < pause <= 3 for pause in self.slept), self.slept)

    def test_where_plain_requests_work_only_a_site_whose_plain_request_fails_opens_the_browser(self):
        self.two_sites("Gamma", "Delta")
        self.run_wd()
        self.assertEqual((self.loads, self.playwrights), ([], []))  # (all read with plain requests: no browser)
        self.wd[WD_BETA_API] = 500  # (as for a company name Workday doesn't have)
        self.run_wd()
        self.assertEqual(self.loads, ["https://beta.wd1.myworkdayjobs.com/en-US/Careers"])
        [playwright] = self.playwrights
        self.assertEqual((playwright.launched, playwright.stopped), (1, True))
        # If the browser can't start there, that site is skipped and the others are still read with plain requests.
        self.loads.clear()
        self.posts.clear()
        self.browser_broken = True
        with self.assertLogs(jm.log, "INFO") as logs:
            self.run_wd()
        output = "\n".join(logs.output)
        self.assertNotIn("Workday reading stops", output)
        self.assertEqual(output.count("a site whose plain request fails is skipped this run; the others are still read"), 1)
        self.assertIn("3 of 4 company Workday sites read", output)
        self.assertEqual(sorted(api for api, _ in self.asked("api")), sorted(self.wd))
        self.assertEqual(self.loads, [])

    def test_workday_reading_stops_when_its_time_is_up_and_continues_next_run(self):
        self.two_sites()
        self.wd[WD_API] = [(2, "Software Engineer", "Posted Today")]
        clock = [0.0]

        def tick():
            clock[0] += 50
            return clock[0]

        self.patch(jm.workday, "_monotonic", tick)
        self.run_wd()
        self.posts.clear()
        with self.assertLogs(jm.log, "INFO") as logs:
            self.run_wd(workday_minutes_per_run=1)
        self.assertIn("1 of 2 company Workday sites read", "\n".join(logs.output))
        self.assertIn("1 wait for a later run (time's up: 1 min a run)", "\n".join(logs.output))
        self.assertIn("1 read, 0 couldn't be read (tried again later), 1 left for a later run (time's up: 1 min a run).",
                      jm.boards_page_path().read_text())
        self.assertIn("so each is read only every several hours: at the last run's pace, reading all 2 takes about 2 runs. Last run",
                      jm.boards_page_path().read_text())
        self.assertIn("Beta | [Workday](https://beta.wd1.myworkdayjobs.com/Careers) (not read yet: waiting for its turn)",
                      jm.boards_page_path().read_text())
        self.assertEqual(self.asked(), [(WD_API, 0)])
        # Next run Beta's turn comes first (never read), then Acme's (read longest ago).
        self.posts.clear()
        self.run_wd(workday_minutes_per_run=1)
        self.assertEqual(self.asked(), [(WD_BETA_API, 0)])
        self.posts.clear()
        self.run_wd(workday_minutes_per_run=5)
        self.assertEqual(self.asked(), [(WD_API, 0), (WD_BETA_API, 0)])

    def test_no_workday_site_is_started_once_the_run_is_24_minutes_old(self):
        # Runs come every 30 minutes: whatever time workday_minutes_per_run leaves, no Workday site is
        # started once the run (counted from when its Reader was made, at its start) is RUN_MINUTES old.
        limit = jm.workday.RUN_MINUTES
        self.assertEqual((limit, jm.workday.MINUTES_PER_RUN), (24, 24))
        clock = [5000.0]
        self.patch(jm.workday, "_monotonic", lambda: clock[0])
        self.two_sites("Gamma")
        reader = jm.workday.Reader(pause=0)
        clock[0] += (limit - 2) * 60  # (everything else took 22 minutes)
        answer = self.wd_answer

        def slow(*args):  # (each site takes a minute and a half)
            clock[0] += 90
            return answer(*args)

        self.wd_answer = slow
        sites = [{"url": WD_SITE, "name": "Acme"}] + [{"url": f"https://{n}.wd1.myworkdayjobs.com/Careers", "name": n} for n in ("beta", "gamma")]
        with self.assertLogs(jm.log, "INFO") as logs:
            results, summary = jm.workday.read_sites(reader, sites, {}, limit)
        self.assertEqual(list(results), [WD_SITE, "https://beta.wd1.myworkdayjobs.com/Careers"])  # (Beta started at 23:30)
        self.assertEqual((summary["read"], summary["left"], summary["why"]),
                         (2, 1, "the run is 24 minutes old, and runs come every 30 minutes"))
        self.assertIn("No more Workday sites this run: the run is 24 minutes old", "\n".join(logs.output))
        self.assertEqual(reader.read(WD_SITE), ("stopped", None))  # (your entries too)
        self.assertEqual(len(self.asked()), 2)

    def test_the_workflows_time_limit_leaves_room_after_the_last_workday_site(self):
        # Setup (up to ~4 min), RUN_MINUTES, a site started just before (up to ~3 min) and the email
        # and save must all fit in the workflow's timeout-minutes, or the run is stopped unsaved.
        workflow = (REPO_DIR / ".github" / "workflows" / "job-monitor.yml").read_text()
        [limit] = [int(n) for n in jm.re.findall(r"^\s*timeout-minutes:\s*(\d+)", workflow, jm.re.MULTILINE)]
        self.assertEqual(limit, jm.workday.JOB_MINUTES)
        self.assertGreaterEqual(limit - jm.workday.RUN_MINUTES, 10)
        self.assertLess(jm.workday.RUN_MINUTES, 30)  # (the next run is due 30 minutes after this one)

    def test_a_run_that_is_24_minutes_old_leaves_workday_for_the_next_run(self):
        self.two_sites()
        mine = target(name="Acme Corp", url=WD_SITE + "?workerSubType=abc", mode="browser", link_selector="")
        self.run_wd(mine)
        clock = [0.0]
        self.patch(jm.workday, "_monotonic", lambda: clock[0])
        feed_get = jm.requests.get

        def slow_get(url, *args, **kwargs):
            if url == SIMPLIFY_URL:
                clock[0] += jm.workday.RUN_MINUTES * 60  # (this run, the lists took 24 minutes)
            return feed_get(url, *args, **kwargs)

        self.patch(jm.requests, "get", slow_get)
        read_sites = mock.Mock(wraps=jm.workday.read_sites)
        self.patch(jm.workday, "read_sites", read_sites)
        self.posts.clear()
        with self.assertLogs(jm.log, "INFO") as logs:
            self.run_wd(mine, workday_minutes_per_run=30)
        self.assertEqual(self.asked(), [])  # neither your Workday entry nor the company sites
        self.assertIn("Acme Corp", "\n".join(logs.output))
        self.assertIn("Not read this run: the run is 24 minutes old", "\n".join(logs.output))
        self.assertEqual(read_sites.call_args.args[3], jm.workday.RUN_MINUTES)  # (the setting can't go past 24 minutes either)
        self.assertIn("0 read, 0 couldn't be read (tried again later), 1 left for a later run (the run is 24 minutes old",
                      jm.boards_page_path().read_text())
        # A run of the usual length reads them.
        self.patch(jm.requests, "get", feed_get)
        self.run_wd(mine)
        self.assertEqual(self.asked(), [(WD_API, 0), (WD_BETA_API, 0)])

    def test_sites_never_read_come_first_newest_linked_first_then_the_longest_unread(self):
        known = {f"k{i}": {"url": f"https://c{i}.wd1.myworkdayjobs.com/Careers", "added": "2026-11-10"} for i in range(5)}
        known["k1"]["tried_at"], known["k3"]["tried_at"] = "2026-11-19T10:00:00", "2026-11-19T08:00:00"
        known["k4"]["added"] = "2026-11-18"
        sites = [{"url": info["url"], "_board_key": key} for key, info in known.items()]
        ordered = jm.workday.order(sites, known, {"k2": "2026-11-19", "k0": "2026-11-12"})
        self.assertEqual([s["_board_key"] for s in ordered], ["k2", "k0", "k4", "k3", "k1"])
        self.assertEqual(jm.workday.newest_links([(wd_link(1), "2026-11-12"), (wd_link(2), "2026-11-19"), ("https://x.example/1", "2026-11-20")]),
                         {jm._url_key(WD_SITE): "2026-11-19"})

    def test_a_site_the_lists_link_under_both_forms_of_its_address_is_read_once_and_emails_once(self):
        # The lists link Chewy (and Guidewire, Magna) both as chewy.wd5.myworkdayjobs.com/External and as
        # wd5.myworkdaysite.com/recruiting/chewy/External: one site with the same postings.
        other_form = "wd5.myworkdaysite.com/en-US/recruiting/acme/External"
        self.pages[SIMPLIFY_URL] = json.dumps([
            simplify_row("Acme", "Software Engineer", url=wd_link(1)),
            simplify_row("Acme", "Software Engineer II", url=wd_link(2, "Software Engineer II", site=other_form))])
        self.wd[WD_API] = self.wd["https://wd5.myworkdaysite.com/wday/cxs/acme/External/jobs"] = \
            [(1, "Software Engineer", "Posted 5 Days Ago"), (2, "Software Engineer II", "Posted 5 Days Ago")]  # (the same list)
        self.run_wd()
        self.run_wd()
        self.assertEqual([k for k in self.known() if "myworkday" in k], [jm._url_key(WD_SITE)])
        self.posts.clear()
        self.wd[WD_API].insert(0, (500, "Software Engineer, Platform", "Posted Today"))
        self.assertEqual(self.run_wd(), {"Acme": ["Software Engineer, Platform"]})
        self.assertEqual(self.asked(), [(WD_API, 0)])  # read once
        # A posting the list gave under one form isn't emailed again by the site under the other.
        rows = json.loads(self.pages[SIMPLIFY_URL])
        self.pages[SIMPLIFY_URL] = json.dumps(rows + [simplify_row("Acme", "Software Engineer, Data", url=wd_link(700, "Software Engineer, Data", site=other_form))])
        self.assertEqual(self.run_wd(), {"Acme (via SimplifyJobs New Grad)": ["Software Engineer, Data"]})
        self.wd[WD_API].insert(0, (700, "Software Engineer, Data", "Posted Today"))
        self.assertEqual(self.run_wd(), {})
        # Nor the other way round.
        self.wd[WD_API].insert(0, (800, "Software Engineer, Infrastructure", "Posted Today"))
        self.assertEqual(self.run_wd(), {"Acme": ["Software Engineer, Infrastructure"]})
        rows = json.loads(self.pages[SIMPLIFY_URL])
        self.pages[SIMPLIFY_URL] = json.dumps(rows + [simplify_row("Acme", "Software Engineer, Infrastructure",
                                                                   url=wd_link(800, "Software Engineer, Infrastructure", site=other_form))])
        self.assertEqual(self.run_wd(), {})

    def test_a_workday_site_your_entry_searches_is_not_checked_twice(self):
        mine = target(name="Acme", url="https://acme.wd5.myworkdayjobs.com/en-US/External?workerSubType=abc", mode="api")
        self.wd[WD_API] = [(2, "Software Engineer", "Posted Today")]
        self.run_wd(mine)
        self.run_wd(mine)
        self.assertEqual([k for k in self.known() if "myworkday" in k], [jm._url_key(WD_SITE)])  # (found)
        self.assertNotIn(WD_SITE, self.saved_state())  # (not checked)
        self.assertEqual({o for _, o in self.asked()}, {0})

    def test_a_companys_workday_site_is_not_read_when_one_of_your_entries_has_its_name(self):
        # Your PayPal entry reads PayPal's Eightfold page, which lists the same jobs as PayPal's Workday site
        # under other numbers: nothing could tell them apart, so reading both would email each new role twice.
        self.wd[WD_API] = [(2, "Software Engineer", "Posted Today")]
        self.pages[URL] = page(*jobs_named("Software Engineer"))
        mine = target(name="ACME, Inc.", url=URL)  # (its own career page: same name, whatever the case or "Inc")
        self.run_wd(mine)
        self.wd[WD_API].insert(0, (3, "Software Engineer, Platform", "Posted Today"))
        self.pages[URL] = page(*jobs_named("Software Engineer", "Software Engineer, Platform"))
        self.assertEqual(self.run_wd(mine), {"ACME, Inc.": ["Software Engineer, Platform"]})  # (once)
        self.assertEqual([k for k in self.known() if "myworkday" in k], [jm._url_key(WD_SITE)])  # (found)
        self.assertNotIn(WD_SITE, self.saved_state())  # (not checked)
        self.assertEqual(self.asked(), [])
        # The same while your entry is paused (its postings stay yours); a board on another platform under
        # the same name, and a Workday site when your entry with its name is on Workday too (the company's
        # job numbers tell its postings apart there), are still read.
        rows = [{"url": wd_link(1), "company": "Acme"}, {"url": "https://job-boards.greenhouse.io/acme/jobs/123", "company": "Acme"}]
        state = {SIMPLIFY_URL: rows}
        feed = target(name="SimplifyJobs New Grad", url=SIMPLIFY_URL, mode="feed")

        def plan(*yours):
            return sorted(jm._board_platform(b["url"]) for b in jm.plan_boards(self.entry(), [feed, *yours], state, jm._board_meta({}), False))

        self.assertEqual(plan(), ["greenhouse", "workday"])
        self.assertEqual(plan(target(name="Acme", url="https://acme.eightfold.ai/careers", mode="eightfold", enabled=False)), ["greenhouse"])
        self.assertEqual(plan(target(name="Acme", url="https://acme.wd5.myworkdayjobs.com/University", mode="browser")), ["greenhouse", "workday"])
        self.assertEqual(plan(target(name="Acme Labs", url=URL)), ["greenhouse", "workday"])

    def test_an_hourly_check_reads_a_workday_site_only_down_to_what_it_saw_before(self):
        self.wd[WD_API] = [(i, "Account Executive", "Posted Today") for i in range(60, 0, -1)]
        self.run_wd()
        self.run_wd()  # the site's first check reads all three pages
        self.wd[WD_API].insert(0, (61, "Software Engineer", "Posted Today"))
        self.posts.clear()
        self.assertEqual(self.run_wd(), {"Acme": ["Software Engineer"]})
        self.assertEqual(self.asked(), [(WD_API, 0)])

    def test_a_workday_site_keeps_its_latest_postings_and_those_it_emailed(self):
        self.patch(jm.workday, "RETENTION", 3)
        self.patch(jm.workday, "PAGE_SIZE", 2)
        self.patch(jm.workday, "MAX_PAGES", 1)
        self.wd[WD_API] = [(2, "Account Executive", "Posted Today"), (1, "Sales Manager", "Posted Today")]
        self.run_wd()
        self.run_wd()
        self.wd[WD_API][:0] = [(4, "Account Executive II", "Posted Today"), (3, "Software Engineer II", "Posted Today")]
        self.assertEqual(self.run_wd(), {"Acme": ["Software Engineer II"]})
        # A busy site: its new postings push older ones out, but never one it emailed you...
        self.wd[WD_API][:0] = [(6, "Store Manager", "Posted Today"), (5, "Cashier", "Posted Today")]
        self.run_wd()
        self.wd[WD_API][:0] = [(8, "Pharmacist", "Posted Today"), (7, "Stocker", "Posted Today")]
        self.run_wd()
        self.assertEqual([r["url"] for r in self.saved_state()[WD_SITE]],
                         [wd_link(8, "Pharmacist"), wd_link(7, "Stocker"), wd_link(6, "Store Manager"), wd_link(3, "Software Engineer II")])
        # ...so a list linking it a day later doesn't email it again.
        rows = json.loads(self.pages[SIMPLIFY_URL])
        self.pages[SIMPLIFY_URL] = json.dumps(rows + [simplify_row("Acme", "Software Engineer II", url=wd_link(3, "Software Engineer II"))])
        self.assertEqual(self.run_wd(), {})
        # (The postings emailed are kept up to their own limit, the latest first.)
        old = [{"url": f"https://x.example/{i}", "reported": "match"} for i in range(3)] + [{"url": "https://x.example/p"}]
        self.assertEqual([j["url"] for j in jm.merge_jobs_new_first(old, [{"url": "https://x.example/new"}], 1, max_reported=1)],
                         ["https://x.example/new", "https://x.example/0"])

    def test_a_workday_posting_emailed_from_a_list_is_not_emailed_again_from_the_site(self):
        self.wd[WD_API] = [(1, "New Grad Software Engineer", "Posted Today")]
        self.run_wd()
        self.run_wd()  # (the site's first check)
        # The list adds a posting (linked under another site path, with a tracking tag); then the site lists it too.
        self.pages[SIMPLIFY_URL] = json.dumps([
            simplify_row("Acme", "New Grad Software Engineer", url=wd_link(1, "New Grad Software Engineer")),
            simplify_row("Acme", "Software Engineer, Platform", url=wd_link(8, "Software-Engineer-Platform", site="acme.wd5.myworkdayjobs.com/en-US/Careers") + "?utm_source=Simplify")])
        self.assertEqual(self.run_wd(), {"Acme (via SimplifyJobs New Grad)": ["Software Engineer, Platform"]})
        self.wd[WD_API].insert(0, (8, "Software Engineer, Platform", "Posted Today"))
        self.assertEqual(self.run_wd(), {})
        # And the other way round: the site's posting, listed later by the list, isn't emailed again.
        self.wd[WD_API].insert(0, (9, "Software Engineer, Data", "Posted Today"))
        self.assertEqual(self.run_wd(), {"Acme": ["Software Engineer, Data"]})
        rows = json.loads(self.pages[SIMPLIFY_URL])
        self.pages[SIMPLIFY_URL] = json.dumps(rows + [simplify_row("Acme", "Software Engineer, Data", url=wd_link(9, "Software Engineer, Data") + "?utm_source=Simplify")])
        self.assertEqual(self.run_wd(), {})

    def test_the_shipped_config_finds_companies_in_both_whole_simplify_lists(self):
        # The new-grad feed only keeps tech rows from the last 90 days; its whole list (and the internship
        # list) are read for companies, so one that last hired in the spring, or under Hardware, is checked too.
        config = jm.load_config(REPO_DIR / "config_all.json")
        [entry] = [t for t in config["targets"] if t.get("mode") == "boards"]
        [feed] = [t for t in config["targets"] if t.get("format") == "simplify"]
        self.assertIn(INTERNSHIPS_URL, [i["url"] for i in entry["find_companies_in"]])
        self.assertIn(feed["url"], [i["url"] for i in entry["find_companies_in"]])
        self.assertIn("Quant", feed["categories"])

    def test_the_shipped_boards_entry_reads_workday_sites(self):
        config = jm.load_config(REPO_DIR / "config_all.json")
        [entry] = [t for t in config["targets"] if t.get("mode") == "boards"]
        self.assertIn("workday", entry["platforms"])
        self.assertEqual((entry["workday_minutes_per_run"], entry["workday_seconds_between_requests"]),
                         (jm.workday.MINUTES_PER_RUN, jm.workday.SECONDS_BETWEEN_REQUESTS))


class WorkdayBrowserTests(WorkdayTestCase):
    """Reading Workday through the browser once plain requests are refused (as from GitHub's machines)."""

    def setUp(self):
        super().setUp()
        self.api_refuses = True

    def test_once_a_plain_request_is_refused_the_run_reads_workday_through_one_browser(self):
        self.two_sites()
        self.wd[WD_API] = [(2, "Software Engineer", "Posted Today"), (1, "New Grad Software Engineer", "Posted Today")]
        self.run_wd()
        with self.assertLogs(jm.log, "INFO") as logs:
            self.assertEqual(self.run_wd(), {"Acme": ["Software Engineer"], "Beta": ["Software Engineer"]})
        self.assertIn("Workday refused a plain request (429); reading Workday through the browser", "\n".join(logs.output))
        # The plain API was asked once; the sites' own pages gave their job lists.
        self.assertEqual(self.posts, [(WD_API, 0, "api"), (WD_API, 0, "page"), (WD_BETA_API, 0, "page")])
        self.assertEqual(self.loads, ["https://acme.wd5.myworkdayjobs.com/en-US/External", "https://beta.wd1.myworkdayjobs.com/en-US/Careers"])
        [browser] = self.playwrights  # one browser for the run, a tab per site, all closed at the end
        self.assertEqual((browser.launched, len(browser.tabs), all(t.closed for t in browser.tabs)), (1, 2, True))
        self.assertEqual((browser.closed, browser.stopped), (True, True))
        self.assertEqual(browser.context_options["user_agent"], jm.HEADERS["User-Agent"])
        self.assertEqual(len(self.slept), 2)  # (a pause before each page but the first request)
        self.assertEqual(self.saved_state()[WD_SITE], [{"url": wd_link(2), "reported": "match"},
                                                       {"url": wd_link(1, "New Grad Software Engineer")}])

    def test_more_pages_are_asked_from_inside_the_page_the_way_the_page_asks(self):
        self.wd[WD_API] = [(i, "Software Engineer", "Posted Today") for i in range(45, 0, -1)]
        self.run_wd()
        self.run_wd()
        self.assertEqual(self.asked(), [(WD_API, 0), (WD_API, 0), (WD_API, 20), (WD_API, 40)])  # (api refused, page, 2 more)
        self.assertEqual(len(self.saved_state()[WD_SITE]), 45)
        self.assertEqual(self.fetch_headers[0]["x-calypso-csrf-token"], "t0k")  # the page's own headers
        self.assertNotIn("referer", self.fetch_headers[0])
        # If asking for more fails, what the first page gave is kept.
        self.wd["https://beta.wd1.myworkdayjobs.com/wday/cxs/beta/Careers/jobs"] = [(i, "Software Engineer", "Posted Today") for i in range(30)]
        self.pages[SIMPLIFY_URL] = json.dumps(json.loads(self.pages[SIMPLIFY_URL]) + [
            simplify_row("Beta", "Software Engineer", url="https://beta.wd1.myworkdayjobs.com/Careers/job/X/SWE_R1")])
        self.fail_offsets = {20}
        self.run_wd()  # (the list's new row: Beta joins the registry...)
        self.run_wd()  # (...and is read)
        self.assertEqual(len(self.saved_state()["https://beta.wd1.myworkdayjobs.com/Careers"]), 20)

    def test_a_site_with_a_bot_check_or_no_job_list_is_skipped_and_tried_again_later(self):
        self.two_sites()
        self.run_wd()
        self.browser_answers = {WD_API: "challenge", WD_BETA_API: "timeout"}
        with self.assertLogs(jm.log, "INFO") as logs:
            self.assertEqual(self.run_wd(), {})
        output = "\n".join(logs.output)
        self.assertIn("0 of 2 company Workday sites read in", output)
        self.assertIn("2 couldn't be read (tried again later)", output)
        self.assertIn("0 read, 0 unchanged since the last check, 0 not found, 2 could not be read this time", output)
        self.assertNotIn(WD_SITE, self.saved_state())  # nothing recorded
        self.assertEqual(len(self.loads), 2)
        self.assertTrue(all(t.closed for t in self.playwrights[0].tabs))
        self.browser_answers = {}
        self.wd[WD_API] = [(2, "Software Engineer", "Posted Today")]
        self.assertEqual(self.run_wd(), {"Acme": ["Software Engineer"], "Beta": ["Software Engineer"]})

    def test_after_three_sites_in_a_row_cant_be_read_workday_stops_for_the_run(self):
        self.two_sites("Gamma", "Delta")
        self.run_wd()
        for api in self.wd:
            self.browser_answers[api] = "challenge"
        with self.assertLogs(jm.log, "WARNING") as logs:
            self.run_wd()
        self.assertIn("Workday reading stops for this run: 3 Workday sites in a row couldn't be read", "\n".join(logs.output))
        self.assertEqual(len(self.loads), 3)
        self.assertIn("0 read, 3 couldn't be read (tried again later), 1 left for a later run (3 Workday sites in a row",
                      jm.boards_page_path().read_text())
        # The one left comes first next time.
        self.loads.clear()
        self.browser_answers = {}
        self.run_wd(workday_minutes_per_run=0)
        self.assertEqual(self.loads, [])  # (0 minutes: company Workday sites paused)
        self.run_wd()
        self.assertEqual(self.loads[0], "https://gamma.wd1.myworkdayjobs.com/en-US/Careers")  # (by name: Acme, Beta, Delta were tried)

    def test_boards_page_marks_sites_not_read_yet_and_those_that_failed_lately(self):
        self.two_sites("Gamma")
        self.run_wd()
        self.browser_answers = {WD_API: "challenge", WD_BETA_API: "unavailable"}
        self.run_wd()

        def row(name):
            [line] = [l for l in jm.boards_page_path().read_text().splitlines() if l.startswith(f"| {name} |")]
            return line.split(" | ")[1]

        # Tried but never read: still "not read yet" (and why); read: no note.
        self.assertTrue(row("Acme").endswith("(not read yet: couldn't be read at its last try)"), row("Acme"))
        self.assertTrue(row("Beta").endswith("(not read yet: couldn't be read at its last try)"), row("Beta"))
        self.assertTrue(row("Gamma").endswith(")"))
        self.assertNotIn("(", row("Gamma").split("](")[1].rstrip(")"))
        gamma_api = "https://gamma.wd1.myworkdayjobs.com/wday/cxs/gamma/Careers/jobs"
        self.browser_answers = {WD_BETA_API: "unavailable", gamma_api: "timeout"}
        self.run_wd()
        self.assertNotIn("(not", row("Acme"))
        self.assertTrue(row("Beta").endswith("(not read yet: couldn't be read at its last 2 tries)"), row("Beta"))
        self.assertTrue(row("Gamma").endswith("(couldn't be read at its last try)"), row("Gamma"))
        known = self.known()
        self.assertEqual([known[jm._url_key(f"https://{n}.wd1.myworkdayjobs.com/Careers")].get("failed_tries") for n in ("beta", "gamma")], [2, 1])
        self.assertNotIn("failed_tries", known[jm._url_key(WD_SITE)])

    def test_a_company_name_workday_doesnt_have_is_skipped_at_once_and_waits_a_day(self):
        # Workday answers a company name it doesn't have with a server error (500) that leads to its
        # "Workday is currently unavailable" page, the page it shows when it's down: so the site isn't
        # counted as gone, but it's left at once (no waiting for the time limit), and after two such
        # tries it waits at the back of the queue for a day.
        self.two_sites("Gamma", "Delta")
        self.run_wd()
        self.browser_answers = {WD_BETA_API: "unavailable"}
        reader = jm.workday.Reader(pause=0)
        reader.api_refused = True
        with self.assertLogs(jm.log, "WARNING") as logs:
            self.assertEqual(reader.read("https://beta.wd1.myworkdayjobs.com/Careers", quiet=False), ("skipped", None))
        reader.close()
        self.assertIn("skipped this run (the site answered with a server error (500): Workday may be down for a while, "
                      "or the company is no longer on Workday)", "\n".join(logs.output))
        self.assertEqual(self.asked(), [])  # (it never asked for its job list)
        # A page that goes on to another site and never asks for its job list says where it went.
        self.browser_answers = {WD_BETA_API: "elsewhere"}
        reader = jm.workday.Reader(pause=0)
        reader.api_refused = True
        with self.assertLogs(jm.log, "WARNING") as logs:
            reader.read("https://beta.wd1.myworkdayjobs.com/Careers", quiet=False)
        reader.close()
        self.assertIn("timed out after 15 s; the page had gone to community.workday.com/maintenance-page", "\n".join(logs.output))
        # In the runs: Beta is tried in its turn twice, then waits at the back for a day, not tried
        # again before then even when every other site has been read.
        self.browser_answers = {WD_BETA_API: "unavailable"}

        def run_order():
            self.loads.clear()
            self.run_wd()
            return [load.split("//")[1].split(".")[0] for load in self.loads]

        self.assertEqual(run_order(), ["acme", "beta", "delta", "gamma"])  # (never tried: by name)
        self.assertEqual(run_order(), ["acme", "beta", "delta", "gamma"])  # (all tried at the same time)
        self.assertEqual(run_order(), ["acme", "delta", "gamma"])  # (Beta couldn't be read twice: it waits a day)
        self.assertEqual(run_order(), ["acme", "delta", "gamma"])
        self.assertIn("3 read, 0 couldn't be read (tried again later), 1 left for a later run "
                      "(1 couldn't be read at its last tries and waits a day)", jm.boards_page_path().read_text())
        self.assertNotIn(jm._url_key("https://beta.wd1.myworkdayjobs.com/Careers"), self.saved_state()[jm.BOARDS_KEY]["missing"])
        # A day after its last try it takes its turn again (tried longest ago: first).
        known = self.saved_state()[jm.BOARDS_KEY]["known"]
        known[jm._url_key("https://beta.wd1.myworkdayjobs.com/Careers")]["tried_at"] = "2026-01-01T00:00:00"
        state = self.saved_state()
        state[jm.BOARDS_KEY]["known"] = known
        jm.STATE_PATH.write_text(json.dumps(state))
        self.loads.clear()
        self.run_wd()
        self.assertEqual(self.loads[0], "https://beta.wd1.myworkdayjobs.com/en-US/Careers")

    def test_sites_that_always_fail_come_round_together_without_stopping_the_run(self):
        # Sites that fail every time are tried in the same run, so they wait their day together and come
        # round together, side by side at the front: skipping them then doesn't count toward the 3 in a row.
        self.two_sites("Gamma", "Delta", "Epsilon")
        self.run_wd()
        dead = ("beta", "delta", "gamma")
        self.browser_answers = {f"https://{n}.wd1.myworkdayjobs.com/wday/cxs/{n}/Careers/jobs": "unavailable" for n in dead}

        def run_order():
            self.loads.clear()
            self.run_wd()
            return [load.split("//")[1].split(".")[0] for load in self.loads]

        run_order()
        run_order()
        self.assertEqual(run_order(), ["acme", "epsilon"])  # (the three wait their day at the back)
        state = self.saved_state()
        for n in dead:  # a day passes for them
            state[jm.BOARDS_KEY]["known"][jm._url_key(f"https://{n}.wd1.myworkdayjobs.com/Careers")]["tried_at"] = "2026-01-01T00:00:00"
        jm.STATE_PATH.write_text(json.dumps(state))
        with self.assertLogs(jm.log, "INFO") as logs:
            self.assertEqual(run_order(), ["beta", "delta", "gamma", "acme", "epsilon"])
        self.assertNotIn("Workday reading stops for this run", "\n".join(logs.output))
        self.assertIn("2 of 5 company Workday sites read", "\n".join(logs.output))
        # Other sites that can't be read still stop it after 3 in a row (Workday may be down), and a site
        # known to fail (its day up) between two of them, with a server error, neither counts nor clears the count.
        self.browser_answers = {api: "challenge" for api in self.wd}
        self.browser_answers[WD_BETA_API] = "unavailable"
        sites = [{"url": WD_SITE, "name": "Acme", "_board_key": "acme"}] + \
            [{"url": f"https://{n}.wd1.myworkdayjobs.com/Careers", "name": n, "_board_key": n} for n in ("beta", "delta", "gamma", "epsilon")]
        a_day_ago = "2026-01-01T00:00:00"
        known = {"acme": {"failed_tries": 1, "tried_at": a_day_ago}, "beta": {"failed_tries": 2, "tried_at": a_day_ago}}
        reader = jm.workday.Reader(pause=0)
        reader.api_refused = True
        results, summary = jm.workday.read_sites(reader, sites, {}, 5, known)
        reader.close()
        self.assertEqual(list(results), [s["url"] for s in sites[:4]])
        self.assertEqual((summary["left"], summary["why"][:28]), (1, "3 Workday sites in a row cou"))
        reader = jm.workday.Reader(pause=0)
        reader.api_refused = True
        self.assertEqual(len(jm.workday.read_sites(reader, sites, {}, 5)[0]), 3)  # (without the registry: Beta counts)
        reader.close()

    def test_a_bot_check_counts_toward_the_stop_even_from_sites_known_to_fail(self):
        # If Workday refuses the monitor for weeks, every site ends up failing its last tries. A bot check
        # still counts toward the 3 in a row, so each run still stops after 3 such pages.
        self.two_sites("Gamma", "Delta", "Epsilon")
        self.browser_answers = {api: "challenge" for api in self.wd}
        sites = [{"url": WD_SITE, "name": "Acme", "_board_key": "acme"}] + \
            [{"url": f"https://{n}.wd1.myworkdayjobs.com/Careers", "name": n, "_board_key": n} for n in ("beta", "delta", "gamma", "epsilon")]
        known = {s["_board_key"]: {"failed_tries": 7, "tried_at": "2026-01-01T00:00:00"} for s in sites}
        reader = jm.workday.Reader(pause=0)
        reader.api_refused = True
        with self.assertLogs(jm.log, "WARNING") as logs:
            results, summary = jm.workday.read_sites(reader, sites, {}, 5, known)
        reader.close()
        self.assertEqual((len(self.loads), len(results), summary["left"]), (3, 3, 2))
        self.assertIn("Workday reading stops for this run: 3 Workday sites in a row couldn't be read (last: Workday answered "
                      "with a bot check", "\n".join(logs.output))
        # A site still waiting its day isn't tried at all, even with nothing else left to read.
        self.loads.clear()
        now = datetime.now().isoformat(timespec="seconds")
        known = {s["_board_key"]: {"failed_tries": 2, "tried_at": now} for s in sites}
        reader = jm.workday.Reader(pause=0)
        reader.api_refused = True
        results, summary = jm.workday.read_sites(reader, sites, {}, 5, known)
        reader.close()
        self.assertEqual((self.loads, results, summary["left"]), ([], {}, 5))
        self.assertEqual(summary["why"], "5 couldn't be read at their last tries and wait a day")

    def test_no_browser_entry_is_read_after_the_workday_browser_opens(self):
        # While the Workday browser is open, fetch_browser can't start Playwright (boards/browser.py), so
        # every "browser" or "eightfold" entry must be read before Workday, wherever it is in the config.
        self.two_sites()
        opened = []  # whether a Workday browser was open at each fetch_browser call

        def fetch_browser(url, wait_for="", wait_seconds=8):
            opened.append(any(not playwright.stopped for playwright in self.playwrights))
            return page(("1", "Software Engineer")), []

        self.patch(jm, "fetch_browser", fetch_browser)
        mine = target(name="Acme Corp", url=WD_SITE + "?workerSubType=abc", mode="browser", link_selector="")
        zed = target(name="Zed", url="https://zed.example/careers", mode="browser")
        yon = target(name="Yon", url="https://yon.example/careers", mode="eightfold")
        self.run_wd(mine, zed, yon)  # (yours on Workday first in the config)
        self.run_wd(mine, zed, yon)
        self.assertEqual(opened, [False] * 4)
        self.assertEqual(len(self.playwrights), 2)  # (the Workday browser was opened in each run, after them)
        # The shipped config in the order a run reads it: nothing that needs fetch_browser after Workday.
        config = jm.load_config(REPO_DIR / "config_all.json")
        mine = [t for t in config["targets"] if not t.get("_section") and t.get("mode") != "boards"]
        boards = [{"name": "Acme", "url": WD_SITE, "_board": True}, {"name": "Zed", "url": GH_ACME, "_board": True}]
        ordered = jm._workday_last(mine + boards)
        on_workday = [i for i, t in enumerate(ordered) if jm._is_workday_entry(t) or (t.get("_board") and jm._board_platform(t["url"]) == "workday")]
        self.assertTrue(on_workday and any(t.get("mode") == "browser" and not jm._is_workday_entry(t) for t in mine))
        self.assertEqual([t["name"] for t in ordered[on_workday[0]:] if t.get("mode") in ("browser", "eightfold") and not jm._is_workday_entry(t)], [])
        self.assertEqual(on_workday, list(range(on_workday[0], len(ordered))))

    def test_a_site_that_is_gone_is_tried_again_a_day_later(self):
        self.run_wd()
        self.browser_answers = {WD_API: "missing"}
        self.run_wd()
        self.assertIn(WD_SITE, self.saved_state()[jm.BOARDS_KEY]["missing"])
        self.loads.clear()
        self.run_wd()
        self.assertEqual(self.loads, [])

    def test_the_browser_is_closed_even_when_the_run_fails(self):
        self.run_wd()
        self.patch(jm, "_finish_board_check", mock.Mock(side_effect=RuntimeError("something broke")))
        with self.assertRaises(RuntimeError):
            self.run_wd()
        [browser] = self.playwrights
        self.assertEqual((browser.launched, browser.closed, browser.stopped), (1, True, True))

    def test_a_browser_that_cannot_start_stops_workday_reading_for_the_run(self):
        self.two_sites()
        self.run_wd()
        self.browser_broken = True
        with self.assertLogs(jm.log, "WARNING") as logs:
            self.assertEqual(self.run_wd(), {})
        self.assertIn("Workday reading stops for this run: the browser couldn't start (RuntimeError: Executable doesn't exist",
                      "\n".join(logs.output))
        self.assertNotIn("Run playwright install", "\n".join(logs.output))  # (one line)
        self.assertEqual(self.asked("api"), [(WD_API, 0)])

    def test_a_page_whose_script_gets_stuck_is_left_and_the_next_site_gets_a_fresh_browser(self):
        # A stuck page can hold a call that has no time limit of its own (reading the job list's body)
        # forever: the watchdog kills Chromium GRACE_SECONDS past the page's limit, the site is skipped,
        # and the next site is read in a fresh Chromium.
        self.patch(browser_module, "GRACE_SECONDS", 0.5)
        self.patch(jm.workday, "PAGE_TIMEOUT_SECONDS", 0.5)
        self.wd[WD_BETA_API] = [(9, "Software Engineer", "Posted Today")]
        self.browser_answers[WD_API] = "stuck"
        reader = jm.workday.Reader(pause=0)
        reader.api_refused = True
        started = time.monotonic()
        with self.assertLogs(jm.log, "WARNING") as logs:
            self.assertEqual(reader.read(WD_SITE, quiet=False), ("skipped", None))
        self.assertLess(time.monotonic() - started, 5)
        self.assertIn("skipped this run (the browser didn't answer within 1 s, so it was stopped)", "\n".join(logs.output))
        self.assertEqual(self.killed, [1000])
        status, jobs = reader.read("https://beta.wd1.myworkdayjobs.com/Careers")
        self.assertEqual((status, len(jobs)), ("ok", 1))
        reader.close()
        [playwright] = self.playwrights
        self.assertEqual((playwright.launched, self.killed, reader.stopped), (2, [1000], None))  # (the fresh one wasn't killed)
        self.assertTrue(all(tab.waited_for == ["requestfinished"] for tab in playwright.tabs))  # (the job list in full)
        self.assertEqual((playwright.closed, playwright.stopped), (True, True))

    def test_a_page_that_gets_stuck_when_asked_for_more_keeps_its_first_page(self):
        self.patch(browser_module, "GRACE_SECONDS", 0.5)
        self.patch(jm.workday, "API_TIMEOUT_SECONDS", 0.5)
        self.wd[WD_API] = [(i, "Software Engineer", "Posted Today") for i in range(45, 0, -1)]
        self.wd[WD_BETA_API] = [(9, "Software Engineer", "Posted Today")]
        self.browser_answers[WD_API] = "stuck asking for more"
        reader = jm.workday.Reader(pause=0)
        reader.api_refused = True
        started = time.monotonic()
        status, jobs = reader.read(WD_SITE)
        self.assertEqual((status, len(jobs), self.killed), ("ok", 20, [1000]))
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(reader.read("https://beta.wd1.myworkdayjobs.com/Careers")[0], "ok")
        reader.close()
        self.assertEqual((self.playwrights[0].launched, self.killed), (2, [1000]))

    def test_the_browser_module_leaves_a_bot_check_at_once(self):
        test = self

        class Doc:
            status, headers = 429, {"cf-mitigated": "challenge"}

        tab = _FakeTab(self)
        tab.goto = lambda url, **kw: (test.loads.append(url), Doc())[1]
        browser = RealBrowser(start=lambda: mock.Mock(chromium=mock.Mock(launch=lambda **kw: mock.Mock(
            new_context=lambda **kw: mock.Mock(new_page=lambda: tab)))))
        loaded = browser.open("https://acme.wd5.myworkdayjobs.com/en-US/External", lambda url, method: True, 15)
        self.assertEqual((loaded.status, loaded.detail, tab.closed), ("challenge", "429, challenge", True))
        browser.close()
        browser.close()  # (again: nothing happens)


class WorkdayPlaceTests(WorkdayTestCase):
    """A posting whose place the list doesn't give ("2 Locations", or nothing): Workday's page for
    it is asked, for those the email would list, so one outside the US isn't emailed."""

    AUSTRALIA = {"location": "Homebush", "additionalLocations": ["Brisbane"], "country": {"descriptor": "Australia"}}

    def detail(self, rid):
        return f"https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External/job/New-York-NY/Software-Engineer_R{rid}"

    def test_postings_the_list_doesnt_place_are_asked_and_kept_out_when_outside_the_us(self):
        for refused in (True, False):
            with self.subTest(refused=refused):
                jm.STATE_PATH.unlink(missing_ok=True)
                self.detail_asks.clear()
                self.api_refuses = refused
                self.wd[WD_API] = []
                self.run_wd()  # (the site joins the registry: its first read records nothing)
                self.details = {
                    6: self.AUSTRALIA,
                    5: {"location": "Sydney", "additionalLocations": ["Tampa, FL"], "country": {"descriptor": "Australia"}},
                    4: {"location": "Remote", "jobRequisitionLocation": {"country": {"descriptor": "United States of America"}}},
                    3: {"location": "London", "country": {"descriptor": "United Kingdom"}},
                }
                self.wd[WD_API] = [(6, "Software Engineer", "Posted Today", "2 Locations"),
                                   (5, "Software Developer", "Posted Today", "2 Locations"),
                                   (4, "ML Engineer", "Posted Today", ""),
                                   (3, "Infrastructure Engineer", "Posted Today", ""),
                                   (2, "Sales Manager", "Posted Today", "2 Locations"),
                                   (7, "Backend Software Engineer", "Posted Today", "Austin, TX")]
                if not refused:  # (plain requests: the posting's page is asked the same way)
                    for rid, info in self.details.items():
                        self.pages[self.detail(rid).replace("Software-Engineer", {6: "Software-Engineer", 5: "Software-Developer",
                                                            4: "ML-Engineer", 3: "Infrastructure-Engineer"}[rid])] = \
                            json.dumps({"jobPostingInfo": info})
                reported = self.run_wd()
                # Australia (and Brisbane) and London aren't emailed; Australia with a US city is, and so is the US.
                self.assertEqual(sorted(reported.get("Acme", [])), ["Backend Software Engineer", "ML Engineer", "Software Developer"])
                if refused:  # (asked from inside the site's page; no other posting's page)
                    self.assertEqual([how for _, how in self.detail_asks], ["fetch"] * 4)
                    self.assertEqual({u.rsplit("_R", 1)[1] for u, _ in self.detail_asks}, {"6", "5", "4", "3"})
                    self.assertNotIn("content-type", self.fetch_headers[-1])
                else:
                    asked = [u for u, _ in self.requests_made if "/job/" in u]
                    self.assertEqual({u.rsplit("_R", 1)[1] for u in asked}, {"6", "5", "4", "3"})
                # Recorded as seen either way, and not asked again.
                self.assertEqual(len(self.saved_state()[WD_SITE]), 6)
                asks = len(self.detail_asks) + len(self.requests_made)
                self.assertEqual(self.run_wd(), {})
                self.assertEqual(len(self.detail_asks) + len([u for u, _ in self.requests_made]), asks + 1)  # (the feed only)
                self.requests_made.clear()

    def test_a_posting_whose_page_cant_be_had_is_kept(self):
        self.api_refuses = True
        self.wd[WD_API] = []
        self.run_wd()
        self.wd[WD_API] = [(6, "Software Engineer", "Posted Today", "2 Locations")]  # (no page: 404)
        self.assertEqual(self.run_wd(), {"Acme": ["Software Engineer"]})
        self.assertEqual(len(self.detail_asks), 1)

    def test_at_most_a_few_postings_are_asked_per_site(self):
        self.api_refuses = True
        self.patch(jm.workday, "PLACES_PER_SITE", 2)
        self.wd[WD_API] = []
        self.run_wd()
        self.details = {rid: self.AUSTRALIA for rid in (7, 8, 9)}  # (R1 is the feed's)
        self.wd[WD_API] = [(rid, "Software Engineer", "Posted Today", "2 Locations") for rid in (9, 8, 7)]
        self.assertEqual(self.run_wd(), {"Acme": ["Software Engineer"]})  # (the third wasn't asked: kept)
        self.assertEqual(len(self.detail_asks), 2)

    def test_old_postings_and_your_entries_postings_the_filters_drop_arent_asked(self):
        self.api_refuses = True
        mine = target(name="Acme Corp", url=WorkdayEntryTests.MINE, mode="browser", link_selector="")
        self.mark_known(WorkdayEntryTests.MINE)
        self.wd[WD_API] = []
        self.run_wd(mine, boards=False)
        self.details = {7: self.AUSTRALIA, 8: self.AUSTRALIA, 9: self.AUSTRALIA}
        self.wd[WD_API] = [(9, "Senior Software Engineer", "Posted Today", "2 Locations"),  # (left out: senior)
                           (8, "Software Engineer", "Posted 30+ Days Ago", "2 Locations"),  # (not new: too old)
                           (7, "Software Engineer", "Posted Today", "2 Locations")]
        self.assertEqual(self.run_wd(mine, boards=False), {})  # (the one asked is in Australia)
        self.assertEqual([u.rsplit("_R", 1)[1] for u, _ in self.detail_asks], ["7"])

    def test_asking_stops_after_two_failures_in_a_row_and_never_stops_the_site(self):
        self.api_refuses = True
        self.wd[WD_API] = []
        self.run_wd()
        self.wd[WD_API] = [(rid, "Software Engineer", "Posted Today", "2 Locations") for rid in (9, 8, 7)]  # (no pages: 404)
        self.assertEqual(self.run_wd(), {"Acme": ["Software Engineer"] * 3})
        self.assertEqual(len(self.detail_asks), 2)
        # A choice that breaks (a bug) leaves the postings as the list gives them; the site is still read.
        self.wd[WD_API] = [(rid, "Software Engineer", "Posted Today", "2 Locations") for rid in (12, 11, 10)]
        self.patch(jm, "place_unclear", mock.Mock(side_effect=RuntimeError("bug")))
        self.assertEqual(self.run_wd(), {"Acme": ["Software Engineer"] * 3})
        self.assertEqual(len(self.detail_asks), 2)

    def test_asking_stops_when_the_sites_time_for_it_is_up(self):
        self.api_refuses = True
        clock = [0.0]
        self.patch(jm.workday, "_monotonic", lambda: clock[0])
        self.patch(jm.workday, "_sleep", lambda s: clock.__setitem__(0, clock[0] + max(s, 25)))  # (each ask: 25 s)
        self.wd[WD_API] = []
        self.run_wd()
        self.details = {rid: self.AUSTRALIA for rid in range(7, 17)}
        self.wd[WD_API] = [(rid, "Software Engineer", "Posted Today", "2 Locations") for rid in range(16, 6, -1)]
        self.run_wd()
        self.assertLessEqual(len(self.detail_asks), 3)  # (60 s: 3 asks at most, not 10)

    def test_postings_arent_asked_without_the_us_filter(self):
        self.assertIsNone(jm._workday_place_wanted({}, [], None, us_only=False))
        self.assertIsNone(jm._workday_place_wanted({"us_only": False}, [], None, us_only=True))
        self.assertIsNotNone(jm._workday_place_wanted({}, [], None, us_only=True))

    def test_what_workdays_page_says_decides(self):
        located = jm.workday.located
        cases = [  # (what the page says, where the posting is noted, outside the US)
            (self.AUSTRALIA, "Homebush; Brisbane", True),
            ({"location": "Sydney", "additionalLocations": ["Tampa, FL"], "country": {"descriptor": "Australia"}}, "Sydney; Tampa, FL", False),
            ({"location": "Annapolis Junction, MD", "additionalLocations": ["McLean, VA"],
              "country": {"descriptor": "United States of America"}}, "Annapolis Junction, MD; McLean, VA", False),
            ({"location": "London", "jobRequisitionLocation": {"country": {"descriptor": "United Kingdom"}}}, "London", True),
            ({"location": "Somewhere"}, "Somewhere", False),  # (no country: the text decides, as before)
            ({"location": "San Juan", "country": {"descriptor": "Puerto Rico"}}, "San Juan", False),  # (in the US)
            ({"location": "Toronto", "additionalLocations": [f"Place {i}" for i in range(40)] + ["Austin, TX"],
              "country": {"descriptor": "Canada"}}, "; ".join(["Toronto"] + [f"Place {i}" for i in range(40)] + ["Austin, TX"]), False),
            (None, "2 Locations", False),  # (no page: kept)
            ({"other": 1}, "2 Locations", False),
        ]
        for info, where, outside in cases:
            with self.subTest(info=info):
                job = located({"title": "Software Engineer", "url": "u", "location": "2 Locations"},
                              None if info is None else {"jobPostingInfo": info} if "other" not in (info or {}) else info)
                self.assertEqual((job["location"], jm.is_outside_us(job)), (where, outside))
        self.assertTrue(jm.place_unclear({"location": "2 Locations"}))
        self.assertTrue(jm.place_unclear({"location": ""}))
        self.assertTrue(jm.place_unclear({"location": "Kronberg Campus, Kronberg"}))
        self.assertFalse(jm.place_unclear({"location": "Austin, TX"}))
        self.assertFalse(jm.place_unclear({"location": "London"}))  # (the US filter already knows)
        self.assertFalse(jm.place_unclear({"location": "2 Locations", "primary_country": "India"}))


class WorkdayEntryTests(WorkdayTestCase):
    """Your own entries on a Workday site (like NVIDIA's in targets/big_tech.json)."""

    MINE = "https://acme.wd5.myworkdayjobs.com/en-US/External?redirect=/en-US/External/userHome&workerSubType=abc"

    def mine(self, **extra):
        return target(name="Acme Corp", url=self.MINE, **{"mode": "browser", "link_selector": "", **extra})

    def test_your_entry_is_read_with_its_filters_whatever_its_mode(self):
        for mode in ("api", "html", "browser"):
            for refused in (False, True):
                with self.subTest(mode=mode, refused=refused):
                    jm.STATE_PATH.unlink(missing_ok=True)
                    self.posts.clear()
                    self.loads.clear()
                    self.api_refuses = refused
                    self.wd[WD_API] = [(2, "Software Engineer", "Posted Today")]
                    self.mark_known(self.MINE)
                    self.assertEqual(self.run_wd(self.mine(mode=mode), boards=False), {"Acme Corp": ["Software Engineer"]})
                    if refused:
                        self.assertEqual(self.loads, ["https://acme.wd5.myworkdayjobs.com/en-US/External?workerSubType=abc"])
                    else:
                        self.assertEqual((self.loads, self.asked()), ([], [(WD_API, 0)]))
                    self.assertEqual((self.bodies[-1]["appliedFacets"], self.bodies[-1]["searchText"]), ({"workerSubType": ["abc"]}, ""))
        # Its location is kept, for the email.
        self.assertEqual(self.saved_state()[self.MINE][0]["location"], "New York, NY")

    def test_a_page_that_ignores_your_filters_is_asked_with_them(self):
        self.api_refuses = True
        self.browser_answers[WD_API] = "ignores filters"
        self.wd[WD_API] = [(2, "Software Engineer", "Posted Today")]
        self.mark_known(self.MINE)
        self.run_wd(self.mine(), boards=False)
        self.assertEqual(self.asked(), [(WD_API, 0), (WD_API, 0), (WD_API, 0)])  # refused, the page's own, then with the filters
        self.assertEqual([via for _, _, via in self.posts], ["api", "page", "fetch"])

    def test_postings_keep_the_links_they_were_recorded_with(self):
        # Recorded by earlier versions: through the browser (links with the page's filters after "?")
        # and through the API (without); the same postings now must not count as new.
        with_filters = wd_link(1) + "?workerSubType=abc"
        jm.save_state({self.MINE: [{"title": "Software Engineer", "url": with_filters},
                                   {"title": "ML Engineer", "url": wd_link(2, "ML Engineer")}]})
        self.wd[WD_API] = [(3, "Software Engineer III", "Posted Today"), (2, "ML Engineer", "Posted Yesterday"),
                           (1, "Software Engineer", "Posted 2 Days Ago")]
        for refused in (True, False):
            with self.subTest(refused=refused):
                self.api_refuses = refused
                before = self.saved_state()[self.MINE]
                reported = self.run_wd(self.mine(), boards=False)
                self.assertEqual(reported, {"Acme Corp": ["Software Engineer III"]} if refused else {})
                saved = self.saved_state()[self.MINE]
                ids = [jm.compute_job_id(j) for j in saved]
                self.assertEqual(len(ids), len(set(ids)))
                self.assertEqual([j["url"] for j in saved], [wd_link(3, "Software Engineer III"), with_filters, wd_link(2, "ML Engineer")])
                self.assertEqual(saved[1:], before[-2:])  # (the old records as they were)

    def test_an_old_posting_your_entry_had_not_seen_is_not_new(self):
        self.mark_known(self.MINE)
        self.wd[WD_API] = [(3, "Software Engineer", "Posted Today"), (2, "ML Engineer", "Posted 30+ Days Ago")]
        self.assertEqual(self.run_wd(self.mine(), boards=False), {"Acme Corp": ["Software Engineer"]})

    def test_a_posting_a_list_already_gave_you_is_not_emailed_again_by_your_entry_and_the_other_way_round(self):
        self.pages[SIMPLIFY_URL] = json.dumps([simplify_row("Acme", "Software Engineer, Data", url=wd_link(5, "Software Engineer, Data") + "?utm_source=Simplify")])
        self.mark_known(SIMPLIFY_URL, self.MINE)
        self.assertEqual(self.run_wd(self.mine(), boards=False), {"Acme (via SimplifyJobs New Grad)": ["Software Engineer, Data"]})
        self.wd[WD_API] = [(6, "Software Engineer, Platform", "Posted Today"), (5, "Software Engineer, Data", "Posted Today")]
        self.assertEqual(self.run_wd(self.mine(), boards=False), {"Acme Corp": ["Software Engineer, Platform"]})
        rows = json.loads(self.pages[SIMPLIFY_URL])
        self.pages[SIMPLIFY_URL] = json.dumps(rows + [simplify_row("Acme", "Software Engineer, Platform", url=wd_link(6, "Software Engineer, Platform"))])
        self.assertEqual(self.run_wd(self.mine(), boards=False), {})

    def test_a_posting_whose_link_workday_changed_is_not_emailed_again(self):
        self.mark_known(self.MINE)
        self.wd[WD_API] = [(70, "Software Engineer II", "Posted Today")]
        self.assertEqual(self.run_wd(self.mine(), boards=False), {"Acme Corp": ["Software Engineer II"]})
        # The same job number under a new link (an edited title changes the path before "_R70").
        self.wd[WD_API] = [(70, "Software Engineer II (Backend)", "Posted Today")]
        self.assertEqual(self.run_wd(self.mine(), boards=False), {})
        self.wd[WD_API].insert(0, (71, "Software Engineer III", "Posted Today"))
        self.assertEqual(self.run_wd(self.mine(), boards=False), {"Acme Corp": ["Software Engineer III"]})

    def test_your_entry_that_workday_refuses_is_reported_like_any_entry(self):
        self.api_refuses = True
        self.browser_answers[WD_API] = "challenge"
        self.mark_known(self.MINE)
        with self.assertLogs(jm.log, "WARNING") as logs:
            self.run_wd(self.mine(), boards=False)
        self.assertIn("Acme Corp: skipped this run (Workday answered with a bot check or refusal (403, challenge))", "\n".join(logs.output))
        record = self.saved_state()[jm.HEALTH_KEY][self.MINE]
        self.assertEqual((record["kind"], record["why"]), ("failed", "the site is refusing access (403)"))
        self.assertEqual(self.saved_state()[self.MINE], [])  # (nothing recorded)

    def test_your_entries_are_read_before_the_company_sites_and_outside_their_time(self):
        self.two_sites()
        self.run_wd(self.mine())
        self.posts.clear()
        self.run_wd(self.mine(), workday_minutes_per_run=0)
        self.assertEqual(self.asked(), [(WD_API, 0)])  # yours, even with no time for company sites
        self.assertNotIn(WD_SITE, self.saved_state())  # (Acme's site is your entry's: not checked twice)


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

    def test_a_workday_requisition_recorded_without_being_shown_hides_no_other_city(self):
        # Workday posts one job number in several cities ("-1" for the next one). A Dublin
        # posting the entry saved without showing (outside the US) mustn't hide the San
        # Francisco one; once a posting of that number has been shown, the others are repeats.
        site = "https://circle.wd1.myworkdayjobs.com/en-US/Circle"
        dublin = {"title": "Software Engineer", "url": f"{site}/job/Dublin/Software-Engineer_JR999999"}
        sf = {"title": "Software Engineer", "url": f"{site}/job/San-Francisco-CA/Software-Engineer_JR999999-1"}
        for current, others in ((site, set()), ("board", {"board"})):  # an entry, a company site
            with self.subTest(current=current):
                state = {current: [dublin, sf]}
                self.assertEqual(jm._drop_repeats([sf], state, current, set(), others, match_titles=False), [sf])
                state = {current: [{**dublin, jm.REPORTED_MARK: jm.SHOWN_AS_MATCH}, sf]}
                self.assertEqual(jm._drop_repeats([sf], state, current, set(), others, match_titles=False), [])


class ForgetClosedPostingsTests(MonitorTestCase):
    """A company job board's posting that has closed is remembered FORGET_GONE_DAYS, then forgotten,
    so state.json holds open roles and recently closed ones rather than every role a company had."""

    def board_target(self, url, jobs):
        return {"name": "Acme", "url": url, "mode": "api", "_board": True, "_compact": True, "_quiet": True,
                "_prefetched": jobs, "_forget_gone_days": jm.FORGET_GONE_DAYS}

    def on(self, day):
        self.patch(jm, "_today", lambda: day)

    def test_a_closed_posting_is_forgotten_after_sixty_days_and_one_back_in_time_is_kept(self):
        url = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
        job = lambda n: {"title": "Software Engineer", "url": f"https://boards.greenhouse.io/acme/jobs/{n}"}  # noqa: E731
        state = {}
        self.on("2026-11-20")
        jm.check_target(self.board_target(url, [job(1), job(2), job(3)]), state, [], None)
        self.assertEqual([r.get(jm.GONE_KEY) for r in state[url]], [None, None, None])
        # 2 and 3 close; 2 comes back a month later; 3 stays closed.
        jm.check_target(self.board_target(url, [job(1)]), state, [], None)
        self.assertEqual({r["url"][-1]: r.get(jm.GONE_KEY) for r in state[url]}, {"1": None, "2": "2026-11-20", "3": "2026-11-20"})
        self.on("2026-12-20")
        new, _ = jm.check_target(self.board_target(url, [job(1), job(2)]), state, [], None)
        self.assertEqual(new, [])  # (back, not new)
        self.assertEqual({r["url"][-1]: r.get(jm.GONE_KEY) for r in state[url]}, {"1": None, "2": None, "3": "2026-11-20"})
        self.on("2027-01-18")  # (59 days)
        jm.check_target(self.board_target(url, [job(1), job(2)]), state, [], None)
        self.assertEqual(sorted(r["url"][-1] for r in state[url]), ["1", "2", "3"])
        self.on("2027-01-19")  # (60 days: forgotten)
        jm.check_target(self.board_target(url, [job(1), job(2)]), state, [], None)
        self.assertEqual(sorted(r["url"][-1] for r in state[url]), ["1", "2"])
        # A posting still listed is never forgotten, however long ago it was first seen.
        self.on("2028-01-01")
        jm.check_target(self.board_target(url, [job(1), job(2)]), state, [], None)
        self.assertEqual(sorted(r["url"][-1] for r in state[url]), ["1", "2"])

    def test_a_posting_emailed_to_you_is_never_forgotten(self):
        # Closed for a season and reopened with the same link: it was emailed once, so it must not be again.
        url = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
        posting = {"title": "Software Engineer, New Grad", "url": "https://boards.greenhouse.io/acme/jobs/4001234"}
        state = {url: [{"url": posting["url"], jm.REPORTED_MARK: jm.SHOWN_AS_MATCH},
                       {"url": "https://boards.greenhouse.io/acme/jobs/5", jm.EARLIER_COMPANY_MARK: True,
                        jm.REPORTED_MARK: jm.SHOWN_AS_MATCH}]}
        self.on("2026-11-20")
        jm.check_target(self.board_target(url, []), state, [], None)
        self.on("2027-08-15")
        jm.check_target(self.board_target(url, []), state, [], None)
        self.assertEqual(len(state[url]), 2)
        new, _ = jm.check_target(self.board_target(url, [posting]), state, [], None)
        self.assertEqual(new, [])

    def test_without_forget_days_nothing_is_marked_or_forgotten(self):
        old = [{"url": "https://x.example/1"}, {"url": "https://x.example/2"}]
        self.on("2030-01-01")
        merged = jm.merge_jobs_new_first(old, [{"url": "https://x.example/1"}], 1000)
        self.assertEqual(merged, old)  # (Workday sites, SmartRecruiters, your entries and the lists: as before)
        merged = jm.merge_jobs_new_first(old + [{"url": "https://x.example/3", jm.GONE_KEY: "2000-01-01"}],
                                         [{"url": "https://x.example/1"}], 1000)
        self.assertEqual(len(merged), 3)

    def test_only_boards_whose_whole_list_is_read_forget(self):
        known = {
            "greenhouse:acme": {"name": "Acme", "url": "https://boards-api.greenhouse.io/v1/boards/acme/jobs"},
            "ashby:beta": {"name": "Beta", "url": "https://api.ashbyhq.com/posting-api/job-board/beta"},
            "lever:gamma": {"name": "Gamma", "url": "https://api.lever.co/v0/postings/gamma"},
            "sr:delta": {"name": "Delta", "url": "https://api.smartrecruiters.com/v1/companies/delta/postings"},
            "wd:eps": {"name": "Eps", "url": "https://eps.wd1.myworkdayjobs.com/Careers"},
        }
        meta = {"known": known, "list_etags": {}}
        boards = jm.plan_boards({"platforms": ["greenhouse", "ashby", "lever", "smartrecruiters", "workday"]}, [], {}, meta, False)
        self.assertEqual({b["name"]: b.get("_forget_gone_days") for b in boards},
                         {"Acme": 60, "Beta": 60, "Gamma": 60, "Delta": None, "Eps": None})

    def test_state_json_is_written_compactly(self):
        jm.save_state({"https://x.example/board": [{"url": "https://x.example/1"}]})
        text = jm.STATE_PATH.read_text()
        self.assertNotIn("\n ", text)
        self.assertIn('[{"url":"https://x.example/1"}]', text)
        self.assertEqual(jm.load_state()["https://x.example/board"], [{"url": "https://x.example/1"}])


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
        shutil.copytree(REPO_DIR / "boards", self.tmp / "boards", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy(REPO_DIR / "sources.py", self.tmp / "sources.py")

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

        # A new posting with SMTP down: reported, exit 1, and kept for the next email.
        self.handler.body = page(*jobs_named("Data Analyst", "Quant Researcher", "ML Engineer")).encode()
        result = self.run_script(unreachable)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("ML Engineer", result.stdout)
        state = json.loads((self.tmp / "state.json").read_text())
        self.assertEqual([j["title"] for j in state[jm.ALERT_POOL_KEY]], ["ML Engineer"])

        # Delivered on the next run, even though the page no longer lists it.
        self.handler.body = page(*jobs_named("Data Analyst", "Quant Researcher")).encode()
        result = self.run_script({"enabled": False})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ML Engineer", result.stdout)
        state = json.loads((self.tmp / "state.json").read_text())
        self.assertEqual(len(state[self.page_url]), 3)
        self.assertNotIn(jm.ALERT_POOL_KEY, state)

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
        state = json.loads((self.tmp / "state.json").read_text())
        self.assertEqual(len(state[jm.ALERT_POOL_KEY]), 2)  # waiting for an email that works

        result = self.run_script({"enabled": False})
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads((self.tmp / "state.json").read_text())
        self.assertEqual(len(state[self.page_url]), 2)
        self.assertNotIn(jm.ALERT_POOL_KEY, state)


if __name__ == "__main__":
    unittest.main()
