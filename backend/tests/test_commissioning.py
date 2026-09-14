"""The installer's day: site, recorder, credentials, cameras, zones, rules.

Every test here runs against a real PostgreSQL through the real API, because the
things worth checking — a rotated SMTP password, a unique channel, a zone replaced as
a set — are constraints the database enforces, not assertions Python can fake.
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

from sqlalchemy import select

from app.core.security import hash_password, verify_password
from app.db import models
from tests.conftest import requires_postgres
from tests.test_api import PASSWORD, auth, login

pytestmark = requires_postgres


def seed_account(session_factory, role: str = "ADMIN") -> dict:
    """A tenant with one user of the given role. No site: the tests create those.

    ADMIN by default because creating a site is an account-level act in the matrix
    (spec §14); a SECURITY_MANAGER configures the sites they were given.
    """

    async def _seed():
        async with session_factory() as session:
            tenant = models.Tenant(id=uuid4(), name=f"Tenant {uuid4().hex[:6]}")
            user = models.User(
                id=uuid4(),
                tenant_id=tenant.id,
                email=f"{uuid4().hex[:8]}@example.com",
                password_hash=hash_password(PASSWORD),
                role=role,
                full_name="Kurulumcu",
            )
            session.add(tenant)
            await session.flush()
            session.add(user)
            await session.commit()
            return {"tenant_id": tenant.id, "email": user.email, "user_id": user.id}

    return asyncio.run(_seed())


def headers(client, data) -> dict[str, str]:
    return auth(login(client, data["email"]))


def make_site(client, head, name: str = "Beton Tesisi") -> dict:
    response = client.post(
        "/v1/sites", json={"name": name, "timezone": "Europe/Istanbul"}, headers=head
    )
    assert response.status_code == 201, response.text
    return response.json()


def make_device(client, head, site_id: str, name: str = "SANTIYE-A-XVR") -> dict:
    response = client.post(
        f"/v1/sites/{site_id}/devices",
        json={"name": name, "model": "DH-XVR5108HS-I3/T", "channel_count": 8},
        headers=head,
    )
    assert response.status_code == 201, response.text
    return response.json()


class TestSites:
    def test_create_list_and_fetch(self, client, session_factory):
        head = headers(client, seed_account(session_factory))
        site = make_site(client, head)
        assert site["is_armed"] is True

        listed = client.get("/v1/sites", headers=head).json()
        assert [s["id"] for s in listed] == [site["id"]]
        assert client.get(f"/v1/sites/{site['id']}", headers=head).json()["name"] == "Beton Tesisi"

    def test_an_unknown_timezone_is_refused(self, client, session_factory):
        """Every night schedule resolves through this field."""
        head = headers(client, seed_account(session_factory))
        response = client.post(
            "/v1/sites", json={"name": "X", "timezone": "Mars/Olympus"}, headers=head
        )
        assert response.status_code == 422

    def test_disarming_is_audited(self, client, session_factory):
        account = seed_account(session_factory)
        head = headers(client, account)
        site = make_site(client, head)

        response = client.patch(f"/v1/sites/{site['id']}", json={"is_armed": False}, headers=head)
        assert response.status_code == 200
        assert response.json()["is_armed"] is False

        async def actions():
            async with session_factory() as session:
                rows = (
                    (
                        await session.execute(
                            select(models.AuditLog).where(
                                models.AuditLog.tenant_id == account["tenant_id"]
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                return [row.action for row in rows]

        assert "site_disarmed" in asyncio.run(actions())

    def test_a_guard_cannot_create_a_site(self, client, session_factory):
        head = headers(client, seed_account(session_factory, role="GUARD"))
        assert client.post("/v1/sites", json={"name": "X"}, headers=head).status_code == 403

    def test_another_tenants_site_is_a_404(self, client, session_factory):
        mine = headers(client, seed_account(session_factory))
        theirs = headers(client, seed_account(session_factory))
        site = make_site(client, theirs, name="Onların Şantiyesi")
        assert client.get(f"/v1/sites/{site['id']}", headers=mine).status_code == 404


class TestDevicesAndCredentials:
    def test_register_a_recorder(self, client, session_factory):
        head = headers(client, seed_account(session_factory))
        site = make_site(client, head)
        device = make_device(client, head, site["id"])
        assert device["model"] == "DH-XVR5108HS-I3/T"
        assert client.get(f"/v1/devices/{device['id']}", headers=head).status_code == 200

    def test_the_smtp_password_is_returned_once_and_stored_hashed(
        self, client, session_factory
    ):
        head = headers(client, seed_account(session_factory))
        device = make_device(client, head, make_site(client, head)["id"])

        response = client.post(f"/v1/devices/{device['id']}/smtp-account", headers=head)
        assert response.status_code == 201
        issued = response.json()
        assert len(issued["password"]) >= 20

        async def stored():
            async with session_factory() as session:
                return await session.scalar(
                    select(models.DeviceSmtpAccount).where(
                        models.DeviceSmtpAccount.username == issued["username"]
                    )
                )

        account = asyncio.run(stored())
        assert account.password_hash != issued["password"]
        assert verify_password(issued["password"], account.password_hash)

    def test_rotating_deactivates_the_previous_account(self, client, session_factory):
        """A recorder still holding the old password must fail loudly, not share one."""
        head = headers(client, seed_account(session_factory))
        device = make_device(client, head, make_site(client, head)["id"])

        first = client.post(f"/v1/devices/{device['id']}/smtp-account", headers=head).json()
        second = client.post(f"/v1/devices/{device['id']}/smtp-account", headers=head).json()
        assert first["username"] != second["username"]
        assert first["password"] != second["password"]

        async def states():
            async with session_factory() as session:
                rows = (
                    (
                        await session.execute(
                            select(models.DeviceSmtpAccount).where(
                                models.DeviceSmtpAccount.device_id == UUID(device["id"])
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                return {row.username: row.is_active for row in rows}

        active = asyncio.run(states())
        assert active[first["username"]] is False
        assert active[second["username"]] is True

    def test_a_guard_cannot_mint_device_credentials(self, client, session_factory):
        manager = headers(client, seed_account(session_factory))
        device = make_device(client, manager, make_site(client, manager)["id"])
        guard = headers(client, seed_account(session_factory, role="GUARD"))
        response = client.post(f"/v1/devices/{device['id']}/smtp-account", headers=guard)
        # 403 from the permission gate, which runs before any row is loaded: a guard
        # is refused for what they are, without the API confirming the id exists.
        assert response.status_code == 403


class TestCamerasAndZones:
    def test_a_channel_can_only_have_one_camera(self, client, session_factory):
        head = headers(client, seed_account(session_factory))
        device = make_device(client, head, make_site(client, head)["id"])
        body = {"name": "Depo Arka", "channel_number": 3}

        assert client.post(f"/v1/devices/{device['id']}/cameras", json=body, headers=head).status_code == 201
        second = client.post(f"/v1/devices/{device['id']}/cameras", json=body, headers=head)
        assert second.status_code == 409

    def test_zones_must_be_normalized(self, client, session_factory):
        """Pixel coordinates would silently move the zone on another resolution."""
        head = headers(client, seed_account(session_factory))
        device = make_device(client, head, make_site(client, head)["id"])
        camera = client.post(
            f"/v1/devices/{device['id']}/cameras",
            json={"name": "Depo", "channel_number": 1},
            headers=head,
        ).json()

        response = client.put(
            f"/v1/cameras/{camera['id']}/zones",
            json=[{"name": "Yasak", "zone_type": "RESTRICTED", "points": [[0, 0], [1920, 0], [1920, 1080]]}],
            headers=head,
        )
        assert response.status_code == 422

    def test_replacing_zones_replaces_the_whole_set(self, client, session_factory):
        head = headers(client, seed_account(session_factory))
        device = make_device(client, head, make_site(client, head)["id"])
        camera = client.post(
            f"/v1/devices/{device['id']}/cameras",
            json={"name": "Depo", "channel_number": 1},
            headers=head,
        ).json()
        triangle = [[0.1, 0.1], [0.9, 0.1], [0.5, 0.9]]

        client.put(
            f"/v1/cameras/{camera['id']}/zones",
            json=[
                {"name": "Yasak", "zone_type": "RESTRICTED", "points": triangle},
                {"name": "Yol", "zone_type": "IGNORE", "points": triangle},
            ],
            headers=head,
        )
        after = client.put(
            f"/v1/cameras/{camera['id']}/zones",
            json=[{"name": "Sadece Yasak", "zone_type": "RESTRICTED", "points": triangle}],
            headers=head,
        )
        assert after.status_code == 200
        names = [z["name"] for z in client.get(f"/v1/cameras/{camera['id']}/zones", headers=head).json()]
        assert names == ["Sadece Yasak"]

    def test_a_guard_cannot_redraw_zones(self, client, session_factory):
        account = seed_account(session_factory)
        head = headers(client, account)
        device = make_device(client, head, make_site(client, head)["id"])
        camera = client.post(
            f"/v1/devices/{device['id']}/cameras",
            json={"name": "Depo", "channel_number": 1},
            headers=head,
        ).json()

        async def demote():
            async with session_factory() as session:
                user = await session.get(models.User, account["user_id"])
                user.role = "GUARD"
                await session.commit()

        asyncio.run(demote())
        guard_head = headers(client, account)
        response = client.put(
            f"/v1/cameras/{camera['id']}/zones",
            json=[{"name": "X", "zone_type": "NORMAL", "points": [[0.1, 0.1], [0.2, 0.1], [0.2, 0.2]]}],
            headers=guard_head,
        )
        assert response.status_code == 403


class TestLiveSessions:
    def test_live_view_answers_501_with_the_dmss_handoff(self, client, session_factory):
        """V1 is honest about this: there is no inbound path through CGNAT (spec §17)."""
        head = headers(client, seed_account(session_factory))
        device = make_device(client, head, make_site(client, head)["id"])
        camera = client.post(
            f"/v1/devices/{device['id']}/cameras",
            json={"name": "Depo", "channel_number": 2},
            headers=head,
        ).json()

        response = client.post(f"/v1/cameras/{camera['id']}/live-sessions", headers=head)
        assert response.status_code == 501
        body = response.json()
        assert body["handoff"]["scheme"] == "dmss"
        assert body["handoff"]["channel"] == 2


class TestRules:
    def test_create_update_and_delete(self, client, session_factory):
        head = headers(client, seed_account(session_factory))
        site = make_site(client, head)

        created = client.post(
            "/v1/rules",
            json={
                "name": "Gece insan alarmı",
                "site_id": site["id"],
                "event_types": ["person_detected"],
                "schedule": {"timezone": "Europe/Istanbul", "start": "19:00", "end": "07:00"},
                "min_severity": "MEDIUM",
                "actions": ["push", "snapshot"],
            },
            headers=head,
        )
        assert created.status_code == 201, created.text
        rule = created.json()

        updated = client.patch(f"/v1/rules/{rule['id']}", json={"name": "X", "enabled": False}, headers=head)
        assert updated.status_code == 200
        assert updated.json()["enabled"] is False

        assert client.delete(f"/v1/rules/{rule['id']}", headers=head).status_code == 204
        assert client.get("/v1/rules", headers=head).json() == []

    def test_an_unknown_severity_is_refused(self, client, session_factory):
        head = headers(client, seed_account(session_factory))
        response = client.post(
            "/v1/rules", json={"name": "X", "min_severity": "VERY_BAD"}, headers=head
        )
        assert response.status_code == 422

    def test_a_supervisor_cannot_change_the_rules(self, client, session_factory):
        head = headers(client, seed_account(session_factory, role="SUPERVISOR"))
        assert client.post("/v1/rules", json={"name": "X"}, headers=head).status_code == 403


class TestPushRegistration:
    def test_a_guard_registers_their_own_phone(self, client, session_factory):
        head = headers(client, seed_account(session_factory, role="GUARD"))
        response = client.post(
            "/v1/push-devices", json={"platform": "android", "token": "a" * 40}, headers=head
        )
        assert response.status_code == 201
        assert response.json()["platform"] == "android"

    def test_a_reused_token_moves_to_the_new_owner(self, client, session_factory):
        """A phone handed to the next shift must notify its new holder, not the old one."""
        first = seed_account(session_factory, role="GUARD")
        second = seed_account(session_factory, role="GUARD")
        token = "shared-handset-" + uuid4().hex

        created = client.post(
            "/v1/push-devices", json={"platform": "ios", "token": token}, headers=headers(client, first)
        ).json()
        moved = client.post(
            "/v1/push-devices", json={"platform": "ios", "token": token}, headers=headers(client, second)
        ).json()
        assert moved["id"] == created["id"]

        async def owner():
            async with session_factory() as session:
                row = await session.scalar(
                    select(models.PushDevice).where(models.PushDevice.token == token)
                )
                return row.user_id

        assert asyncio.run(owner()) == second["user_id"]

    def test_nobody_can_unregister_someone_elses_phone(self, client, session_factory):
        """Otherwise silencing a colleague would be a single API call."""
        mine = seed_account(session_factory, role="GUARD")
        theirs = seed_account(session_factory, role="GUARD")
        registered = client.post(
            "/v1/push-devices",
            json={"platform": "ios", "token": "t" * 40},
            headers=headers(client, theirs),
        ).json()

        response = client.delete(f"/v1/push-devices/{registered['id']}", headers=headers(client, mine))
        assert response.status_code == 404
