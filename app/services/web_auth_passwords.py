"""Argon2id password hashing for web-auth accounts (argon2-cffi's default
PasswordHasher profile) - no home-grown crypto.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

MIN_PASSWORD_LENGTH = 10

_hasher = PasswordHasher()


class WeakPasswordError(ValueError):
    """Password does not meet the minimum beta policy."""


def validate_password_policy(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPasswordError(
            f"Пароль должен быть не короче {MIN_PASSWORD_LENGTH} символов."
        )


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False
