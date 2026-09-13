"""Access and refresh tokens with rotation (spec §14, §19.2).

Refresh tokens are stored **hashed**, exactly like passwords: a dump of the token
table must not let anyone mint sessions. Each refresh rotates — the presented token is
retired and a new one issued — and re-presenting a retired token is treated as theft,
which revokes the whole family rather than just failing the call.

That last part is the reason this is a module and not three lines in a handler: silent
failure on a replayed token leaves the attacker holding a working session.
"""

from __future__ import annotations

import enum
import hashlib
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import jwt

ALGORITHM = "HS256"
#: Tolerance for clock skew between API nodes. Without it, a token minted on a node
#: whose clock is a second ahead is rejected as "not yet valid" by the next node.
CLOCK_SKEW_LEEWAY_SECONDS = 30
ACCESS_TTL_MINUTES = 15
REFRESH_TTL_DAYS = 30
#: 256 bits of entropy; the token is never stored, only its hash.
REFRESH_TOKEN_BYTES = 32


class TokenType(enum.StrEnum):
    ACCESS = "access"
    REFRESH = "refresh"


class TokenError(Exception):
    pass


class TokenExpired(TokenError):
    pass


class TokenInvalid(TokenError):
    pass


class TokenReuseDetected(TokenError):
    """A retired refresh token was presented: assume theft and revoke the family."""


def hash_refresh_token(token: str) -> str:
    """SHA-256 is right here: the token is already 256 bits of random, so there is
    nothing to brute-force and a slow KDF would only cost latency on every refresh."""
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(slots=True)
class RefreshTokenRecord:
    """What the database keeps. Never the token itself."""

    id: UUID
    user_id: UUID
    tenant_id: UUID
    token_hash: str
    family_id: UUID
    issued_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None
    replaced_by: UUID | None = None

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None

    def is_expired(self, now: datetime) -> bool:
        return now >= self.expires_at


@dataclass(slots=True)
class IssuedTokens:
    access_token: str
    refresh_token: str
    record: RefreshTokenRecord
    expires_in: int
    token_type: str = "Bearer"  # noqa: S105 - the OAuth scheme name, not a secret

    def to_dict(self) -> dict[str, Any]:
        # The refresh token is returned once, to the caller, and never logged.
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "token_type": self.token_type,
            "expires_in": self.expires_in,
        }


def create_access_token(
    *,
    user_id: UUID,
    tenant_id: UUID,
    role: str,
    secret: str,
    ttl_minutes: int = ACCESS_TTL_MINUTES,
    now: datetime | None = None,
) -> str:
    issued = now or datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "tid": str(tenant_id),
        "role": role,
        "typ": TokenType.ACCESS.value,
        "iat": issued,
        "exp": issued + timedelta(minutes=ttl_minutes),
        "jti": str(uuid4()),
    }
    return jwt.encode(payload, secret, algorithm=ALGORITHM)


def decode_access_token(token: str, secret: str) -> dict[str, Any]:
    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=[ALGORITHM],
            leeway=CLOCK_SKEW_LEEWAY_SECONDS,
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenExpired("access token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenInvalid(str(exc)) from exc

    if claims.get("typ") != TokenType.ACCESS.value:
        # A refresh token must never be accepted as an access token.
        raise TokenInvalid("wrong token type")
    return claims


def issue_tokens(
    *,
    user_id: UUID,
    tenant_id: UUID,
    role: str,
    secret: str,
    family_id: UUID | None = None,
    access_ttl_minutes: int = ACCESS_TTL_MINUTES,
    refresh_ttl_days: int = REFRESH_TTL_DAYS,
    now: datetime | None = None,
) -> IssuedTokens:
    issued = now or datetime.now(UTC)
    refresh = secrets.token_urlsafe(REFRESH_TOKEN_BYTES)
    record = RefreshTokenRecord(
        id=uuid4(),
        user_id=user_id,
        tenant_id=tenant_id,
        token_hash=hash_refresh_token(refresh),
        family_id=family_id or uuid4(),
        issued_at=issued,
        expires_at=issued + timedelta(days=refresh_ttl_days),
    )
    return IssuedTokens(
        access_token=create_access_token(
            user_id=user_id,
            tenant_id=tenant_id,
            role=role,
            secret=secret,
            ttl_minutes=access_ttl_minutes,
            now=issued,
        ),
        refresh_token=refresh,
        record=record,
        expires_in=access_ttl_minutes * 60,
    )


@dataclass(slots=True)
class TokenFamilyStore:
    """In-memory refresh store. The database implementation keeps the same rules."""

    records: dict[str, RefreshTokenRecord] = field(default_factory=dict)

    def add(self, record: RefreshTokenRecord) -> None:
        self.records[record.token_hash] = record

    def find(self, token: str) -> RefreshTokenRecord | None:
        return self.records.get(hash_refresh_token(token))

    def revoke_family(self, family_id: UUID, now: datetime) -> int:
        revoked = 0
        for record in self.records.values():
            if record.family_id == family_id and record.is_active:
                record.revoked_at = now
                revoked += 1
        return revoked

    def rotate(
        self,
        presented: str,
        *,
        secret: str,
        role: str,
        now: datetime | None = None,
        access_ttl_minutes: int = ACCESS_TTL_MINUTES,
        refresh_ttl_days: int = REFRESH_TTL_DAYS,
    ) -> IssuedTokens:
        """Exchange a refresh token for a new pair, retiring the old one."""
        moment = now or datetime.now(UTC)
        record = self.find(presented)
        if record is None:
            raise TokenInvalid("unknown refresh token")

        if not record.is_active:
            # Already rotated once. Either a replay or a stolen token being used
            # alongside the legitimate client - both mean the family is compromised.
            self.revoke_family(record.family_id, moment)
            raise TokenReuseDetected("refresh token reuse; session family revoked")

        if record.is_expired(moment):
            record.revoked_at = moment
            raise TokenExpired("refresh token expired")

        issued = issue_tokens(
            user_id=record.user_id,
            tenant_id=record.tenant_id,
            role=role,
            secret=secret,
            family_id=record.family_id,
            access_ttl_minutes=access_ttl_minutes,
            refresh_ttl_days=refresh_ttl_days,
            now=moment,
        )
        record.revoked_at = moment
        record.replaced_by = issued.record.id
        self.add(issued.record)
        return issued

    def revoke(self, token: str, now: datetime | None = None) -> bool:
        """Log out one session."""
        record = self.find(token)
        if record is None or not record.is_active:
            return False
        record.revoked_at = now or datetime.now(UTC)
        return True

    def purge_expired(self, now: datetime) -> int:
        stale = [h for h, r in self.records.items() if r.is_expired(now)]
        for token_hash in stale:
            del self.records[token_hash]
        return len(stale)
