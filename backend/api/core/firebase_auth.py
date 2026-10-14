"""Verifies the Firebase ID token sent with requests from the frontend."""

import asyncio

from fastapi import Depends, Header, HTTPException, status
from firebase_admin import auth as firebase_auth
from firebase_admin import initialize_app
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.core.config import settings
from api.db.models import User
from api.db.session import get_session

_app = None

# Used when settings.auth_dev_bypass=True — a fixed user for local dev, no
# real Firebase project/ADC needed. See the warning in api/core/config.py.
_DEV_BYPASS_FIREBASE_UID = "local-dev-user"
_DEV_BYPASS_EMAIL = "dev@localhost"


def _get_app():
    global _app
    if _app is None:
        _app = initialize_app(options={"projectId": settings.firebase_project_id})
    return _app


class CurrentUser:
    """`id` is the internal uuid (PK of the users table) — used as the FK when
    creating Project/…, do NOT use `firebase_uid` directly (that's an arbitrary
    string from Firebase, not a uuid, see api/db/models.py)."""

    def __init__(self, id, firebase_uid: str, email: str | None):
        self.id = id
        self.firebase_uid = firebase_uid
        self.email = email


async def _get_or_create_user(session: AsyncSession, firebase_uid: str, email: str | None) -> User:
    result = await session.execute(select(User).where(User.firebase_uid == firebase_uid))
    user = result.scalar_one_or_none()
    if user is not None:
        return user
    user = User(firebase_uid=firebase_uid, email=email or f"{firebase_uid}@unknown.local")
    session.add(user)
    try:
        await session.commit()
    except IntegrityError:
        # 2 requests racing on the same first login both try to create the
        # user — the losing request rolls back and re-reads the record the
        # other request just created, instead of surfacing a 500 to the user.
        await session.rollback()
        result = await session.execute(select(User).where(User.firebase_uid == firebase_uid))
        return result.scalar_one()
    await session.refresh(user)
    return user


async def get_current_user(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> CurrentUser:
    """Dependency used by every endpoint that requires login. Automatically
    creates a `users` record on the first Firebase login (auto-provisioning).

    Usage:
        @router.get("/projects")
        async def list_projects(user: CurrentUser = Depends(get_current_user)): ...
    """
    if settings.auth_dev_bypass:
        # Local dev has no real Firebase project/ADC — skip token verification,
        # always use 1 fixed user. The Firebase Admin SDK is not touched on this branch.
        user = await _get_or_create_user(session, _DEV_BYPASS_FIREBASE_UID, _DEV_BYPASS_EMAIL)
        return CurrentUser(id=user.id, firebase_uid=user.firebase_uid, email=user.email)

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    token = authorization.removeprefix("Bearer ")
    try:
        _get_app()
        # firebase_admin.auth is a sync library (network call) — run it in a
        # thread pool so it doesn't block the event loop (see backend AGENTS.md).
        decoded = await asyncio.to_thread(firebase_auth.verify_id_token, token)
    except Exception as exc:  # noqa: BLE001 - intentionally catch every verify error as 401
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token") from exc

    firebase_uid = decoded["uid"]
    email = decoded.get("email")
    user = await _get_or_create_user(session, firebase_uid, email)
    return CurrentUser(id=user.id, firebase_uid=firebase_uid, email=user.email)
