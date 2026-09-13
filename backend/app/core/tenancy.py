"""Tenant isolation primitives (spec §29).

Every business query is scoped by `tenant_id`. The service layer must never accept a
tenant id from the client: it comes from the verified access token only.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


class TenantIsolationError(PermissionError):
    """Raised when a request touches a resource outside its tenant."""


@dataclass(frozen=True, slots=True)
class TenantContext:
    tenant_id: UUID
    user_id: UUID
    role: str
    permissions: frozenset[str]

    def require(self, permission: str) -> None:
        if permission not in self.permissions:
            raise PermissionError(f"missing permission: {permission}")

    def assert_owns(self, resource_tenant_id: UUID) -> None:
        if resource_tenant_id != self.tenant_id:
            raise TenantIsolationError("cross-tenant access denied")


# TODO(V1-BLOCKER): PHASE 12 - PostgreSQL row-level security policies plus the
# cross-tenant test matrix over every endpoint.
