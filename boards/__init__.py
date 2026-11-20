"""Readers for company job boards, one module per job site, used by job_monitor.py.

common.py holds what they share with job_monitor.py (request headers, time helpers, the log), and
browser.py the headless browser a site that refuses scripted requests is read through (Workday)."""
