"""Argon2id password hashing (SPEC-locked algorithm choice).

Verification lands with the login flow in a later row; only hashing is
needed to seed accounts.
"""

from argon2 import PasswordHasher
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
