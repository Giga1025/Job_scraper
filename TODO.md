# To-do

**Goal:** cover as many companies as possible, straight from their own job boards, so LinkedIn and
Jobright aren't needed.

## Next
- [ ] **iCIMS**: read companies' iCIMS career sites (about 6% of the lists' postings; research done:
      about 376 iCIMS portals and 95 company sites linked by the lists).
- [x] **Workday**: about 37% of the lists' postings (about 1,340 company sites: about 640 from the
      new-grad lists, about 700 more from the internship list). Read through the headless browser
      when Workday refuses plain requests from GitHub's machines (since Nov 16), a few minutes per
      run, in turns (each site about every 8 hours); your NVIDIA, Salesforce, Circle and Intel
      entries too.
- [ ] **Workday, after the first days**: check in the run logs and `boards.md` that sites are read
      on GitHub (not skipped as bot checks), how often each comes round, and how much `state.json`
      grows (about 21 MB expected, to about 38 MB; GitHub warns about a file over 50 MB); adjust
      `workday_minutes_per_run` to match, keeping runs well under GitHub's 30 minutes (whatever it
      says, no Workday site is started after a run's 20th minute).

## More companies
- [ ] **Boards gone (404)**: 56 Greenhouse, Ashby and Lever boards no longer exist (Nov 19: 20, 19
      and 17). Find where those companies moved and add their new boards.
- [ ] **All of Workable**: its hourly file (already read in full) has every Workable company, about
      5,300. Watch them all, with a block list of staffing and gig companies (about 2 real extra
      roles a day, about 10 junk ones before the block list).
- [ ] **Other job sites the lists link**: Oracle (about 66 companies), TikTok/ByteDance, Rippling,
      Jobvite, BambooHR.
- [ ] **Beyond the four lists**: find more sources of companies (other new-grad lists, startup
      directories) so the registry grows past what the lists link.

## Fewer wrong roles
- [ ] **Filter clean-up**: SmartRecruiters' big non-tech employers bring non-software roles through
      loose title words ("Back End" night shift, CNC programmer, sales representative for
      infrastructure, curriculum developer, civil and environmental engineers). Decide on postdocs.
- [ ] **US filter misses** ("IN - Jaipur", "2 Locations") and engineering-but-not-software titles.
- [x] **Duplicate link**: Salesforce sometimes listed one job with and without the page's filters
      after "?". Workday entries now keep a link a posting was already recorded with.

## Housekeeping
- [ ] Delete the stale `state.json` on `main` (the state lives on the `monitor-state` branch).
- [ ] Delete the old log of workflow run 37739926237.
