"""Auth primitives (spec §31)."""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.core.security import (
    create_access_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.core.tenancy import TenantContext, TenantIsolationError

SECRET = "test-secret-at-least-32-bytes-long!!"


def test_password_hash_is_argon2_and_verifies():
    digest = hash_password("correct horse battery staple")
    assert digest.startswith("$argon2")
    assert verify_password("correct horse battery staple", digest)
    assert not verify_password("wrong", digest)


def test_password_hashes_are_salted():
    assert hash_password("same") != hash_password("same")


def test_access_token_round_trip():
    token = create_access_token("user-1", "tenant-1", SECRET, role="GUARD")
    claims = decode_token(token, SECRET)
    assert claims["sub"] == "user-1"
    assert claims["tid"] == "tenant-1"
    assert claims["role"] == "GUARD"
    assert claims["typ"] == "access"


def test_token_signed_with_another_secret_is_rejected():
    import jwt

    token = create_access_token("user-1", "tenant-1", SECRET)
    with pytest.raises(jwt.InvalidSignatureError):
        decode_token(token, "another-secret-at-least-32-bytes-!!")


def test_cross_tenant_access_is_refused():
    context = TenantContext(uuid4(), uuid4(), "ADMIN", frozenset({"sites.view"}))
    context.assert_owns(context.tenant_id)
    with pytest.raises(TenantIsolationError):
        context.assert_owns(uuid4())


def test_missing_permission_is_refused():
    context = TenantContext(uuid4(), uuid4(), "GUARD", frozenset({"alarms.view"}))
    context.require("alarms.view")
    with pytest.raises(PermissionError):
        context.require("rules.manage")
