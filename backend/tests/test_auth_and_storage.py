"""RBAC, token rotation, and media storage.

The storage tests run against the MinIO in the dev stack when it is reachable and skip
otherwise, so the suite stays runnable on a laptop with nothing started.
"""

from __future__ import annotations

import itertools
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.core.rbac import Permission, Role, has_permission, permissions_for
from app.integrations.object_storage.media_store import (
    RETENTION_DAYS,
    MediaKind,
    MediaRejected,
    MediaStore,
    build_client,
    build_key,
    expiry_for,
    validate,
)
from app.modules.auth.tokens import (
    TokenExpired,
    TokenFamilyStore,
    TokenInvalid,
    TokenReuseDetected,
    create_access_token,
    decode_access_token,
    hash_refresh_token,
    issue_tokens,
)

SECRET = "test-secret-at-least-32-bytes-long!!"
NOW = datetime(2026, 9, 14, 23, 14, tzinfo=UTC)
#: Token tests need an issue time in the past: PyJWT rejects a future `iat`.
TOKEN_NOW = datetime(2026, 9, 12, 23, 14, tzinfo=UTC)
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 4096 + b"\xff\xd9"
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2048


class TestRbac:
    def test_guard_can_acknowledge_but_not_resolve(self):
        """Closing an incident is a supervisory act, not a night-watch one."""
        assert has_permission(Role.GUARD, Permission.ALARMS_ACKNOWLEDGE)
        assert not has_permission(Role.GUARD, Permission.ALARMS_RESOLVE)
        assert has_permission(Role.SUPERVISOR, Permission.ALARMS_RESOLVE)

    def test_only_security_manager_and_above_change_rules(self):
        """A rule change can silently disarm a site."""
        for role in (Role.VIEWER, Role.GUARD, Role.SUPERVISOR, Role.SITE_MANAGER):
            assert not has_permission(role, Permission.RULES_MANAGE)
        for role in (Role.SECURITY_MANAGER, Role.ADMIN, Role.OWNER):
            assert has_permission(role, Permission.RULES_MANAGE)

    def test_billing_admin_sees_money_not_cameras(self):
        assert has_permission(Role.BILLING_ADMIN, Permission.BILLING_MANAGE)
        assert not has_permission(Role.BILLING_ADMIN, Permission.ALARMS_VIEW)
        assert not has_permission(Role.BILLING_ADMIN, Permission.SITES_VIEW)

    def test_owner_is_a_superset_of_admin(self):
        assert permissions_for(Role.ADMIN) <= permissions_for(Role.OWNER)

    def test_roles_are_cumulative_up_the_chain(self):
        chain = [
            Role.VIEWER,
            Role.GUARD,
            Role.SUPERVISOR,
            Role.SITE_MANAGER,
            Role.SECURITY_MANAGER,
            Role.ADMIN,
            Role.OWNER,
        ]
        for lower, higher in itertools.pairwise(chain):
            assert permissions_for(lower) < permissions_for(higher)

    def test_unknown_role_grants_nothing(self):
        """Fail closed: a typo in a role name must not hand out access."""
        assert permissions_for("SUPER_DUPER_ADMIN") == frozenset()


class TestAccessTokens:
    def test_round_trip(self):
        # Issued at the real current time: the decode path checks exp against it.
        user, tenant = uuid4(), uuid4()
        token = create_access_token(user_id=user, tenant_id=tenant, role="GUARD", secret=SECRET)
        claims = decode_access_token(token, SECRET)
        assert claims["sub"] == str(user)
        assert claims["tid"] == str(tenant)
        assert claims["role"] == "GUARD"

    def test_expired_token_is_rejected(self):
        token = create_access_token(
            user_id=uuid4(),
            tenant_id=uuid4(),
            role="GUARD",
            secret=SECRET,
            ttl_minutes=1,
            now=TOKEN_NOW - timedelta(hours=2),
        )
        with pytest.raises(TokenExpired):
            decode_access_token(token, SECRET)

    def test_another_secret_is_rejected(self):
        token = create_access_token(user_id=uuid4(), tenant_id=uuid4(), role="GUARD", secret=SECRET)
        with pytest.raises(TokenInvalid):
            decode_access_token(token, "another-secret-at-least-32-bytes!!")

    def test_a_refresh_token_cannot_be_used_as_an_access_token(self):
        import jwt

        forged = jwt.encode({"sub": "x", "typ": "refresh"}, SECRET, algorithm="HS256")
        with pytest.raises(TokenInvalid, match="wrong token type"):
            decode_access_token(forged, SECRET)


