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

### 2. First run (generates config)

```bash
python job_monitor.py
```

Run periodically from the same process:

```bash
python job_monitor.py --config config_all.json --interval-minutes 15
```

Windows helper script:

```powershell
.\run_periodic_monitor.ps1 -Config config_all.json -IntervalMinutes 15
```

This creates `config.json`. It comes pre-configured with the Microsoft careers page for US remote entry-level jobs.

### 3. Edit config.json

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
  "max_jobs_per_target": 10,
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

First run = baseline snapshot (silent). Second run onwards = detects new postings.

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
          SMTP_PASSWORD: ${{ secrets.SMTP_PASSWORD }}
      - name: Save state
        run: |
          git config user.name "Job Monitor"
          git config user.email "bot@noreply.com"
          git add state.json
          git diff --cached --quiet || git commit -m "Update state"
          git push
```

---

## Files

| File | Purpose |
|---|---|
| `config.json` | Your settings (edit this) |
| `state.json` | Last-seen jobs (auto-managed, don't edit) |
| `monitor.log` | Run history and errors |

## Tips

- **First run is always silent** — it captures the baseline.
- **Browser mode is slower** (~15-20 sec per page) but handles any site.
- **html mode is fast** (~1-2 sec) but only works for static pages.
- **Don't over-check** — every 4-6 hours is plenty. Career pages don't update faster than that.
- If a site blocks you, try increasing the wait time or reducing check frequency.
