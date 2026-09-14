"""Shared fixtures.

API and persistence tests run against a real PostgreSQL — the schema uses JSONB and
native UUID columns, so SQLite would test a different database than production. They
skip cleanly when nothing is running, so `pytest` still works on a bare checkout.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.core.deps import get_session
from app.core.security import use_fast_hashing_for_tests
from app.db import models  # noqa: F401 - registers tables on Base.metadata
from app.db.base import Base
from app.main import app

# Password hashing at production cost dominates this suite's runtime and proves
# nothing about it; the hashing parameters themselves are not what these tests check.
use_fast_hashing_for_tests()

ADMIN_URL = os.environ.get(
    "TEST_ADMIN_DATABASE_URL",
    "postgresql+asyncpg://nightshift:nightshift@localhost:5432/nightshift",
)
TEST_DB = os.environ.get("TEST_DATABASE_NAME", "nightshift_test")
TEST_URL = ADMIN_URL.rsplit("/", 1)[0] + f"/{TEST_DB}"


def _postgres_available() -> bool:
    async def check() -> bool:
        engine = create_async_engine(ADMIN_URL, poolclass=NullPool)
        try:
            async with engine.connect():
                return True
        except Exception:
            return False
        finally:
            await engine.dispose()

    try:
        return asyncio.run(check())
    except Exception:
        return False


POSTGRES_UP = _postgres_available()
requires_postgres = pytest.mark.skipif(
    not POSTGRES_UP, reason="PostgreSQL is not running (docker compose up postgres)"
)


@pytest.fixture(scope="session")
def test_engine():
    """Create the test database and schema once, drop the schema at the end."""
    if not POSTGRES_UP:
        pytest.skip("PostgreSQL is not running")

    async def ensure_database() -> None:
        admin = create_async_engine(ADMIN_URL, isolation_level="AUTOCOMMIT", poolclass=NullPool)
        try:
            async with admin.connect() as conn:
                from sqlalchemy import text

                exists = await conn.scalar(
                    text("select 1 from pg_database where datname = :name"), {"name": TEST_DB}
                )
                if not exists:
                    await conn.execute(text(f'create database "{TEST_DB}"'))
        finally:
            await admin.dispose()

    asyncio.run(ensure_database())

    # NullPool: the fixtures and the TestClient each run their own event loop, and a
    # pooled connection created on one loop cannot be reused on another.
    engine = create_async_engine(TEST_URL, poolclass=NullPool)

    async def create_schema() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(create_schema())
    yield engine

    async def teardown() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()

    asyncio.run(teardown())


@pytest.fixture
def session_factory(test_engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(test_engine, expire_on_commit=False, class_=AsyncSession)


@pytest.fixture
def clean_db(test_engine) -> Iterator[None]:
    """Truncate every table between tests so ordering never matters."""

    async def truncate() -> None:
        from sqlalchemy import text

        tables = ", ".join(f'"{t.name}"' for t in reversed(Base.metadata.sorted_tables))
        async with test_engine.begin() as conn:
            await conn.execute(text(f"truncate {tables} restart identity cascade"))

    asyncio.run(truncate())
    yield


@pytest.fixture
def db_session(session_factory, clean_db) -> Iterator[AsyncSession]:
    """A session for tests that talk to the database directly."""
    session = session_factory()
    yield session

    async def close() -> None:
        await session.rollback()
        await session.close()

    asyncio.run(close())


@pytest.fixture
def client(session_factory, clean_db) -> Iterator[TestClient]:
    """TestClient wired to the test database."""

    async def override() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_session] = override
    get_settings.cache_clear()
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
