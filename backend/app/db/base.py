"""SQLAlchemy declarative base and shared column mixins."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class UUIDPrimaryKey:
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)


class Timestamped:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now(), nullable=True
    )


class TenantScoped:
    """Every business table carries tenant_id from day one (spec §29)."""

    tenant_id: Mapped[uuid.UUID] = mapped_column(index=True)


# TODO(V1-BLOCKER): PHASE 3+ - the model set from spec §36/§37 (tenants, sites,
# devices, cameras, events, alarms, rules, media_assets, ...) with Alembic revisions.
