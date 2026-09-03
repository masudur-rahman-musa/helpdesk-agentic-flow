"""Tests for helpdesk.auth.passwords verification.

Expected implementation surface (added to the existing module):

``helpdesk.auth.passwords``
    ``def verify_password(password: str, password_hash: str) -> bool``
        True when ``password`` matches an argon2 hash produced by
        ``hash_password`` (including the salted variant the seed uses).
        False -- never an exception -- on a mismatch or on a string that is
        not a valid argon2 hash at all.
"""

import pytest
from helpdesk.auth.passwords import hash_password, verify_password

PASSWORD = "Correct-Horse-Test-Only-2026!"  # obviously fake, tests only
SEED_SALT = b"0123456789abcdef"  # 16 bytes, mirrors the seed's fixed-salt path


def test_accepts_default_hash() -> None:
    assert verify_password(PASSWORD, hash_password(PASSWORD)) is True


def test_accepts_seeded_salted_hash_format() -> None:
    """The seed stores fixed-salt argon2id hashes; verify must accept them."""
    seeded_hash = hash_password(PASSWORD, salt=SEED_SALT)
    assert seeded_hash.startswith("$argon2")
    assert verify_password(PASSWORD, seeded_hash) is True


@pytest.mark.parametrize(
    "wrong",
    [
        "Wrong-Horse-Test-Only-2026!",
        "correct-horse-test-only-2026!",  # case matters
        PASSWORD + " ",  # trailing space matters
        "",
    ],
)
def test_rejects_wrong_password(wrong: str) -> None:
    assert verify_password(wrong, hash_password(PASSWORD)) is False


@pytest.mark.parametrize(
    "garbage_hash",
    [
        "",
        "not-a-hash",
        "$argon2id$garbage",
        "argon2id$obviously-fake-test-hash",  # the plan-03 fixture placeholder
        "$2b$12$abcdefghijklmnopqrstuv",  # bcrypt-shaped, not argon2
    ],
)
def test_garbage_hash_returns_false_not_raises(garbage_hash: str) -> None:
    assert verify_password(PASSWORD, garbage_hash) is False
