"""Request dependencies: session, current user, permission checks.

Tenant scoping happens here, once. Handlers receive a `CurrentUser` whose `tenant_id`
came from the verified token and never from the request body, so a handler cannot
accidentally trust a client-supplied tenant (spec §14).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.rbac import Permission, Role, permissions_for
from app.db.session import SessionFactory
from app.modules.auth.tokens import TokenError, TokenExpired, decode_access_token

bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True, slots=True)
class CurrentUser:
    user_id: UUID
    tenant_id: UUID
    role: Role
    permissions: frozenset[Permission]

    def can(self, permission: Permission) -> bool:
        return permission in self.permissions

    def assert_owns(self, tenant_id: UUID) -> None:
        """Refuse a cross-tenant reference before it becomes a query."""
        if tenant_id != self.tenant_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                # 404 rather than 403: existence is itself information.
                detail="not found",
            )


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionFactory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def get_app_settings() -> Settings:
    return get_settings()


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    settings: Settings = Depends(get_app_settings),
) -> CurrentUser:
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        claims = decode_access_token(credentials.credentials, settings.jwt_secret)
    except TokenExpired as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="token expired",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        ) from exc
    except TokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token"
        ) from exc

    try:
        role = Role(claims.get("role", ""))
    except ValueError:
        role = Role.VIEWER  # unknown role: the matrix will grant nothing useful

    user = CurrentUser(
        user_id=UUID(claims["sub"]),
        tenant_id=UUID(claims["tid"]),
        role=role,
        permissions=permissions_for(role),
    )
    # Makes tenant_id available to the JSON log formatter for this request.
    request.state.tenant_id = str(user.tenant_id)
    return user


def require(permission: Permission):
    """Dependency factory: `Depends(require(Permission.ALARMS_RESOLVE))`."""

    async def _check(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if not user.can(permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"missing permission: {permission.value}",
            )
        return user

    return _check
