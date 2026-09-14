"""Async engine/session factory.

The request-scoped dependency lives in ``app.core.deps``, which commits on a clean
exit. Deliberately not duplicated here: two `get_session` helpers, one of which
silently never commits, is a trap worth not setting.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings

_settings = get_settings()

engine = create_async_engine(_settings.database_url, pool_pre_ping=True, future=True)
SessionFactory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
