"""Argon2id password hashing and verification."""

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from argon2.low_level import Type, hash_secret

_hasher = PasswordHasher()


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """Hash `password` with argon2id using the library's current defaults.

    `salt` exists solely so the seed script can produce byte-identical hashes
    across runs (its determinism contract). Real account flows must leave it
    None: a caller-fixed salt defeats the per-password salting that makes
    hashes non-comparable across users.
    """
    if salt is None:
        return _hasher.hash(password)
    return hash_secret(
        password.encode(),
        salt,
        time_cost=_hasher.time_cost,
        memory_cost=_hasher.memory_cost,
        parallelism=_hasher.parallelism,
        hash_len=_hasher.hash_len,
        type=Type.ID,
    ).decode()


def verify_password(password: str, password_hash: str) -> bool:
    """True iff `password` matches `password_hash`.

    Returns False (never raises) on a mismatch or a malformed/foreign hash,
    so the login path has exactly one failure shape.
    """
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False
