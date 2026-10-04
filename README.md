# Job Page Monitor v2

Monitors company career pages for new job postings and sends email alerts.  
Supports **JavaScript-rendered pages** (Microsoft, Google, Meta, etc.) via headless browser.

## Quick Start

### 1. Install dependencies

```bash
pip install requests beautifulsoup4 lxml playwright
playwright install chromium
```

The `playwright install chromium` step downloads a headless Chromium browser (~150MB one-time download).

### 2. Pick a config file

The repo ships `config_all.json` (about 60 companies). Which file is used:

- `--config <file>` (or the `JOB_MONITOR_CONFIG` environment variable) always wins. A relative
  path is relative to the folder containing `job_monitor.py`. If the file doesn't exist, the
  monitor stops with an error and changes nothing — except `--config config.json`, which
  writes a starter `config.json` for you to edit (it never overwrites an existing one).
- Without `--config`: `config_all.json` if it exists, otherwise `config.json`. If you have both,
  `config.json` is ignored (a warning says so) — pass `--config config.json` to use it.
- If neither exists, the first run writes a starter `config.json` and exits so you can edit it.

Run periodically from the same process:

```bash
python job_monitor.py --config config_all.json --interval-minutes 15
```

or, on Linux/HPC (uses `.venv` from `./setup.sh` if present):

```bash
./run_periodic_monitor.sh config_all.json 15
```

### 3. Edit your config

Adjust the URL, add more targets, and set up email:

```json
{
  "email": {
    "enabled": true,
    "smtp_server": "smtp.gmail.com",
    "smtp_port": 587,
    "sender_email": "you@gmail.com",
    "sender_password": "abcd efgh ijkl mnop",
    "recipient_email": "you@gmail.com"
  },
  "keyword_filters": ["analyst", "engineer", "data", "quant"],
  "targets": [
    {
      "name": "Microsoft — US Remote Entry Level",
      "url": "https://apply.careers.microsoft.com/careers?start=0&location=United+States&sort_by=timestamp&filter_include_remote=1&filter_seniority=Entry%20Level",
      "mode": "browser",
      "wait_for": "a[href*='/careers/job/']",
      "link_selector": "a[href*='/careers/job/']"
    },
    {
      "name": "Stripe",
      "url": "https://stripe.com/jobs/search",
      "mode": "browser",
      "wait_for": "a[href*='/jobs/listing/']",
      "link_selector": "a[href*='/jobs/listing/']"
    }
  ]
}
```

### 4. Run it

```bash
python job_monitor.py
```

The first check of each target is a silent baseline: it records the jobs already listed
(a first check that finds no jobs doesn't count, since that's usually a page glitch).
From the next run on, only postings that weren't there before are alerted.

---

## Choosing which roles you're alerted about

Job titles vary a lot between companies ("Member of Technical Staff", "Technology Analyst",
"Forward Deployed Engineer"), so a list of titles to *look for* misses roles. Instead,
`role_filter` in the config lists what to *leave out*: every new posting is kept unless its
title clearly says it is senior (`exclude_senior`), an internship (`exclude_internships`),
pure frontend (`exclude_frontend`, unless also full stack/platform), non-engineering
(`exclude_non_engineering`, unless an `engineering_signals` word is present) or
non-software engineering (`exclude_other_disciplines`, unless a `software_signals` word is
present). Matching is whole-word and case-insensitive. Edit the lists to taste; remove
`role_filter` to turn it off.

Left-out postings are not dropped silently. They're listed in a separate "Left out by your
filters" section at the bottom of the next alert email (with the reason), or in a digest email
if there has been no alert for `filtered_digest_hours` (default 24). They're only marked as seen
once an email has listed them. Set `"show_filtered": false` to drop them quietly instead.

`keyword_filters` (global, or per target) still works as an allow-list; postings it leaves out
are listed the same way.

---

## Target Configuration

Each target has these fields:

| Field | Required | Description |
|---|---|---|
| `name` | Yes | Friendly label (used in alerts) |
| `url` | Yes | Career page URL with your filters applied |
| `mode` | Yes | `"html"` for static pages, `"browser"` for JS-heavy pages |
| `wait_for` | No | CSS selector to wait for before scraping (browser mode only) |
| `link_selector` | No | CSS selector for job links. If empty, uses heuristics |

### How to figure out `link_selector` and `wait_for`

1. Open the career page in Chrome
2. Right-click a job listing link → Inspect
3. Look at the `<a href="...">` tag — what does the href look like?
4. Build a selector from the pattern, e.g.:
   - Microsoft: `a[href*='/careers/job/']`
   - Stripe: `a[href*='/jobs/listing/']`
   - Greenhouse-based sites: `a[href*='/jobs/']`
