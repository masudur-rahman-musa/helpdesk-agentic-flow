"""Access domain: the directory port's read half and the snapshot builder.

`directory.py` is the design's `DirectoryProvider` seam (read half; the
mutation half grows in row 11b with `execute_grant`). `snapshot.py` turns
repository state into an earlier change's `RequestSnapshot`. `requests.py` holds the
access-request reads the tools consume.
"""

from helpdesk.access.directory import DirectoryProvider, SimulatedDirectory
from helpdesk.access.requests import list_requests_for_requester
from helpdesk.access.snapshot import build_request_snapshot

__all__ = [
    "DirectoryProvider",
    "SimulatedDirectory",
    "build_request_snapshot",
    "list_requests_for_requester",
]
