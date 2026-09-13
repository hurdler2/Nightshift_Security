"""Password hashing and JWT handling (spec §31)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_hasher = PasswordHasher()

ALGORITHM = "HS256"


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False


def create_access_token(
    subject: str, tenant_id: str, secret: str, *, ttl_minutes: int = 15, **claims: Any
) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": subject,
        "tid": tenant_id,
        "iat": now,
        "exp": now + timedelta(minutes=ttl_minutes),
        "typ": "access",
        **claims,
    }
    return jwt.encode(payload, secret, algorithm=ALGORITHM)


def decode_token(token: str, secret: str) -> dict[str, Any]:
    return jwt.decode(token, secret, algorithms=[ALGORITHM])


# TODO(V1-BLOCKER): PHASE 2 - refresh token rotation with hashed storage, and the
# separate machine identity used by edge gateways (spec §32).
