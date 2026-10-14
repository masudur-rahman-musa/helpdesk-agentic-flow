"""The audit modules that stay beside the anchor they keep.

`anchor.py` writes and reads the chain head remembered outside the walk, and
`retention.py` is the other end of that record: an anchor past the window
goes, and the tenant's head never does. The chain core and its writer, verify
and search halves moved to `helpdesk.shared.services.audit`; these did not, so
this package carries no re-exports of its own.
"""