class TestRefreshRotation:
    def issue(self, store: TokenFamilyStore):
        tokens = issue_tokens(
            user_id=uuid4(), tenant_id=uuid4(), role="GUARD", secret=SECRET, now=TOKEN_NOW
        )
        store.add(tokens.record)
        return tokens

    def test_the_token_is_never_stored_in_the_clear(self):
        store = TokenFamilyStore()
        tokens = self.issue(store)
        assert tokens.refresh_token not in store.records
        assert hash_refresh_token(tokens.refresh_token) in store.records

    def test_rotation_issues_a_new_pair(self):
        store = TokenFamilyStore()
        first = self.issue(store)
        second = store.rotate(first.refresh_token, secret=SECRET, role="GUARD", now=TOKEN_NOW)
        assert second.refresh_token != first.refresh_token
        assert second.record.family_id == first.record.family_id

    def test_the_old_token_stops_working(self):
        store = TokenFamilyStore()
        first = self.issue(store)
        store.rotate(first.refresh_token, secret=SECRET, role="GUARD", now=TOKEN_NOW)
        with pytest.raises(TokenReuseDetected):
            store.rotate(first.refresh_token, secret=SECRET, role="GUARD", now=TOKEN_NOW)

    def test_reuse_revokes_the_whole_family(self):
        """A replayed token means someone else has it; one failed call is not enough."""
        store = TokenFamilyStore()
        first = self.issue(store)
        second = store.rotate(first.refresh_token, secret=SECRET, role="GUARD", now=TOKEN_NOW)

        with pytest.raises(TokenReuseDetected):
            store.rotate(first.refresh_token, secret=SECRET, role="GUARD", now=TOKEN_NOW)

        # The thief's replay also killed the legitimate client's current token.
        with pytest.raises(TokenReuseDetected):
            store.rotate(second.refresh_token, secret=SECRET, role="GUARD", now=TOKEN_NOW)

    def test_expired_refresh_is_rejected(self):
        store = TokenFamilyStore()
        tokens = self.issue(store)
        with pytest.raises(TokenExpired):
            store.rotate(
                tokens.refresh_token,
                secret=SECRET,
                role="GUARD",
                now=TOKEN_NOW + timedelta(days=31),
            )

    def test_unknown_token_is_rejected(self):
        with pytest.raises(TokenInvalid):
            TokenFamilyStore().rotate("nonsense", secret=SECRET, role="GUARD")

    def test_logout_revokes_one_session(self):
        store = TokenFamilyStore()
        tokens = self.issue(store)
        assert store.revoke(tokens.refresh_token, TOKEN_NOW) is True
        assert store.revoke(tokens.refresh_token, TOKEN_NOW) is False

    def test_purge_removes_expired_records(self):
        store = TokenFamilyStore()
        self.issue(store)
        assert store.purge_expired(TOKEN_NOW + timedelta(days=40)) == 1


