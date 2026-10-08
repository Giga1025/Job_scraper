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
(if it finds none, it looks once more, since an empty page is often a glitch).
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

Left-out postings are not dropped silently. They wait in `state.json` and are summarised in a
"Left out by your filters" section at the bottom of the next email: a count for each reason,
then up to 25 of them that still look like engineering roles (an engineering or software/ML
word in the title), the ones left out for their kind of role before those left out for their
level. If no email has gone out for `filtered_digest_hours` (default 24), a digest email carries
them. Every left-out posting of the last 7 days, with its reason, is also listed in
`left_out.md` (next to `state.json`; on GitHub, on the `monitor-state` branch), and the email
links to it. Monitors sharing a `state.json` (e.g. batch configs) share this list. This is on
whenever `role_filter` is set; `"show_filtered": false` drops left-out postings quietly instead.

`keyword_filters` (global, or per target) still works as an allow-list. Postings it leaves out
are dropped quietly as before, unless you set `"show_filtered": true`.

### How often emails come

Each run records new postings straight away, and they wait in `state.json` until an email
lists them, so nothing is lost if an email fails or a posting is taken down in the meantime.
With `"email_every_hours": 2` (as in `config_all.json`) at most one email goes out every two
hours, carrying everything found since the last one; `0` emails after every run that finds
something.

### New-grad lists maintained by others (startups and mid-size companies)

A target with `"mode": "feed"` reads a list of postings that someone else keeps up to date,
which covers hundreds of companies in one request. Each posting is emailed under its own
company ("Clay (via SimplifyJobs New Grad)"). `config_all.json` uses three:

