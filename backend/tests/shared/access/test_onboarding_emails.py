"""The email pattern, its length and the finder of typed addresses.

Pure: plain strings. No service, no waiting.
"""

import pytest

from helpdesk.shared.access.onboarding import EMAIL_MAX_LENGTH, EMAIL_PATTERN, emails_in


def test_emails_in_strips_trailing_punctuation_and_skips_placeholders() -> None:
    """Catches a finder that keeps the dot or comma after an address (the
    held email would not match the pattern), or that reports a placeholder the
    gateway put in place of the real address.

    "x <EMAIL_1>, a@b.example." must find only `a@b.example`.
    """
    assert emails_in("x <EMAIL_1>, a@b.example.") == ["a@b.example"]
    assert emails_in("mara.quill@cuan.example, into Engineering") == ["mara.quill@cuan.example"]


def test_emails_in_finds_nothing_in_text_without_an_address() -> None:
    """Unhappy path. Catches a finder that answers a word or a placeholder when
    there is no address, so onboarding would hold garbage."""
    assert emails_in("Onboard Mara Quill into Engineering") == []
    assert emails_in("") == []


def test_emails_in_finds_every_address_in_order() -> None:
    """Catches a finder that stops at the first address, which would hide the
    two-address case the loop must send back."""
    assert emails_in("a.one@cuan.example or b.two@cuan.example") == [
        "a.one@cuan.example",
        "b.two@cuan.example",
    ]


@pytest.mark.parametrize("tail", ["\n", "\r\n", " "])
def test_a_trailing_newline_crlf_or_space_never_rides_into_an_address(tail: str) -> None:
    """Catches a finder that returns the whitespace after an address, which would
    then be stored in the directory."""
    assert emails_in(f"mara@cuan.example{tail}") == ["mara@cuan.example"]


@pytest.mark.parametrize("tail", ["\n", "\r\n", " "])
def test_the_pattern_refuses_a_trailing_newline_crlf_or_space(tail: str) -> None:
    """Catches a pattern checked with `match` or `$`, which lets a trailing
    newline through."""
    assert EMAIL_PATTERN.fullmatch("mara@cuan.example") is not None
    assert EMAIL_PATTERN.fullmatch(f"mara@cuan.example{tail}") is None


def test_the_email_length_is_the_column_length() -> None:
    """Catches a copied length that drifted from the column: 320 is the limit
    the address column holds, and a wrong number would store or refuse the
    wrong addresses."""
    assert EMAIL_MAX_LENGTH == 320