class TestMediaKeysAndValidation:
    def test_key_layout_is_tenant_first_then_date(self):
        key = build_key(
            tenant_id="t1",
            site_id="s1",
            event_id="e1",
            kind=MediaKind.SNAPSHOT,
            occurred_at=NOW,
        )
        assert key == "t1/s1/2026/09/14/e1/snapshot.jpg"

    def test_clip_extension(self):
        key = build_key(
            tenant_id="t1", site_id="s1", event_id="e1", kind=MediaKind.CLIP, occurred_at=NOW
        )
        assert key.endswith("clip.mp4")

    def test_jpeg_is_validated_by_content_not_by_name(self):
        assert validate(JPEG, MediaKind.SNAPSHOT) == "image/jpeg"
        with pytest.raises(MediaRejected, match="not a JPEG"):
            validate(b"<html>error</html>" * 100, MediaKind.SNAPSHOT)

    def test_tiny_image_is_refused(self):
        with pytest.raises(MediaRejected):
            validate(b"\xff\xd8\xff" + b"\x00" * 10, MediaKind.SNAPSHOT)

    def test_mp4_validation(self):
        assert validate(MP4, MediaKind.CLIP) == "video/mp4"
        with pytest.raises(MediaRejected, match="not an MP4"):
            validate(JPEG, MediaKind.CLIP)

    def test_empty_object_is_refused(self):
        with pytest.raises(MediaRejected, match="empty"):
            validate(b"", MediaKind.SNAPSHOT)

    @pytest.mark.parametrize(("plan", "days"), list(RETENTION_DAYS.items()))
    def test_retention_by_plan(self, plan, days):
        assert expiry_for(plan, from_time=NOW) == NOW + timedelta(days=days)

    def test_unknown_plan_falls_back_to_business(self):
        assert expiry_for("mystery", from_time=NOW) == NOW + timedelta(days=30)


# ---------------------------------------------------------------------------
# Against the real MinIO in the dev stack, when it happens to be running.
# ---------------------------------------------------------------------------

MINIO_ENDPOINT = os.environ.get("S3_ENDPOINT", "http://localhost:9000")


def _minio_available() -> bool:
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(  # noqa: S310 - fixed localhost health URL
            f"{MINIO_ENDPOINT}/minio/health/live", timeout=2
        ) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError):
        return False


requires_minio = pytest.mark.skipif(
    not _minio_available(), reason="MinIO is not running (docker compose up minio)"
)


@requires_minio
class TestAgainstMinio:
    @pytest.fixture
    def store(self):
        client = build_client(
            endpoint=MINIO_ENDPOINT,
            access_key=os.environ.get("S3_ACCESS_KEY", "nightshift"),
            secret_key=os.environ.get("S3_SECRET_KEY", "nightshift-secret"),
        )
        return MediaStore(client, os.environ.get("S3_BUCKET", "nightshift-media"))

    def test_round_trip(self, store):
        stored = store.put(
            JPEG,
            tenant_id=uuid4(),
            site_id=uuid4(),
            event_id=uuid4(),
            kind=MediaKind.SNAPSHOT,
            occurred_at=NOW,
        )
        assert stored.size_bytes == len(JPEG)
        assert store.exists(stored.key)
        assert store.get(stored.key) == JPEG
        store.delete(stored.key)
        assert not store.exists(stored.key)

    def test_presigned_url_works_and_the_bare_url_does_not(self, store):
        """The bucket must be private: only the signed URL may read (spec §19.2)."""
        import urllib.error
        import urllib.request

        stored = store.put(
            JPEG,
            tenant_id=uuid4(),
            site_id=uuid4(),
            event_id=uuid4(),
            kind=MediaKind.SNAPSHOT,
            occurred_at=NOW,
        )
        try:
            signed = store.presigned_url(stored.key, ttl_seconds=60)
            with urllib.request.urlopen(signed, timeout=5) as response:  # noqa: S310
                assert response.read() == JPEG

            unsigned = f"{MINIO_ENDPOINT}/{store.bucket}/{stored.key}"
            with pytest.raises(urllib.error.HTTPError) as excinfo:
                urllib.request.urlopen(unsigned, timeout=5)  # noqa: S310
            assert excinfo.value.code in (401, 403)
        finally:
            store.delete(stored.key)

    def test_invalid_content_never_reaches_the_bucket(self, store):
        with pytest.raises(MediaRejected):
            store.put(
                b"not an image",
                tenant_id=uuid4(),
                site_id=uuid4(),
                event_id=uuid4(),
                kind=MediaKind.SNAPSHOT,
                occurred_at=NOW,
            )
