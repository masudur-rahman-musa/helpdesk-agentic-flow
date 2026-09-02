"""Smoke test for the helpdesk package."""

import helpdesk


def test_version_is_nonempty_string() -> None:
    assert isinstance(helpdesk.__version__, str)
    assert helpdesk.__version__ != ""
