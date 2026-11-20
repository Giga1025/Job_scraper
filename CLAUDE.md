# Working on this repo

A personal job monitor: GitHub Actions runs `job_monitor.py` every 30 minutes and emails new US
new-grad SDE/ML/AI/infra/backend roles. The goal: cover so many companies that LinkedIn and
Jobright aren't needed. See `README.md` (how it works) and `TODO.md` (what's next).

## The owner's standing rules

- **Coverage first, always.** When a choice trades coverage (more companies, more sites read,
  more postings found) against speed, frequency, shorter runs or any other shortcut, choose
  coverage. Use the time a run has rather than leaving it idle (Workday gets the rest of each
  run for this reason).
- **Don't break what already works, and verify every change**: the test suite on Python 3.11,
  3.12 and 3.13 (`python -m unittest discover -s tests`), and a dry run on GitHub
  (`workflow_dispatch` with `dry_run: true`, which emails nothing and saves to
  `monitor-state-test`) for anything that changes what a run does or how long it takes.
- **Keep it modular**: a new job platform goes in its own module under `boards/`, not into
  `job_monitor.py`.
- **Brief the owner after every change**: what changed, which files, how many lines, the tests.
- **Public repo**: recipient addresses stay in the `RECIPIENT_EMAIL` secret; never commit an
  email address, token or other secret.
- The owner approved the headless browser (plain Playwright Chromium, as for Workday) and does not
  require robots.txt. No stealth plugins, proxies or CAPTCHA solving.
- `skip_companies` is not applied to the speedyapply and Simplify lists.
