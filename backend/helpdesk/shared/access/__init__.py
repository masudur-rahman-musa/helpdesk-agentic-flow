"""The access modules that stay close to the grant they write.

`execute.py` is the one place a grant is written, with the design's re-check in
front of it, and `revoke.py` the one place access is taken away. `expiry.py`
holds the scheduler's expire and warn jobs, `leavers.py` the daily leaver
sweep, `review_schedule.py` the review generation and reminders, and
`requests.py` the access-request reads and writes. The approval, directory,
lifecycle, reviews and snapshot halves moved to
`helpdesk.shared.services.access`; these did not, so this package carries no
re-exports of its own.
"""