5. Use the same selector for both `wait_for` and `link_selector`

### Pre-built selectors for popular companies

```json
// Microsoft
"wait_for": "a[href*='/careers/job/']",
"link_selector": "a[href*='/careers/job/']"

// Google
"wait_for": "a[href*='jobs/results']",
"link_selector": "a[href*='jobs/results']"

// Amazon
"wait_for": "a[href*='/job/']",
"link_selector": "a[href*='/job/']"

// Greenhouse-based (used by many startups)
"wait_for": "a[href*='boards.greenhouse.io']",
"link_selector": "a[href*='boards.greenhouse.io']"

// Lever-based
"wait_for": "a[href*='jobs.lever.co']",
"link_selector": "a[href*='jobs.lever.co']"
```

---

## Email Setup (Gmail)

1. Go to https://myaccount.google.com/security
2. Enable 2-Step Verification
3. Go to https://myaccount.google.com/apppasswords
4. Create an app password — copy the 16-character code
5. Paste it into config.json as `sender_password`
6. Set `enabled` to `true`

For Outlook: use `smtp-mail.outlook.com` port `587`.

### Keeping credentials out of the config file

Any of these environment variables is used when the matching config value is empty,
so you can leave them blank in a tracked config such as `config_all.json`:

| Variable | Config field | Example |
|---|---|---|
| `SENDER_EMAIL` | `sender_email` | `you@gmail.com` |
| `SENDER_PASSWORD` | `sender_password` | the 16-character app password |
| `RECIPIENT_EMAIL` | `recipient_email` | `you@gmail.com, friend@example.com` |

If an email can't be sent, the new postings aren't marked as seen: they're printed,
the error is logged, and they're emailed again on the next run.

---

## Scheduling

### macOS / Linux (cron)

```bash
crontab -e
```

Add:

```
0 */6 * * * cd /path/to/job_monitor_v2 && python3 job_monitor.py >> cron.log 2>&1
```

Common schedules:
- `0 9 * * *` — daily at 9 AM
- `0 */6 * * *` — every 6 hours
- `0 9 * * 1-5` — weekdays at 9 AM

### Windows (Task Scheduler)

1. Open Task Scheduler → Create Basic Task
2. Trigger: Daily at 9:00 AM
3. Action: Start a Program
4. Program: `python` | Arguments: `job_monitor.py` | Start in: `C:\path\to\job_monitor_v2`

### GitHub Actions (free, runs in the cloud)

Create `.github/workflows/monitor.yml`:

```yaml
name: Job Monitor
on:
  schedule:
    - cron: '0 */6 * * *'
  workflow_dispatch:

permissions:
  contents: write  # needed to push state.json back; without it every run is a silent baseline

jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - run: |
          pip install requests beautifulsoup4 lxml playwright
          playwright install chromium
      - run: python job_monitor.py
        env:
          SENDER_EMAIL: ${{ secrets.SENDER_EMAIL }}
          SENDER_PASSWORD: ${{ secrets.SENDER_PASSWORD }}
          RECIPIENT_EMAIL: ${{ secrets.RECIPIENT_EMAIL }}
      - name: Save state
        run: |
          git config user.name "Job Monitor"
          git config user.email "bot@noreply.com"
          git add -f state.json  # -f: state.json is in .gitignore
          git diff --cached --quiet || git commit -m "Update state"
          git push
```

---

## Files

| File | Purpose |
|---|---|
| `config_all.json` | Shipped list of companies; used by default (see step 2) |
| `config.json` | Your own settings; use it with `--config config.json` if `config_all.json` exists |
| `state.json` | Last-seen jobs (auto-managed, don't edit) |
| `state.json.*` | Lock file, last-email time and any set-aside unreadable state (auto-managed) |
| `monitor.log` | Run history and errors |

## Tests

The tests run offline (network and email are faked) and need only the packages in `requirements.txt`:

```bash
python -m unittest discover -s tests -v
```

## Tips

- **The first check of a target is silent** — it captures the baseline. This also applies when you add a target or change its URL, and after an unreadable `state.json` is set aside.
- **Browser mode is slower** (~15-20 sec per page) but handles any site.
- **html mode is fast** (~1-2 sec) but only works for static pages.
- **Don't over-check** — every 4-6 hours is plenty. Career pages don't update faster than that.
- If a site blocks you, try increasing the wait time or reducing check frequency.
