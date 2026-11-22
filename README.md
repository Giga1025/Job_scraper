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

The repo ships `config_all.json`: your settings (email, filters, schedule), with the companies
to check split into three files it lists under `target_files`:

| File | What's in it |
|---|---|
| `targets/big_tech.json` | Big tech and other companies with a hand-made entry each (finance, crypto, AI labs, data and infrastructure; about 60) |
| `targets/new_grad_lists.json` | New-grad lists maintained on GitHub by others |
| `targets/startups.json` | Startups, unicorns and mid-size companies on Greenhouse, Ashby, Lever, Workable, SmartRecruiters, Rippling or Gem, and companies of any size on Workday, Oracle or iCIMS (see [Startup and mid-size company job boards](#startup-and-mid-size-company-job-boards) and [Workday](#workday)) |

Each file holds a list of entries (under `"targets"`); a config can also have its own
`"targets"` list, checked first. Which config is used:

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
pure frontend (`exclude_frontend`, unless also full stack/platform), not tech
(`exclude_non_tech`: sales and pre-sales engineers, solutions engineers and architects,
support engineers, technicians, recruiters, healthcare, teaching, wet-lab science and other
jobs that aren't tech even with "engineer" or "developer" in the title; such a title is kept
only when `non_tech_ok_if` names the role itself, as in "Software Engineer, Sales Platform"),
non-engineering (`exclude_non_engineering`, unless an `engineering_signals` word is present) or
non-software engineering (`exclude_other_disciplines`, unless a `software_signals` word is
present). Matching is whole-word and case-insensitive. Edit the lists to taste; remove
`role_filter` to turn it off.

The "not tech" list was built from the ~8,500 titles the monitor had seen that passed the other
lists (Nov 2026): it removes about half of the non-tech ones among them while keeping all but a
handful of the ~5,100 tech ones (each word was checked against every title, and every title it
drops was reviewed).

Left-out postings are not dropped silently. They wait in `state.json` and are summarised in a
"Left out by your filters" section at the bottom of the next email: a count for each reason,
then up to 25 of them that still look like engineering roles (an engineering or software/ML
word in the title; "not tech" ones such as sales engineers are only counted), the ones left out
for their kind of role before those left out for their level. If no email has gone out for `filtered_digest_hours` (default 24), a digest email carries
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

An email with 10 or more roles starts with **top picks**: up to 15 roles whose title says new
grad, university grad, early career or entry level, then level-one titles (Software Engineer I,
Junior), then roles from the new-grad lists (not ones titled II or senior); software titles first
within each. They're listed under their companies too. `"top_picks"` in the config changes the
phrases, the limit and when it shows (`false`: never).

A **big email goes in parts** ("part 1 of 3"...), each kept under about 80 KB: Gmail cuts off an
email past about 100 KB, hiding the rest behind "[Message clipped]". That happens after a gap
(an email that failed) or when a new kind of company board is checked for the first time. The
warnings and top picks are in part 1, the left-out list in the last part, and a company too long
for one part is continued in the next. If sending stops partway, the parts that went out aren't
sent again: the rest go at the next run.

### New-grad lists maintained by others (startups and mid-size companies)

A target with `"mode": "feed"` reads a list of postings that someone else keeps up to date,
which covers hundreds of companies in one request. Each posting is emailed under its own
company ("Clay (via SimplifyJobs New Grad)"). `config_all.json` uses three:

| Feed | `format` | What's read |
|---|---|---|
| [SimplifyJobs/New-Grad-Positions](https://github.com/SimplifyJobs/New-Grad-Positions) | `simplify` | its `listings.json`: rows in the given `categories` (Software, AI/ML/Data and Quant; not Hardware or Product) |
| [speedyapply 2027 SWE](https://github.com/speedyapply/2027-SWE-College-Jobs) and [AI](https://github.com/speedyapply/2027-AI-College-Jobs) | `speedyapply` | the tables in `NEW_GRAD_USA.md` |

They live in `targets/new_grad_lists.json`. Each feed remembers every posting it has listed in the last `max_age_days` (90 for Simplify,
120 for speedyapply) and emails a link it hasn't seen before, whatever its posting date:
Simplify often adds a posting days or weeks after it went up. Closed postings are remembered
too but never emailed, so one that is re-opened doesn't come back as new. A posting that was already found,
by a company's own entry or another feed, isn't emailed again, even under a different link:
the job number in the link (Greenhouse, Ashby, Lever, Workday) is compared, and feeds are also
compared by company and title. A company's own entry may be filtered (a team, a level, the
newest page), so its postings that only a feed lists are still emailed. Your role filter and
US-only setting apply as usual. Like any new target, a feed's first check records what it
lists without emailing.

### Startup and mid-size company job boards

Most startups and mid-size companies post on Greenhouse, Ashby, Lever, Workable or
SmartRecruiters, whose job boards can be read one company at a time (Workable's through one
file of all its customers' jobs, see below). Big employers often use Workday (see below) or
Oracle Recruiting Cloud (American Express, Fortinet, Oracle, Texas Instruments, Cummins: about
180 company sites in the lists). An Oracle site (`<host>.oraclecloud.com/hcmUI/CandidateExperience/
.../sites/<site>`, one host can have several) is read through the API its career page uses, its
newest 100 postings each hour, with their countries (main and other places) for the US filter and
their posting date for a company's first check; `boards/oracle.py`. Goldman Sachs's and Uber's
own career sites (your entries) show their Oracle sites' postings under the same numbers, so a job
on both is emailed once; any other company's Oracle site isn't read while one of your entries has
its name (the two would email each job twice), unless its own site is mapped the same way
(`FRONT_ENDS` in `boards/oracle.py`).

Three more platforms are read the same way (Nov 2026):

- **iCIMS career sites built on Jibe** (AMD, Keysight, Rivian, Garmin, Viasat, ICE, DocuSign,
  Johns Hopkins APL, Schneider Electric, Peraton: about 130 sites in the lists, on
  `<name>.jibeapply.com` or the company's own domain, linked like `careers.amd.com/jobs/85806?icims=1`).
  Every such site serves one public JSON API at its root; the newest 100 postings are read each
  hour, and the next 100 too while they're from the last 3 days (a site posting a hundred a day)
  or, after an outage, from before the site's last good read, with every place's country (by its
  name: some codes are wrong) and the posting date; `boards/jibe.py`. A site whose API ignores the
  sort and lists oldest first (Bowman) has its last pages read; one whose API refuses the sort
  (AVI-SPL) is read in its own order, up to 500 postings. A site's saved ETag (which lets an
  unchanged site answer "not modified") is used only once it's known to list newest first. Since
  only the newest are read, a posting seen for the first time is new only if it
  went up in the last 7 days, or since the company's last good read (an older one moves up into the
  newest when newer ones close); when it went up is the later of its posting date and the day it
  reached the site. The link emailed is always the site's own: some sites name another domain for
  their postings where they aren't found (H-E-B's name careers.hrblock.com). A site is one iCIMS
  customer: the same job is on its classic iCIMS portals (`careers-amd.icims.com`) under the same
  number, and on any other domain the site names for it (Schneider Electric's `careers.se.com`);
  one company's two addresses (`careers.ice.com`, `ice.jibeapply.com`) are read once (the other
  one if the first's last read failed or it isn't found). Your AMD entry reads AMD's site under the same name, so a
  job on both is emailed once; a site isn't read while one of your entries has its name but reads
  somewhere else, or reads the site under another name. To add one under `companies`, link its
  search page or a posting with `?icims=1` added (a company's own domain can't otherwise be told
  from any other site). iCIMS's classic portals (`*.icims.com`, about 340 in the lists) aren't
  read yet (see `TODO.md`).
- **Rippling** (`ats.rippling.com/<board>/jobs`: about 180 boards in the lists, like Boom
  Supersonic and Foundation Robotics), read whole each hour; `boards/rippling.py`. Rippling's
  list gives no posting dates, so at a board's first check the jobs the email would keep (a few
  per board) get their date from their own page, and the ones from the last 3 days are emailed;
  if a job's page errs or has no date, or a run's 2 minutes of date requests are used up, the
  board is dated again next run (3 runs at most; a job whose page isn't found, closed since, is
  left out), and if Rippling doesn't answer, throttles or errs on two boards, no more dates are
  asked that run. A read missing a page
  counts as failed (a job missing from it would be forgotten, then emailed again).
- **Gem** (`jobs.gem.com/<board>`: Nuvo, Casap, 43North's portfolio companies), read whole each
  hour, with the posting dates; `boards/gem.py`.

The
`"mode": "boards"` entry in `targets/startups.json` checks the whole board of each company
in a registry that only grows:

- the companies you list under `companies` (a name and a link to the board, or to any of its
  postings); it ships with about 50 well-known startups,
- every company the new-grad lists link to on one of those sites, added the first time it
  shows up,
- every company the lists under `find_companies_in` link to: SimplifyJobs' internship list
  (companies that hire interns usually hire new grads too) and SimplifyJobs' whole new-grad
  list, every row since it started and every category (the new-grad feed itself only emails
  tech rows from the last 90 days, so a company that last hired new grads in the spring, or is
  listed under Hardware or Product, would otherwise never be checked: about 1,075 more boards,
  400 of them Workday sites, in Nov 2026). These lists are only used to find companies; their
  postings aren't emailed from here.

**Greenhouse boards behind companies' own sites.** Many companies show their Greenhouse board
on their own careers site, so the lists link `careers.airbnb.com/positions/7859317?gh_jid=7859317`,
not `job-boards.greenhouse.io/airbnb/...`: the link carries the posting's Greenhouse job id but
not the board's name, which reading the board needs. `boards/greenhouse.py` works the name out
for each such site (about 260 in the lists: Airbnb, AQR, Akuna Capital, Duolingo, Block, C3.ai,
Andreessen Horowitz...), each answer checked, and the board joins the registry like any other:

1. **From a posting**: Greenhouse's embed address for a job (`boards.greenhouse.io/embed/
   job_app?token=<job id>`) redirects to one naming its board while the posting is open. Each
   job id is asked about once (3 a site a check, newest first), unless a board already found
   for the site lists it; a site can have several boards (a main and a campus board, or a
   careers platform several companies use, each named after its own company).
2. **By a guess**: names from the site's address (only for a site one company uses) and the
   company's name, taken only if that board's postings link back to the same site (the company
   sets those links). Weekly, also for a site with a board: the board a posting named can be a
   side one (Thoughtworks' referrals board), the guess its main one.
3. **From a posting's page**, if the site still has no board: the board a careers page embeds
   (a closed posting's page too), taken if its postings link to the same site (or to
   Greenhouse's pages, and the name starts like the company's: a page can link other
   companies' boards).

A request that got no answer (a timeout, an error) isn't taken as "no board": that step is tried
again next run; after 10 unanswered requests of a kind in a row, or "too many requests", the rest
wait for the next run; at most 4 minutes a check (the first takes about a minute on GitHub: 236
boards for 267 sites in the dry run; later ones seconds). What was found is kept in `state.json`
(`greenhouse_sites`); the boards show in `boards.md` as "via companies' own sites".

A posting on the board and the lists' link to the company's site are one job (the same Greenhouse
job id), and so is the same page's link without the id. A board isn't read while one of your
entries reads that company's site and can't be matched with it both ways (it has the board's name
and records the ids): your "Square / Block" entry reads block.xyz, so Block's board isn't read.

Companies on **Workable** join the same registry (as their Workable account); the code is in
`boards/workable.py`. Workable refuses quick reading of each company's page ("429 Too Many
Requests"), but it publishes one file of every customer's published jobs, updated hourly, for
exactly this use: the monitor reads it once per check (about a minute; "not modified", in under
a second, when it hasn't changed). The file names companies, not their accounts, so each
account is matched to its company there:

1. **By a job code**: one a list linked it to, or one Workable lists for the account when the
   monitor asks it (each account at most once a day whatever the answer, 30 accounts a check,
   2 seconds apart, under the monitor's own name; it stops asking for that check as soon as
   Workable refuses, fails or doesn't answer). If the company renames itself in Workable, the
   account follows it through the jobs it listed before.
2. **Failing that, by name**: when exactly one company in the file has the account's name (the
   name Workable gives it if Workable answered, else the lists'; "Inc", "LLC" and the like at the
   end don't count). Not when two companies or two accounts share the name, when another account
   holds that company by a job code, or when Workable listed jobs for the account that the file
   doesn't have. A name match is undone when Workable's later answer contradicts it, and a job
   code found later wins over it; when the account moves to another company that way, that
   company's roles from the last few days are emailed, not its older ones.

An account matched neither way isn't checked yet (its roles the lists carry still reach you);
once it's matched, its first check catches up on everything it posted since it was added.
To add a Workable company yourself, link one of its postings
(`apply.workable.com/<account>/j/<code>`). `boards.md` marks accounts not matched yet, and
those matched by name. Job links are emailed as Workable gives them.

Companies on **SmartRecruiters** join it too, read through SmartRecruiters' public Posting API
(no key needed; its robots.txt asks bots other than LinkedIn's to stay away, and you chose to
read it anyway, gently). One request per company per check for its 100 newest US postings
("not modified", with no data, when nothing changed), two at a time with a pause, sent under
the monitor's own name; the first "too many requests" or refusal ends SmartRecruiters reading
until the next check. A company that left SmartRecruiters just answers with an empty list.

Companies on **Workday** join it too; see [Workday](#workday) below.

A company, once in, stays even when the lists drop it. `boards.md` (on the `monitor-state`
branch, next to `left_out.md`) lists every board checked, when it was added and where it was
found (and Workable accounts not matched yet, marked as such). Boards the config already has an entry for aren't checked twice.

The boards are checked about every `every_hours` (1), in parallel, and a company new to the
registry right away. Its first check records what its board lists and emails only the roles
posted in the last `catch_up_days` (3) days (Greenhouse, Ashby and Lever give each posting's
date), so a company's thousands of older postings don't flood your inbox but a role it posted
yesterday isn't missed. Boards first checked before `catch_up_days` existed are caught up the
same way once. Each board is asked "anything changed since this version?" (its ETag), so an
unchanged one answers with no data. A board that no longer exists is tried again a day later.

Only titles containing one of `tech_titles` (software, ML, AI, infrastructure, inference,
forward deployed, member of technical staff, research engineer/scientist, new grad, ...) are
considered, minus titles with a `skip_titles` word: gig work such as AI trainer, tutor,
annotator, rater or labeler, "expert", contractor and part-time roles. Words that also name
real engineering work ("Software Engineer, Annotation Systems") aren't on that list. Then your
role filter and US-only setting apply as usual. What they leave out at these hundreds of
companies (mostly senior roles and internships) is skipped without being listed, so the
left-out list isn't flooded. Companies under `skip_companies` (gig-work platforms) aren't
checked at all, and `"enabled": false` on the entry pauses every board.

Postings are emailed under the company's name. A posting already emailed from a new-grad list
isn't emailed again from the board (or the other way round), and a Greenhouse job a company
posts on two of its boards (say its main and campus boards) is emailed once. The same role on
two Ashby or Lever boards, or a new-grad list linking the other board's copy, can still come
twice.

### Workday

About 37% of the postings the new-grad lists link to are on Workday (`<company>.wdN.myworkdayjobs.com`
or `wdN.myworkdaysite.com`): defense, banks, chips, enterprise software. That is about 640 company
sites in the new-grad lists today, and the internship list under `find_companies_in` (about half
of its postings are on Workday) adds about 700 more: about 1,340 in all. The code is in
`boards/workday.py` (and `boards/browser.py` for the browser).

**Why a browser.** Since about Nov 16, 2026, Workday puts a bot check (Cloudflare) in front of its
sites. Plain requests from GitHub's machines are answered "429" (with a `Cf-Mitigated:
challenge` header), while a browser opening the same search page loads it as usual. So each run
asks Workday plainly once; if that is refused, the rest of the run reads Workday through one
headless Chromium (Playwright's, the one installed for "browser" entries): it opens each site's
search page and takes the job list the page itself fetches, and asks for more pages from inside
the page, the way the page does. It doesn't try to get past the check: no stealth add-ons, no
proxies, no solving anything, no retries. A site whose page shows a bot check or a server error,
or whose job list doesn't arrive in full within 15 seconds, is skipped for that run, and after 3
such sites in a row Workday is left until the next run (a site that already couldn't be read at
its last 2 tries doesn't count unless it shows a bot check, see below). Asking a page for more
postings waits 20 seconds at most, and a watchdog stops the browser if a page's stuck script
holds it 15 seconds past those limits (that site is skipped, the next one gets a fresh browser),
so one bad site can't hold up the run.
Where plain requests work (from your own computer, for instance), the browser is started only for
a site whose plain request fails (a server error or no answer, say); if it can't start there, that
site is skipped and the others are still read with plain requests.

**Outside the US.** Workday's job list gives each posting's place as text, and for many only
"2 Locations" or nothing at all (Accenture's), so the US filter can't tell a posting in Germany
or India from one in Virginia. For a new posting the email would list (a title your filters want,
posted in the last 7 days) whose place the text doesn't settle, the monitor asks Workday for
that posting's own page while the site is open (one request each, at most 20 a site a visit):
it names the country of the posting's first place and its other places. The posting is left
out unless that country is the US or one of its other places is clearly in the US ("McLean,
VA"); the email shows its places instead of "2 Locations". A posting whose page can't be had
(or past the 20) is kept, as before.

**The rest of each run.** Runs come every 30 minutes and everything else takes 5-10 minutes, so
Workday gets the rest: no Workday site (your entries or the company sites) is started once the
run is 24 minutes old, whatever `workday_minutes_per_run` says; the sites left wait for the next
run. A site started just before that takes a minute or two at most, so a run ends after about
25-30 minutes (the workflow's own limit, `timeout-minutes`, is 40, only as a safety net).

**Your entries on Workday** (NVIDIA, Salesforce, Circle and Intel in `targets/big_tech.json`) are
read this way whatever their `mode`, with the filters in their link (the part after `?`), after
every other entry, and every run (the time limit below is for the company sites) unless the run is
already 24 minutes old. A posting keeps a link it was already recorded with (with or without the
filters after `?`), so a posting isn't emailed twice because its link changed form. A posting a
new-grad list already brought you isn't emailed again by your entry (same Workday job number),
nor the other way round, nor one your entry emailed before and Workday now shows under a new
link (an edited title or location).

**Company sites** (every Workday site the lists link to, added like other boards):

- read after everything else, one request at a time, `workday_seconds_between_requests` (2)
  apart, for at most `workday_minutes_per_run` (24: the rest of the run) minutes per run (`targets/startups.json`; 0
  pauses them; at most 20, and never past the run's 20th minute, see above). A site takes about
  4 seconds, so about 240 are read per run (15-20 minutes);
- in turns: sites never tried first (those a list linked most recently first), then the ones tried
  longest ago. With about 1,410 sites, about 240 a run and a run every 30 minutes, each comes
  round about every 3 hours rather than hourly, still ahead of the lists (they're a day or two
  late). A site that couldn't be read at its last 2 tries waits at the back, and isn't tried
  again until a day after its last try. When it comes round again, skipping it doesn't count
  toward the "3 in a row" that stops Workday: such sites were all tried in the same run, so they
  come round together, and without this a few sites that fail every time would stop the company
  sites every day. A bot check still counts, whatever the site's history, so if Workday keeps
  refusing the monitor for weeks, each run still stops after 3 of them;
- a site that no longer exists answers "404" when its company is still on Workday; it is then
  tried again daily, like other boards. A company name Workday doesn't have at all (after an
  acquisition, say) gets a server error instead, which leads to Workday's "Workday is currently
  unavailable" page: the same page Workday shows while it is down for maintenance. The monitor
  can't tell those apart, so it doesn't count such a site as gone. It skips it at once (no
  15-second wait), like any site that couldn't be read, and after 2 such tries the site waits at
  the back for a day;
- only the newest postings (20 per page, newest first): down to postings already seen (one page,
  most of the time), at most 3 pages. A posting seen for the first time counts as new only if
  Workday says it went up in the last 7 days, so an older one that moves up the list isn't
  emailed. A site's first read emails only what went up in the last `catch_up_days` days;
- a Workday site one of your entries already searches (like NVIDIA's) isn't read twice, nor a
  site the lists link under both forms of its address (`chewy.wd5.myworkdayjobs.com/External`
  and `wd5.myworkdaysite.com/recruiting/chewy/External`);
- a company's Workday site isn't read when one of your other entries (paused or not) has the
  company's name: your PayPal entry reads PayPal's Eightfold page, which lists the same jobs as
  `paypal.wd1.myworkdayjobs.com` under other numbers, so each new role would be emailed twice;
- each site remembers (the link only) its 100 latest postings that weren't emailed to you, and
  besides those its 100 latest that were, so a busy site (Walmart, CVS) never forgets one it
  emailed and a list linking it later doesn't email it again. That is up to about 140 bytes a
  posting, so up to about 28 KB a site (and 49 MB if all ~1,770 sites filled up both; about 25 MB
  is likelier), reached over weeks as sites fill up. See "How big state.json gets" below.

**How big `state.json` gets.** GitHub warns about a file over 50 MB and refuses one over 100 MB
(the state then couldn't be saved), so the state keeps only what it needs:
- a company job board on Greenhouse, Ashby, Lever or Workable remembers (the link only, about 90
  bytes) every posting it lists now, and a posting it no longer lists (closed) for 60 days
  (`FORGET_GONE_DAYS`): the day a posting is first missing it gets a `"gone"` date, and it is
  forgotten once it has been missing 60 days; one that comes back loses the date and isn't
  emailed again. A posting that was emailed to you is never forgotten (about 1% of them), so one
  reopened months later with the same link isn't emailed twice. Before this (Nov 2026) every
  posting a board ever listed stayed, so the file grew every day with the roles companies posted,
  not only with new companies. Postings already closed when this started all got that first
  day's date, so the first are forgotten around Jan 19, 2027: until then the boards grow as
  before (about 0.13 MB a day), then level off at their open postings plus 60 days of closed ones
  (about 13-16 MB, against 9.3 MB in Nov 2026);
- SmartRecruiters boards keep the latest 1,000 (their newest postings are read, not the whole
  list, so a posting missing from them may still be open), Workday sites as above, your own
  entries the latest 1,000, the new-grad lists their last 90 or 120 days, the left-out list 7 days;
- the file is written without indentation (about a fifth smaller), and each run logs its size
  ("Saved state.json: 18.5 MB").

The log and `boards.md` say how many sites each run read, couldn't read, and left for the next
run (and `boards.md`, how many runs a round of all sites takes at that pace). `boards.md` also
marks each site not read yet ("waiting for its turn", or "couldn't be read at its last try" when
it was tried) and each read before that couldn't be read at its last tries.
Runs now take about 25-30 minutes: Workday uses what's left of each half hour (the workflow's
safety limit is 40 minutes).

### Job-board APIs and US-only

When a target's `url` is one of these public job-board APIs, the monitor reads it directly
(no browser, clean titles, and each job's location):

| Service | URL form | Optional filters in the URL |
|---|---|---|
| Greenhouse | `https://boards-api.greenhouse.io/v1/boards/<board>/jobs` | `departments[]=<id or name>`, `offices[]=` |
| Ashby | `https://api.ashbyhq.com/posting-api/job-board/<board>` | `department=<name>`, `team=<name>` |
| Lever | `https://api.lever.co/v0/postings/<company>` | `location=`, `department=`, `team=`, `commitment=` |
| Oracle HCM | `https://<host>.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitions?...finder=findReqs;siteNumber=...` | the finder's own parameters |
| iCIMS (Jibe sites) | `https://<site>/api/jobs?page=1&limit=100&sortBy=posted_date&descending=true&internal=false` | the API's own parameters |
| Rippling | `https://ats.rippling.com/api/v2/board/<board>/jobs?page=0&pageSize=1000&groupJobsByLocation=true` | |
| Gem | `https://api.gem.com/job_board/v0/<board>/job_posts/` | |
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

**Several links for one company:** add one entry per link and give them the same `name`
(Uber, for example, has one entry for its University team and one for all its US openings).
A posting both links show is emailed once, under that name, whichever entry finds it first,
when both pages link it the same way or with the same Greenhouse, Ashby, Lever, Workable or SmartRecruiters job id (a
job the two pages link differently can be listed twice). The order of the entries doesn't
matter, and a new entry's first run, which records what its page lists without emailing,
never hides a posting from the other entry; nor does one entry's own filter (`keyword_filters`,
`us_only`) leaving out a posting without listing it, and a posting one entry only listed as
left out is still emailed by another whose filters let it through. Entries count as the same
company when their names match ignoring case, punctuation and endings like "Inc" (in any
script). A paused entry still counts, so what it already emailed isn't sent again by the
other. (state.json marks each posting an entry found for you with `"reported": "match"` or
`"reported": "left out"`; postings saved before November 2026 have no mark, so a newly added
entry may email one of those once more rather than risk hiding it.) Where it can't be sure,
it errs toward emailing twice rather than not at all: after you edit or remove one of a
company's links, a posting that link already emailed may come again from the other; and if
the entries use different `keyword_filters`, one email can list a posting both as new and
under "left out".

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

### Reliable runs every 30 minutes (cron-job.org)

GitHub starts scheduled runs late, or skips them, when it is busy; on this repository it ran
the 30-minute schedule only a few times a day. A free scheduler can ask GitHub to start the
**Job monitor** workflow instead, exactly like clicking **Run workflow**, every 30 minutes.
The GitHub schedule stays in the workflow as a backup.

**1. Make a GitHub token that can only start this repository's workflows.**

1. GitHub → your profile picture → **Settings** → **Developer settings** (bottom of the left
   menu) → **Personal access tokens** → **Fine-grained tokens** → **Generate new token**.
   GitHub may ask for your password again.
2. **Token name:** `cron-job.org dispatch`. **Expiration:** a date up to a year away (GitHub
   emails you before it expires).
3. **Resource owner:** your account. **Repository access:** **Only select repositories** →
   pick `Job_scraper`.
4. **Permissions:** under repository permissions add **Actions** and set it to **Read and
   write**. (**Metadata: Read-only** is added automatically.) Add nothing else; in
   particular not **Workflows**, which can edit workflow files.
5. **Generate token**, and copy the `github_pat_...` value now; GitHub shows it only once.
   Paste it only into cron-job.org below: never into this repository, an issue or a chat.
   (GitHub automatically revokes a token that is pushed to a public repository.)

**2. Create the job at [cron-job.org](https://cron-job.org)** (free; sign up, confirm the
email, then sign in at console.cron-job.org; turning on multi-factor authentication under
**Settings** is a good idea, because the token is stored there as plain text).

1. **Cronjobs** → **Create cronjob**.
2. **Common** tab:
   - **Title:** `Job monitor`
   - **URL:** replace the `http://` with
     `https://api.github.com/repos/Giga1025/Job_scraper/actions/workflows/job-monitor.yml/dispatches`
   - **Save responses in job history:** on (then GitHub's error message is kept if a call fails)
   - **Execution schedule:** **Every** → **30 minutes** (the default is 15)
3. **Notifications** tab: turn on **execution of the cronjob fails** and **execution of the
   cronjob succeeds after it failed before** (both are off by default).
4. **Advanced** tab:
   - **Headers** → **Add**, once for each:

     | Key | Value |
     |---|---|
     | `Accept` | `application/vnd.github+json` |
     | `Authorization` | `Bearer ` followed by your token (one space, no quotes) |
     | `X-GitHub-Api-Version` | `2026-03-10` |
     | `Content-Type` | `application/json` |

   - **Request method:** **POST** (the body box stays grey until you pick it)
   - **Request body:** `{"ref":"main"}`
5. **Test run** → **Start test run**. This really starts a run. It worked if the response is
   **200** (with a link to the run); then a new **Job monitor** run shows on the Actions tab.
   Click **Create**.

**If it stops working:** cron-job.org's **History** shows each call's result. **401** means
the token expired or was mistyped (make a new one and paste it into the `Authorization`
header); **403** or **404** usually mean the token lacks **Actions: Read and write** or this
repository; **422** usually means the body isn't exactly `{"ref":"main"}`. After 25 failures
in a row (about 13 hours) cron-job.org switches the job off and emails you; switch **Enable
job** back on once it's fixed. A **200** only means GitHub started the run; the run's own
result is on the Actions tab.

With both cron-job.org and the backup schedule, two runs sometimes come close together; the
second waits for the first, and a run that was waiting can show as **Cancelled** when a newer
one replaces it. That's expected and harmless. If you'd rather have only cron-job.org, delete
the `schedule:` lines from `.github/workflows/job-monitor.yml`.

### Reading a run

- On the **Actions** tab, each run shows ✅ (worked), ❌ (failed) or a spinner (running).
- Click a run, then **check**, to see each step. Open **Check every company for new postings**
  to read the monitor's log: every company, how many jobs it found, and what's new.
- A run fails (❌) if the email couldn't be sent, for example a wrong app password. Nothing
  is lost: postings that weren't emailed are sent by the next run. GitHub emails you when a
  scheduled run fails.
- A company that can't be read is logged and skipped; the run still succeeds.

### Fixing an entry that stopped working

Sites change: a careers page moves, a company switches job sites, or a site starts refusing
GitHub's machines. When one of your entries (in `targets/big_tech.json` or
`targets/new_grad_lists.json`) can't be read for 6 hours, or finds no postings at all for 2
days, every email starts with a short warning: the entry, why (in plain words: refused with
429/403, page gone with 404, didn't load in time, or found nothing), and since when. It
disappears by itself once the entry works again. Company job boards found through the lists
aren't part of it (many have no openings for a while); `boards.md` lists them.

What to do:

- **Page gone (404) or found nothing:** open the company's careers site, find where it lists
  jobs now (often a Greenhouse, Ashby or Lever board: see "Job-board APIs and US-only"), and
  update the entry's `url` (keep its `name`, so nothing is lost or emailed twice).
- **Refused (429/403):** the site blocks automated visitors from GitHub's machines. That can't
  be fixed by the monitor without getting around the site's protection, which it doesn't do.
  Pause the entry with `"enabled": false` (the warning then stops); its roles usually still
  reach you through the new-grad lists, a day or two later. (Workday entries are already read
  through the browser when plain requests are refused; see [Workday](#workday).)

### When a whole source stops

A company board that can't be read now and then is normal, but a whole source going quiet would
look just like "no new jobs". So every email starts with a warning while one of these lasts
(`sources.py`):

- **Workday** read no site in 3 runs in a row, with the reason: Workday refusing the browser,
  every site answering "not found", the runs running out of time before Workday's turn, or why
  its reading stopped (not while you've paused it with `workday_minutes_per_run`);
- **a job-board platform** (Greenhouse, Ashby, Lever, Workable, SmartRecruiters, Oracle, iCIMS (Jibe),
  Rippling, Gem): half or
  more of its boards (10 or more) couldn't be read, or answered "not found" (an API that moved),
  in 3 checks in a row;
- **the company boards** weren't checked for 6 hours, or 3 times their `every_hours`;
- **a new-grad list** added no posting for 3 days (it may have stopped updating, or moved), or a
  list used to find companies couldn't be read for 3 days.

A run can't report that the runs stopped altogether, so once a UTC day (`"daily_check_hour_utc":
14` in `config_all.json`: from 14:00 UTC, 8 AM US Central; at once if the last one is over 30 hours
old) a short **Daily check** email says the
monitor is still running: how many runs there were in the last 24 hours (about 48 expected),
how many company boards, Workday sites and entries were checked, how many roles were found and
emailed, and the same warnings. **If a day passes without one, the runs have stopped**: look at
the Actions tab (a failed run, or none since a while: see "Reliable runs every 30 minutes"
above). What it needs is kept in `state.json` (`_sources`, the last 2 days of runs).

What to do about a warning: open the latest run's log on the Actions tab and search for the
source's name (`[workday]`, `[boards]`, the list's name). A list that moved needs its new
address in `targets/new_grad_lists.json`; a platform that changed its API needs a code change.

### Changing things

- **How often:** change the schedule of the cron-job.org job (see above). The backup schedule
  is the `cron` line in `.github/workflows/job-monitor.yml`; it has five fields (minute, hour,
  day of month, month, day of week), in UTC: `"7,37 * * * *"` means minutes 7 and 37 of every
  hour, and `"7 */3 * * *"` would mean every 3 hours. How often emails go out is
  `email_every_hours` in `config_all.json`.
- **Pause everything:** Actions tab → **Job monitor** → **⋯** → **Disable workflow** (and
  **Enable workflow** to resume).
- **After 60 quiet days:** GitHub turns off scheduled workflows in a public repository when
  nothing has happened in it for 60 days (it emails you first). The monitor's own saves don't
  count, so if you go two months without pushing anything, click **Enable workflow** on the
  Actions tab (or push any commit) to turn it back on.
- **Pause one company:** add `"enabled": false` to its entry in `targets/big_tech.json` (Tesla and
  Atlassian are paused this way for now). Its saved postings are kept, so when you remove the
  line, only what it posted in the meantime is new.
- **Watch another page of a company:** add a second entry with the same `name` (see
  "Several links for one company" under Target Configuration). Its first run records what it
  lists without emailing; from then on its new postings are emailed, except ones the
  company's other entry already found for you.
- **Edit an entry's link:** fine at any time. The new link's first run records what it lists
  without emailing, and postings the old link found that are still waiting for the next email
  are still sent, as long as the entry keeps its `name`.

### Working on the code from your computer

The workflow never commits to `main`, so `main` only changes when you (or a merged pull
request) change it. The `state.json` still in `main` is the starting point for the very first
run that uses the `monitor-state` branch; after that it isn't used or updated.

---

## Files

| File | Purpose |
|---|---|
| `job_monitor.py` | The monitor: reads the config, checks every entry, emails what's new |
| `sources.py` | Whether whole sources still work (the warnings at the top of emails) and the daily check email; `job_monitor.py` needs it next to it |
| `TODO.md` | What's planned next, and the goal: as many companies as possible, from their own job boards |
| `boards/` | Readers for company job sites, one file each (`boards/workable.py`, `boards/workday.py`), and `boards/browser.py`, the headless browser Workday is read through; `job_monitor.py` needs this folder next to it |
| `config_all.json` | Shipped settings; used by default (see step 2) |
| `targets/*.json` | The companies and lists `config_all.json` checks (big tech, new-grad lists, startups) |
| `config.json` | Your own settings; use it with `--config config.json` if `config_all.json` exists |
| `state.json` | Postings already seen, and those waiting to be emailed (auto-managed, don't edit; on GitHub it lives on the `monitor-state` branch) |
| `left_out.md` | Every posting the filters left out in the last 7 days (auto-managed; on the `monitor-state` branch) |
| `boards.md` | Every startup and mid-size company job board checked (auto-managed; on the `monitor-state` branch) |
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
