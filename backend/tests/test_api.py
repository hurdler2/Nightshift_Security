"""API behaviour against a real database: auth, RBAC and tenant isolation.

The cross-tenant tests are the point of this file. Everything else in the system can
be correct and the product is still unsellable if tenant A can see tenant B's cameras.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.core.config import get_settings
from app.core.security import hash_password
from app.db import models
from tests.conftest import requires_postgres

pytestmark = requires_postgres

PASSWORD = "correct-horse-battery-staple"
NOW = datetime.now(UTC)


def seed(session_factory, *, role: str = "SECURITY_MANAGER"):
    """Create a tenant with one user, one site, one event and one alarm."""

    async def _seed():
        async with session_factory() as session:
            tenant = models.Tenant(id=uuid4(), name=f"Tenant {uuid4().hex[:6]}")
            site = models.Site(id=uuid4(), tenant_id=tenant.id, name="Beton Tesisi")
            user = models.User(
                id=uuid4(),
                tenant_id=tenant.id,
                email=f"{uuid4().hex[:8]}@example.com",
                password_hash=hash_password(PASSWORD),
                role=role,
                full_name="Test User",
            )
            event = models.Event(
                id=uuid4(),
                tenant_id=tenant.id,
                site_id=site.id,
                device_id=uuid4(),
                event_type="person_detected",
                occurred_at=NOW,
                dedup_key="k",
                severity="MEDIUM",
                risk_score=65,
                risk_reasons=["Yasak bölge"],
                status="ALERTED",
            )
            alarm = models.Alarm(
                id=uuid4(),
                tenant_id=tenant.id,
                site_id=site.id,
                event_id=event.id,
                severity="MEDIUM",
                status="ALERTED",
                opened_at=NOW,
            )
            # Flushed in dependency order: alarms.event_id is a plain foreign key with
            # no ORM relationship, so the unit of work has nothing to sort the inserts
            # by and would otherwise write the alarm before its event.
            session.add_all([tenant, site])
            await session.flush()
            session.add_all([user, event])
            await session.flush()
            session.add(alarm)
            await session.commit()
            return {
                "tenant_id": tenant.id,
                "site_id": site.id,
                "email": user.email,
                "user_id": user.id,
                "event_id": event.id,
                "alarm_id": alarm.id,
            }

    return asyncio.run(_seed())


def login(client, email: str, password: str = PASSWORD):
    response = client.post("/v1/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response.json()


def auth(tokens) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


class TestLogin:
    def test_valid_login_returns_a_pair(self, client, session_factory):
        data = seed(session_factory)
        tokens = login(client, data["email"])
        assert tokens["access_token"] and tokens["refresh_token"]
        assert tokens["token_type"] == "Bearer"
        assert tokens["expires_in"] > 0

    def test_wrong_password_is_rejected(self, client, session_factory):
        data = seed(session_factory)
        response = client.post(
            "/v1/auth/login", json={"email": data["email"], "password": "wrong"}
        )
        assert response.status_code == 401

    def test_unknown_email_gives_the_same_answer(self, client, session_factory):
        """No account enumeration: the response must not distinguish the two cases."""
        data = seed(session_factory)
        wrong_password = client.post(
            "/v1/auth/login", json={"email": data["email"], "password": "wrong"}
        )
        unknown_user = client.post(
            "/v1/auth/login", json={"email": "nobody@example.com", "password": "wrong"}
        )
        assert wrong_password.status_code == unknown_user.status_code == 401
        assert wrong_password.json() == unknown_user.json()

    def test_inactive_user_cannot_log_in(self, client, session_factory):
        data = seed(session_factory)

        async def deactivate():
            async with session_factory() as session:
                user = await session.get(models.User, data["user_id"])
                user.is_active = False
                await session.commit()

        asyncio.run(deactivate())
        response = client.post(
            "/v1/auth/login", json={"email": data["email"], "password": PASSWORD}
        )
        assert response.status_code == 401


class TestMe:
    def test_returns_the_caller_with_permissions(self, client, session_factory):
        data = seed(session_factory, role="GUARD")
        response = client.get("/v1/auth/me", headers=auth(login(client, data["email"])))
        assert response.status_code == 200
        body = response.json()
        assert body["email"] == data["email"]
        assert body["role"] == "GUARD"
        assert "alarms.acknowledge" in body["permissions"]
        assert "alarms.resolve" not in body["permissions"]

    def test_without_a_token(self, client):
        assert client.get("/v1/auth/me").status_code == 401

    def test_with_a_garbage_token(self, client):
        response = client.get("/v1/auth/me", headers={"Authorization": "Bearer nonsense"})
        assert response.status_code == 401


class TestRefreshRotation:
    def test_refresh_returns_a_new_pair(self, client, session_factory):
        data = seed(session_factory)
        first = login(client, data["email"])
        response = client.post(
            "/v1/auth/refresh", json={"refresh_token": first["refresh_token"]}
        )
        assert response.status_code == 200
        assert response.json()["refresh_token"] != first["refresh_token"]

    def test_replaying_a_used_token_revokes_the_family(self, client, session_factory):
        """The stolen-token scenario, end to end through the API (spec §14)."""
        data = seed(session_factory)
        first = login(client, data["email"])
        second = client.post(
            "/v1/auth/refresh", json={"refresh_token": first["refresh_token"]}
        ).json()

        replay = client.post(
            "/v1/auth/refresh", json={"refresh_token": first["refresh_token"]}
        )
        assert replay.status_code == 401
        assert "reuse" in replay.json()["detail"]

        # The legitimate client's current token is dead too - by design.
        after = client.post(
            "/v1/auth/refresh", json={"refresh_token": second["refresh_token"]}
        )
        assert after.status_code == 401

    def test_logout_kills_the_session(self, client, session_factory):
        data = seed(session_factory)
        tokens = login(client, data["email"])
        assert (
            client.post("/v1/auth/logout", json={"refresh_token": tokens["refresh_token"]})
        ).status_code == 204
        assert (
            client.post("/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
        ).status_code == 401

    def test_logout_is_quiet_about_unknown_tokens(self, client):
        response = client.post("/v1/auth/logout", json={"refresh_token": "x" * 40})
        assert response.status_code == 204


class TestEventsAndAlarms:
    def test_list_and_fetch(self, client, session_factory):
        data = seed(session_factory)
        headers = auth(login(client, data["email"]))

        events = client.get("/v1/events", headers=headers).json()
        assert [e["id"] for e in events] == [str(data["event_id"])]
        assert events[0]["risk_score"] == 65
        assert events[0]["risk_reasons"] == ["Yasak bölge"]

        alarms = client.get("/v1/alarms", headers=headers).json()
        assert [a["id"] for a in alarms] == [str(data["alarm_id"])]

    def test_filters(self, client, session_factory):
        data = seed(session_factory)
        headers = auth(login(client, data["email"]))
        assert client.get("/v1/events?event_type=person_detected", headers=headers).json()
        assert client.get("/v1/events?event_type=video_loss", headers=headers).json() == []
        assert client.get("/v1/alarms?severity=medium", headers=headers).json()
        assert client.get("/v1/alarms?severity=critical", headers=headers).json() == []

    def test_acknowledge(self, client, session_factory):
        data = seed(session_factory, role="GUARD")
        headers = auth(login(client, data["email"]))
        response = client.post(f"/v1/alarms/{data['alarm_id']}/acknowledge", headers=headers)
        assert response.status_code == 200
        assert response.json()["status"] == "ACKNOWLEDGED"
        assert response.json()["acknowledged_at"] is not None

    def test_acknowledge_is_idempotent(self, client, session_factory):
        """Two guards tapping at the same moment must not race."""
        data = seed(session_factory, role="GUARD")
        headers = auth(login(client, data["email"]))
        first = client.post(f"/v1/alarms/{data['alarm_id']}/acknowledge", headers=headers)
        second = client.post(f"/v1/alarms/{data['alarm_id']}/acknowledge", headers=headers)
        assert second.status_code == 200
        assert first.json()["acknowledged_at"] == second.json()["acknowledged_at"]

    def test_resolve_records_the_code(self, client, session_factory):
        data = seed(session_factory)
        headers = auth(login(client, data["email"]))
        response = client.post(
            f"/v1/alarms/{data['alarm_id']}/resolve",
            headers=headers,
            json={"resolution_code": "TRUE_SECURITY_INCIDENT", "note": "polis çağrıldı"},
        )
        assert response.status_code == 200
        assert response.json()["resolution_code"] == "TRUE_SECURITY_INCIDENT"
        assert response.json()["acknowledged_at"] is not None  # backfilled for the audit

    def test_resolving_twice_conflicts(self, client, session_factory):
        data = seed(session_factory)
        headers = auth(login(client, data["email"]))
        body = {"resolution_code": "FALSE_POSITIVE"}
        client.post(f"/v1/alarms/{data['alarm_id']}/resolve", headers=headers, json=body)
        second = client.post(
            f"/v1/alarms/{data['alarm_id']}/resolve", headers=headers, json=body
        )
        assert second.status_code == 409

    def test_invalid_resolution_code_is_refused(self, client, session_factory):
        data = seed(session_factory)
        headers = auth(login(client, data["email"]))
        response = client.post(
            f"/v1/alarms/{data['alarm_id']}/resolve",
            headers=headers,
            json={"resolution_code": "BECAUSE_I_SAID_SO"},
        )
        assert response.status_code == 422

    def test_resolution_is_audited(self, client, session_factory):
        data = seed(session_factory)
        headers = auth(login(client, data["email"]))
        client.post(
            f"/v1/alarms/{data['alarm_id']}/resolve",
            headers=headers,
            json={"resolution_code": "ANIMAL"},
        )

        async def read_audit():
            from sqlalchemy import select

            async with session_factory() as session:
                return (
                    (
                        await session.execute(
                            select(models.AuditLog).where(
                                models.AuditLog.tenant_id == data["tenant_id"]
                            )
                        )
                    )
                    .scalars()
                    .all()
                )

        entries = asyncio.run(read_audit())
        assert [e.action for e in entries] == ["alarm_resolved"]
        assert entries[0].detail["code"] == "ANIMAL"


class TestPermissions:
    def test_guard_cannot_resolve(self, client, session_factory):
        data = seed(session_factory, role="GUARD")
        headers = auth(login(client, data["email"]))
        response = client.post(
            f"/v1/alarms/{data['alarm_id']}/resolve",
            headers=headers,
            json={"resolution_code": "FALSE_POSITIVE"},
        )
        assert response.status_code == 403
        assert "alarms.resolve" in response.json()["detail"]

    def test_billing_admin_cannot_see_alarms(self, client, session_factory):
        data = seed(session_factory, role="BILLING_ADMIN")
        headers = auth(login(client, data["email"]))
        assert client.get("/v1/alarms", headers=headers).status_code == 403


class TestTenantIsolation:
    """Spec §14: tenant A must not reach tenant B through any endpoint."""

    def test_lists_are_scoped(self, client, session_factory):
        a = seed(session_factory)
        b = seed(session_factory)
        headers = auth(login(client, a["email"]))

        events = client.get("/v1/events", headers=headers).json()
        alarms = client.get("/v1/alarms", headers=headers).json()
        assert [e["id"] for e in events] == [str(a["event_id"])]
        assert str(b["event_id"]) not in [e["id"] for e in events]
        assert [x["id"] for x in alarms] == [str(a["alarm_id"])]

    def test_fetching_another_tenants_alarm_is_a_404(self, client, session_factory):
        """404, not 403: the existence of the row is itself information."""
        a = seed(session_factory)
        b = seed(session_factory)
        headers = auth(login(client, a["email"]))
        assert client.get(f"/v1/alarms/{b['alarm_id']}", headers=headers).status_code == 404
        assert client.get(f"/v1/events/{b['event_id']}", headers=headers).status_code == 404

    def test_acting_on_another_tenants_alarm_is_refused(self, client, session_factory):
        a = seed(session_factory)
        b = seed(session_factory)
        headers = auth(login(client, a["email"]))
        response = client.post(f"/v1/alarms/{b['alarm_id']}/acknowledge", headers=headers)
        assert response.status_code == 404

        async def still_open():
            async with session_factory() as session:
                alarm = await session.get(models.Alarm, b["alarm_id"])
                return alarm.acknowledged_at is None

        assert asyncio.run(still_open())

    def test_a_token_cannot_be_retargeted_at_another_tenant(self, client, session_factory):
        """The tenant comes from the signed token, never from the request."""
        a = seed(session_factory)
        b = seed(session_factory)
        headers = auth(login(client, a["email"]))
        response = client.get(f"/v1/events?site_id={b['site_id']}", headers=headers)
        assert response.status_code == 200
        assert response.json() == []


class TestExpiry:
    def test_an_expired_access_token_is_rejected(self, client, session_factory):
        from app.modules.auth.tokens import create_access_token

        data = seed(session_factory)
        settings = get_settings()
        stale = create_access_token(
            user_id=data["user_id"],
            tenant_id=data["tenant_id"],
            role="ADMIN",
            secret=settings.jwt_secret,
            ttl_minutes=1,
            now=datetime.now(UTC) - timedelta(hours=2),
        )
        response = client.get("/v1/auth/me", headers={"Authorization": f"Bearer {stale}"})
        assert response.status_code == 401
        assert response.json()["detail"] == "token expired"


def test_openapi_lists_the_new_endpoints(client):
    paths = client.get("/openapi.json").json()["paths"]
    for path in ("/v1/auth/login", "/v1/auth/refresh", "/v1/events", "/v1/alarms"):
        assert path in paths


@pytest.mark.parametrize("path", ["/v1/events", "/v1/alarms"])
def test_endpoints_require_authentication(client, path):
    assert client.get(path).status_code == 401
