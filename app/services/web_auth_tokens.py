"""Random tokens for web-auth sessions/CSRF/invites, and the one-way hash
used to store them.

Session, CSRF, and invite tokens are high-entropy random values, not user
secrets someone might reuse elsewhere - a fast, unsalted SHA-256 hash is
the right primitive here (contrast app.services.web_auth_passwords, which
uses slow Argon2id because passwords are low-entropy and reused). Only the
hash is ever persisted; the raw token is handed to the caller once and
never stored or logged.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

TOKEN_BYTES = 32


def generate_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def tokens_match(raw_token: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_token(raw_token), stored_hash)
