"""The scheduler service: the design's periodic jobs.

Three things only. `runner.py` is the loop, the job registry and the Redis
cadence gate; `jobs.py` is the thin adaptation of the engines in
`helpdesk.access` and `helpdesk.notifications` to a job's shape; `main.py` is
the HTTP surface every service owes (REQUIREMENTS #17). The work itself lives
in the shared library beside the other engines, so every job is testable by
calling it with a session and a fixed `now` — with no running scheduler
anywhere near the test.
"""
