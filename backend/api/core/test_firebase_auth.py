"""Tests for get_current_user — especially the auth_dev_bypass branch (local dev
while Login.tsx hasn't implemented real Google Sign-In yet). Needs a real DB
because of user auto-provisioning (same reason as test_ownership.py/test_projects.py).

Run: DATABASE_URL=postgresql+asyncpg://xivi:xivi@localhost:5433/xivi \
      python -m pytest api/core/test_firebase_auth.py
"""

import os

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.skipif(
    "DATABASE_URL" not in os.environ,
    reason="needs a real Postgres to test user auto-provisioning (see docstring)",
)


async def test_dev_bypass_off_by_default_still_requires_bearer_token(session: AsyncSession):
    from api.core.firebase_auth import get_current_user

    with pytest.raises(HTTPException) as exc_info:
        await get_current_user(authorization=None, session=session)
    assert exc_info.value.status_code == 401


async def test_dev_bypass_on_returns_fixed_user_without_any_header(
    session: AsyncSession, monkeypatch
):
    from api.core import firebase_auth

    monkeypatch.setattr(firebase_auth.settings, "auth_dev_bypass", True)

    user = await firebase_auth.get_current_user(authorization=None, session=session)

    assert user.firebase_uid == firebase_auth._DEV_BYPASS_FIREBASE_UID
    assert user.email == firebase_auth._DEV_BYPASS_EMAIL


async def test_dev_bypass_on_is_idempotent_same_user_across_calls(
    session: AsyncSession, monkeypatch
):
    from api.core import firebase_auth

    monkeypatch.setattr(firebase_auth.settings, "auth_dev_bypass", True)

    first = await firebase_auth.get_current_user(authorization=None, session=session)
    second = await firebase_auth.get_current_user(authorization=None, session=session)

    assert first.id == second.id