| Feed | `format` | What's read |
|---|---|---|
| [SimplifyJobs/New-Grad-Positions](https://github.com/SimplifyJobs/New-Grad-Positions) | `simplify` | its `listings.json`: rows in the given `categories` |
| [speedyapply 2027 SWE](https://github.com/speedyapply/2027-SWE-College-Jobs) and [AI](https://github.com/speedyapply/2027-AI-College-Jobs) | `speedyapply` | the tables in `NEW_GRAD_USA.md` |

Each feed remembers every posting it has listed in the last `max_age_days` (90 for Simplify,
120 for speedyapply) and emails a link it hasn't seen before, whatever its posting date:
Simplify often adds a posting days or weeks after it went up. Closed postings are remembered
too but never emailed, so one that is re-opened doesn't come back as new. A posting that was already found,
by a company's own entry or another feed, isn't emailed again, even under a different link:
the job number in the link (Greenhouse, Ashby, Lever, Workday) is compared, and feeds are also
compared by company and title. A company's own entry may be filtered (a team, a level, the
newest page), so its postings that only a feed lists are still emailed. Your role filter and
US-only setting apply as usual. Like any new target, a feed's first check records what it
lists without emailing.

### Job-board APIs and US-only

When a target's `url` is one of these public job-board APIs, the monitor reads it directly
(no browser, clean titles, and each job's location):

| Service | URL form | Optional filters in the URL |
|---|---|---|
| Greenhouse | `https://boards-api.greenhouse.io/v1/boards/<board>/jobs` | `departments[]=<id or name>`, `offices[]=` |
| Ashby | `https://api.ashbyhq.com/posting-api/job-board/<board>` | `department=<name>`, `team=<name>` |
| Lever | `https://api.lever.co/v0/postings/<company>` | `location=`, `department=`, `team=`, `commitment=` |
| Oracle HCM | `https://<host>.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitions?...finder=findReqs;siteNumber=...` | the finder's own parameters |
| TalentBrew (e.g. jobs.intuit.com) | `https://<site>/search-jobs/results?...&SearchResultsModuleName=Search+Results&SortCriteria=1&SortDirection=1&RecordsPerPage=50` | the search page's own filters (`FacetFilters[0].ID=...`) |

Eightfold sites (Microsoft, Morgan Stanley, PayPal, ...) are read through their API as before.
Where a site records only the posting date (Morgan Stanley, PayPal), the newest postings all tie
and come back in a different order each visit, so the monitor reads the two newest posting days
in full rather than just the first page.

With `"us_only": true` (the default in `config_all.json`), new postings from these sources
whose location clearly lies outside the US are skipped. Unknown or "Remote" locations are kept;
set `"us_only": false` on a target to keep its non-US postings. If a job-board API can't be
read, that target is skipped for the run rather than falling back to another source.

**Sitemaps (Citadel).** Some careers sites block scripted visits to the jobs page but publish a
sitemap. With `"mode": "sitemap"`, the target's `url` is the sitemap and `link_selector` is
text every job page URL contains (e.g. `"/careers/details/"`). The title and region come from
the page address (`...-intern-us-new-york/` becomes "Intern", New York, US).

**Tesla.** For a `https://www.tesla.com/careers/search/?country=US` target, the monitor reads
the jobs data behind that page (`/cua-api/apps/careers/state`), keeping the page's `country`
(and `type`, e.g. `intern`). Tesla's bot protection refuses scripted requests from many
networks (including cloud servers); the monitor then tries the page in the browser, and if that
shows nothing either it skips Tesla for the run without recording anything. It doesn't try to
get around the protection. To see whether your network gets through:

```bash
python -c "import requests;r=requests.get('https://www.tesla.com/cua-api/apps/careers/state',headers={'User-Agent':'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36','Accept':'application/json','Referer':'https://www.tesla.com/careers/search/'},timeout=60);print('HTTP',r.status_code,len(r.content),'bytes')"
```

`HTTP 200` with a large size means Tesla works from that machine; `HTTP 403` means it is blocked there.

---

## Target Configuration

Each target has these fields:

| Field | Required | Description |
|---|---|---|
| `name` | Yes | Friendly label (used in alerts) |
| `url` | Yes | Career page URL with your filters applied |
| `mode` | Yes | `"html"` for static pages, `"browser"` for JS-heavy pages, `"sitemap"` for a sitemap of job pages, `"feed"` for a maintained list of postings (with `format`, `max_age_days`, and for Simplify `categories`) |
| `wait_for` | No | CSS selector to wait for before scraping (browser mode only) |
| `link_selector` | No | CSS selector for job links. If empty, uses heuristics |
| `enabled` | No | `false` pauses the target: it isn't checked, and its saved postings are kept |

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

See [Running on GitHub Actions](#running-on-github-actions) below.

---

## Running on GitHub Actions

GitHub Actions runs programs on GitHub's computers when something happens in your
repository, such as on a schedule or when you push. This repository has two **workflows**
(the files in `.github/workflows/`):

| Workflow | When it runs | What it does |
|---|---|---|
| **Job monitor** (`job-monitor.yml`) | every 30 minutes, or when you click *Run workflow* | checks every company, emails new postings (at most every 2 hours), saves what it has seen |
| **Tests** (`tests.yml`) | on every push | runs the test suite; a red ❌ on a commit means a change broke something |

Each run starts on a fresh, empty computer, so the job monitor workflow installs Python,
the packages and a browser, runs `job_monitor.py`, and then **saves `state.json` (and
`left_out.md`) to the `monitor-state` branch**. That file is how the next run knows which
postings you've already seen and which are waiting to be emailed. The branch always holds a
single commit, which each run replaces, so it doesn't fill the history, and `main` only
changes when you change it. To read the full left-out list, open `left_out.md` on that branch
(the emails link to it).

### One-time setup

1. **Get the workflows onto `main`.** Scheduled workflows only run from the default branch,
   so merge the branch that adds them (open a pull request on GitHub and merge it).
2. **Create a Gmail app password** (see [Email Setup](#email-setup-gmail)), a 16-character
   code that lets the monitor send mail as you without your real password.
3. **Add three secrets.** On GitHub, go to the repository's **Settings → Secrets and variables
   → Actions → New repository secret** and add:

   | Name | Value |
   |---|---|
   | `SENDER_EMAIL` | the Gmail address that sends the alerts |
   | `SENDER_PASSWORD` | the app password from step 2 |
   | `RECIPIENT_EMAIL` | where alerts go (can be the same address; separate several with commas) |

   Secrets are encrypted and are never shown in logs (they appear as `***`), which matters
   because this repository is public and so are its run logs.
4. **Run it once by hand.** Open the **Actions** tab, pick **Job monitor** on the left, click
   **Run workflow**, then **Run workflow** again. The first run only records what each company
   lists today (no email); from the next run on, new postings are emailed.

   Ticking **Dry run** in that menu runs everything but sends no email and saves to a separate
   `monitor-state-test` branch, so it can't affect your real alerts. Runs started from any
   branch other than `main` work the same way (no email, test branch).

### Reading a run

- On the **Actions** tab, each run shows ✅ (worked), ❌ (failed) or a spinner (running).
- Click a run, then **check**, to see each step. Open **Check every company for new postings**
  to read the monitor's log: every company, how many jobs it found, and what's new.
- A run fails (❌) if the email couldn't be sent, for example a wrong app password. Nothing
  is lost: postings that weren't emailed are sent by the next run. GitHub emails you when a
  scheduled run fails.
- A company that can't be read is logged and skipped; the run still succeeds.

### Changing things

- **How often:** edit the `cron` line in `.github/workflows/job-monitor.yml`. It has five fields
  (minute, hour, day of month, month, day of week), in UTC: `"7,37 * * * *"` means minutes 7 and
  37 of every hour; `"7 */3 * * *"` would mean every 3 hours. GitHub may start a run a few
  minutes late.
- **Pause everything:** Actions tab → **Job monitor** → **⋯** → **Disable workflow** (and
  **Enable workflow** to resume).
- **After 60 quiet days:** GitHub turns off scheduled workflows in a public repository when
  nothing has happened in it for 60 days (it emails you first). The monitor's own saves don't
  count, so if you go two months without pushing anything, click **Enable workflow** on the
  Actions tab (or push any commit) to turn it back on.
- **Pause one company:** add `"enabled": false` to its entry in `config_all.json` (Tesla and
  Atlassian are paused this way for now). Its saved postings are kept, so when you remove the
  line, only what it posted in the meantime is new.

### Working on the code from your computer

The workflow never commits to `main`, so `main` only changes when you (or a merged pull
request) change it. The `state.json` still in `main` is the starting point for the very first
run that uses the `monitor-state` branch; after that it isn't used or updated.

---

## Files

| File | Purpose |
|---|---|
| `config_all.json` | Shipped list of companies; used by default (see step 2) |
| `config.json` | Your own settings; use it with `--config config.json` if `config_all.json` exists |
| `state.json` | Postings already seen, and those waiting to be emailed (auto-managed, don't edit; on GitHub it lives on the `monitor-state` branch) |
| `left_out.md` | Every posting the filters left out in the last 7 days (auto-managed; on the `monitor-state` branch) |
| `.github/workflows/` | The GitHub Actions workflows (job monitor and tests) |
| `state.json.*` | Lock file and any set-aside unreadable state (auto-managed) |
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
