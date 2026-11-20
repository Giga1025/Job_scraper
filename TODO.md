# To-do

**Goal:** cover as many companies as possible, straight from their own job boards, so LinkedIn and
Jobright aren't needed.

## Next
- [ ] **iCIMS**: read companies' iCIMS career sites (about 6% of the lists' postings; research done:
      about 376 iCIMS portals and 95 company sites linked by the lists).
- [x] **Workday**: about 37% of the lists' postings (about 1,340 company sites: about 640 from the
      new-grad lists, about 700 more from the internship list). Read through the headless browser
      when Workday refuses plain requests from GitHub's machines (since Nov 16), a few minutes per
      run, in turns (the rest of each run: about 240 sites, each about every 3 hours); your NVIDIA, Salesforce, Circle and Intel
      entries too.
- [ ] **Workday, after the first days**: check in the run logs and `boards.md` that sites are read
      on GitHub (not skipped as bot checks), how often each comes round, and how much `state.json`
      grows (see "State size" below); adjust
      `workday_minutes_per_run` to match (whatever it says, no Workday site is started after a
      run's 24th minute, so runs end before the next one is due; the workflow's limit is 40).

## More companies
- [ ] **Boards gone (404)**: 56 Greenhouse, Ashby and Lever boards no longer exist (Nov 19: 20, 19
      and 17). Find where those companies moved and add their new boards.
- [ ] **All of Workable**: its hourly file (already read in full) has every Workable company, about
      5,300. Watch them all, with a block list of staffing and gig companies (about 2 real extra
      roles a day, about 10 junk ones before the block list).
- [x] **Oracle Recruiting Cloud** (Nov 2026): every Oracle company site the lists link (about 180:
      American Express, Fortinet, Oracle, Texas Instruments, Cummins...) is a company board now,
      read each hour (its newest 100 postings) with the reader the JPMorgan entry uses.
- [ ] **Other job sites the lists link**: TikTok/ByteDance, Rippling, Jobvite, BambooHR.
- [ ] **Beyond the four lists**: find more sources of companies (other new-grad lists, startup
      directories) so the registry grows past what the lists link.

## Fewer wrong roles
- [ ] **Filter clean-up**: SmartRecruiters' big non-tech employers bring non-software roles through
      loose title words ("Back End" night shift, CNC programmer, sales representative for
      infrastructure, curriculum developer, civil and environmental engineers). Decide on postdocs.
- [ ] **US filter misses** ("IN - Jaipur", "2 Locations") and engineering-but-not-software titles.
- [x] **Duplicate link**: Salesforce sometimes listed one job with and without the page's filters
      after "?". Workday entries now keep a link a posting was already recorded with.

## State size (`state.json` on `monitor-state`; GitHub warns at 50 MB, refuses 100 MB)
- [x] **Forget closed postings** (Nov 2026): it was 22.6 MB after the whole new-grad list added
      1,039 companies, and grew every day, because each company board kept every posting it had
      ever listed (up to 1,000 a board; the median board had 20, so almost none reached the cap).
      Now a Greenhouse, Ashby, Lever or Workable board keeps the postings it lists now plus those
      that closed in the last 60 days: a closed one gets a "gone" date and is deleted after 60
      days missing (one that comes back loses the date). Postings still listed are never deleted.
      A posting that was emailed to you is never forgotten (about 1%), so a reopened one isn't
      emailed twice. The file is also written without indentation (22.6 MB -> 18.5 MB, same
      content), and each run logs its size ("Saved state.json: ... MB") so growth can be watched.
- [ ] **Check the size after late January 2027**: nothing is forgotten before about Jan 19 (the
      postings already closed all got the first day's date), so until then the boards still grow
      about 0.13 MB a day; then they should level off at about 13-16 MB. If the whole file still
      heads for 50 MB, in this order:
      1. Workday sites: each keeps up to 100 postings not emailed plus 100 emailed, about 140
         bytes each (long links): 49 MB if all ~1,770 sites filled both (about 25 MB likelier).
         Store a short key (tenant + job number) instead of the whole link, or keep fewer.
      2. Save the state compressed (state.json.gz: 18 MB -> about 3 MB). The workflow's save step
         and the loader both change, with a fallback to read the old file once.
      3. Split the state into several files (one per platform), each well under the limits.
- [ ] SmartRecruiters boards still keep up to 1,000 postings each (0.8 MB today): only their
      newest pages are read, so a posting missing there may still be open and isn't forgotten.

## Housekeeping
- [ ] Delete the stale `state.json` on `main` (the state lives on the `monitor-state` branch).
- [ ] Delete the old log of workflow run 37739926237.
