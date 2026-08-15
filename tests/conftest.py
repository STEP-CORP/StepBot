"""Shared fixtures. Tests run against in-memory aiosqlite via ``Base.metadata.create_all``
(no Postgres, no migrations) — see migrations/README.md.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.infrastructure.database.models import Base
from src.infrastructure.database.uow import UnitOfWork


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture
async def uow(session_factory: async_sessionmaker) -> UnitOfWork:
    """A reusable UnitOfWork — each ``async with uow:`` opens a fresh session."""
    return UnitOfWork(session_factory)


@pytest_asyncio.fixture
async def session_factory_fk() -> AsyncIterator[async_sessionmaker]:
    """Same in-memory sqlite as ``session_factory``, but with real FK enforcement
    (``PRAGMA foreign_keys=ON``) — sqlite ignores ``ondelete=`` (SET NULL/CASCADE)
    unless this is set per-connection, so ``session_factory`` never exercises it.

    Opt-in on purpose: most of this suite doesn't insert/delete in FK-safe order, so
    flipping this on globally would fail unrelated tests. Use only where a test needs
    to observe real ON DELETE behavior (Postgres enforces it always; sqlite doesn't).
    """
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_fk(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture
async def uow_fk(session_factory_fk: async_sessionmaker) -> UnitOfWork:
    """Same as ``uow`` but backed by ``session_factory_fk`` — see its docstring."""
    return UnitOfWork(session_factory_fk)
