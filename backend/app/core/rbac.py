"""Roles and permissions (spec §14).

A permission matrix rather than role checks scattered through handlers: the question
"who can acknowledge an alarm?" must have one answer, and adding a role must not mean
auditing every endpoint.

The distinctions that matter operationally: a GUARD can acknowledge but not resolve
(closing an incident is a supervisory act), and nobody below ADMIN can change rules,
because a rule change silently disarms a site.
"""

from __future__ import annotations

import enum


class Role(enum.StrEnum):
    OWNER = "OWNER"
    ADMIN = "ADMIN"
    SECURITY_MANAGER = "SECURITY_MANAGER"
    SITE_MANAGER = "SITE_MANAGER"
    SUPERVISOR = "SUPERVISOR"
    GUARD = "GUARD"
    VIEWER = "VIEWER"
    BILLING_ADMIN = "BILLING_ADMIN"


class Permission(enum.StrEnum):
    SITES_VIEW = "sites.view"
    SITES_MANAGE = "sites.manage"
    DEVICES_VIEW = "devices.view"
    DEVICES_MANAGE = "devices.manage"
    CAMERAS_CONFIGURE = "cameras.configure"
    ZONES_MANAGE = "zones.manage"
    EVENTS_VIEW = "events.view"
    ALARMS_VIEW = "alarms.view"
    ALARMS_ACKNOWLEDGE = "alarms.acknowledge"
    ALARMS_RESOLVE = "alarms.resolve"
    RULES_MANAGE = "rules.manage"
    USERS_MANAGE = "users.manage"
    BILLING_MANAGE = "billing.manage"
    ANALYTICS_VIEW = "analytics.view"
    AUDIT_VIEW = "audit.view"


_VIEWER = frozenset({Permission.SITES_VIEW, Permission.EVENTS_VIEW, Permission.ALARMS_VIEW})

_GUARD = _VIEWER | {
    Permission.DEVICES_VIEW,
    # Acknowledge, but not resolve: closing an incident is a supervisory decision.
    Permission.ALARMS_ACKNOWLEDGE,
}

_SUPERVISOR = _GUARD | {Permission.ALARMS_RESOLVE}

_SITE_MANAGER = _SUPERVISOR | {
    Permission.ZONES_MANAGE,
    Permission.CAMERAS_CONFIGURE,
    Permission.ANALYTICS_VIEW,
}

_SECURITY_MANAGER = _SITE_MANAGER | {
    Permission.RULES_MANAGE,
    Permission.DEVICES_MANAGE,
    Permission.AUDIT_VIEW,
}

_ADMIN = _SECURITY_MANAGER | {Permission.SITES_MANAGE, Permission.USERS_MANAGE}

_OWNER = _ADMIN | {Permission.BILLING_MANAGE}

ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.OWNER: frozenset(_OWNER),
    Role.ADMIN: frozenset(_ADMIN),
    Role.SECURITY_MANAGER: frozenset(_SECURITY_MANAGER),
    Role.SITE_MANAGER: frozenset(_SITE_MANAGER),
    Role.SUPERVISOR: frozenset(_SUPERVISOR),
    Role.GUARD: frozenset(_GUARD),
    Role.VIEWER: frozenset(_VIEWER),
    # Billing is a separate axis: money, no cameras.
    Role.BILLING_ADMIN: frozenset({Permission.BILLING_MANAGE, Permission.ANALYTICS_VIEW}),
}


def permissions_for(role: Role | str) -> frozenset[Permission]:
    try:
        resolved = Role(role)
    except ValueError:
        # An unknown role grants nothing rather than defaulting to something usable.
        return frozenset()
    return ROLE_PERMISSIONS.get(resolved, frozenset())


def has_permission(role: Role | str, permission: Permission) -> bool:
    return permission in permissions_for(role)
