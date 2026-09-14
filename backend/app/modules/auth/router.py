"""Authentication endpoints (spec §16).

Login is deliberately uninformative: a wrong password and an unknown address produce
the same response and take the same time, so the endpoint cannot be used to enumerate
who has an account.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.deps import CurrentUser, get_app_settings, get_current_user, get_session
from app.core.rbac import permissions_for
from app.core.security import hash_password, verify_password
from app.db import models
from app.modules.auth.tokens import (
    IssuedTokens,
    RefreshTokenRecord,
    hash_refresh_token,
    issue_tokens,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/auth", tags=["auth"])

#: Verified against this when the account does not exist, so a missing user and a
#: wrong password cost the same time (Argon2 is deliberately slow).
_DUMMY_HASH = hash_password("nightshift-timing-equalizer")


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=10, max_length=500)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "Bearer"  # noqa: S105 - the OAuth scheme name, not a secret
    expires_in: int


class MeResponse(BaseModel):
    id: UUID
    tenant_id: UUID
    email: str
    full_name: str | None
    role: str
    permissions: list[str]


@router.post("/login", response_model=TokenResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_app_settings),
) -> TokenResponse:
    user = await session.scalar(
        select(models.User).where(models.User.email == payload.email.lower())
    )

    if user is None:
        verify_password(payload.password, _DUMMY_HASH)
        raise _invalid_credentials()
    if not verify_password(payload.password, user.password_hash):
        log.warning("failed login for %s", payload.email)
        raise _invalid_credentials()
    if not user.is_active:
        raise _invalid_credentials()

    tokens = issue_tokens(
        user_id=user.id,
        tenant_id=user.tenant_id,
        role=user.role,
        secret=settings.jwt_secret,
        access_ttl_minutes=settings.jwt_access_ttl_minutes,
        refresh_ttl_days=settings.jwt_refresh_ttl_days,
    )
    await _store_refresh(session, tokens.record, request)
    return TokenResponse(**tokens.to_dict())


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    payload: RefreshRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_app_settings),
) -> TokenResponse:
    token_hash = hash_refresh_token(payload.refresh_token)
    record = await session.scalar(
        select(models.RefreshToken).where(models.RefreshToken.token_hash == token_hash)
    )
    if record is None:
        raise _invalid_credentials("invalid refresh token")

    now = datetime.now(UTC)

    if record.revoked_at is not None:
        # Replay of a retired token: assume the family is compromised (spec §14).
        await _revoke_family(session, record.family_id, now)
        # Committed here, not left to the dependency: raising rolls the session back,
        # which would discard the revocation and leave the stolen family usable.
        await session.commit()
        log.warning("refresh token reuse detected for user %s", record.user_id)
        raise _invalid_credentials("refresh token reuse detected; please sign in again")

    if record.expires_at <= now:
        record.revoked_at = now
        await session.commit()  # same reason: the raise below would roll this back
        raise _invalid_credentials("refresh token expired")

    user = await session.get(models.User, record.user_id)
    if user is None or not user.is_active:
        raise _invalid_credentials()

    tokens = issue_tokens(
        user_id=user.id,
        tenant_id=user.tenant_id,
        role=user.role,
        secret=settings.jwt_secret,
        family_id=record.family_id,
        access_ttl_minutes=settings.jwt_access_ttl_minutes,
        refresh_ttl_days=settings.jwt_refresh_ttl_days,
        now=now,
    )
    record.revoked_at = now
    record.replaced_by = tokens.record.id
    await _store_refresh(session, tokens.record, request)
    return TokenResponse(**tokens.to_dict())


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    payload: RefreshRequest,
    session: AsyncSession = Depends(get_session),
) -> None:
    record = await session.scalar(
        select(models.RefreshToken).where(
            models.RefreshToken.token_hash == hash_refresh_token(payload.refresh_token)
        )
    )
    # Always 204: telling a caller whether a token existed is free information.
    if record is not None and record.revoked_at is None:
        record.revoked_at = datetime.now(UTC)


@router.get("/me", response_model=MeResponse)
async def me(
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> MeResponse:
    row = await session.get(models.User, user.user_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    return MeResponse(
        id=row.id,
        tenant_id=row.tenant_id,
        email=row.email,
        full_name=row.full_name,
        role=row.role,
        permissions=sorted(p.value for p in permissions_for(row.role)),
    )


def _invalid_credentials(detail: str = "invalid credentials") -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


async def _store_refresh(
    session: AsyncSession, record: RefreshTokenRecord, request: Request
) -> None:
    session.add(
        models.RefreshToken(
            id=record.id,
            tenant_id=record.tenant_id,
            user_id=record.user_id,
            token_hash=record.token_hash,
            family_id=record.family_id,
            issued_at=record.issued_at,
            expires_at=record.expires_at,
            user_agent=(request.headers.get("user-agent") or "")[:300] or None,
        )
    )
    await session.flush()


async def _revoke_family(session: AsyncSession, family_id: UUID, now: datetime) -> None:
    rows = (
        (
            await session.execute(
                select(models.RefreshToken).where(
                    models.RefreshToken.family_id == family_id,
                    models.RefreshToken.revoked_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        row.revoked_at = now


def issued_to_response(tokens: IssuedTokens) -> TokenResponse:  # pragma: no cover - helper
    return TokenResponse(**tokens.to_dict())


__all__ = ["router", "uuid4"]
