"""Argon2id password hashing and verification."""

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from argon2.low_level import Type, hash_secret

_hasher = PasswordHasher()


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """Hash `password` with argon2id using the library's current defaults.

    `salt` is only for the seed script, which needs the same hash every run.
    Real accounts must leave it None, or hashes become comparable across users.
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
    """True if `password` matches. Never raises, so login has one failure shape."""
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False
