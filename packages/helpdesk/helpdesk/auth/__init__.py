"""Authentication helpers (argon2 hashing now; login and JWT land in a later row)."""

from helpdesk.auth.passwords import hash_password

__all__ = ["hash_password"]
