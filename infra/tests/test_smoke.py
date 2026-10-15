"""Smoke test for the helpdesk package."""

import helpdesk.shared


def test_version_is_nonempty_string() -> None:
    assert isinstance(helpdesk.shared.__version__, str)
    assert helpdesk.shared.__version__ != ""
