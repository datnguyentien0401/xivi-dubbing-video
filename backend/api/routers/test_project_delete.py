"""Tests for DELETE /projects/{id} — delete a project along with all its related data.

Needs a real Postgres to test the cascading delete across several real FK-linked
tables (same reason as test_project_detail.py).

Run: DATABASE_URL=postgresql+asyncpg://xivi:xivi@localhost:5433/xivi \
      python -m pytest api/routers/test_project_delete.py
"""

import os
import uuid

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import Character, DialogueLine, Job, Project, SourceVideo, User

pytestmark = pytest.mark.skipif(
    "DATABASE_URL" not in os.environ,
    reason="needs a real Postgres to test the cascading FK delete (see docstring)",
)


@pytest_asyncio.fixture
async def two_owners_with_full_project(session: AsyncSession):
    """owner_a has 1 project with a full set of video/character/job/dialogue_line;
    owner_b has a separate project — used to test that deleting project_a
    doesn't touch owner_b's data."""
    owner_a = User(firebase_uid=f"a-{uuid.uuid4()}", email=f"a-{uuid.uuid4()}@test.local")
    owner_b = User(firebase_uid=f"b-{uuid.uuid4()}", email=f"b-{uuid.uuid4()}@test.local")
    session.add_all([owner_a, owner_b])
    await session.flush()

    project_a = Project(user_id=owner_a.id, name="project-a", source_language="vi")
    project_b = Project(user_id=owner_b.id, name="project-b", source_language="vi")
    session.add_all([project_a, project_b])
    await session.flush()

    video_a = SourceVideo(project_id=project_a.id, gcs_uri="gs://bucket/a.mp4")
    video_b = SourceVideo(project_id=project_b.id, gcs_uri="gs://bucket/b.mp4")
    session.add_all([video_a, video_b])
    await session.flush()

    character_a = Character(project_id=project_a.id, name="Mèo", speaker_tag_original="speaker_1")
    session.add(character_a)
    await session.flush()

    job_a = Job(project_id=project_a.id, source_video_id=video_a.id, target_language="id")
    job_b = Job(project_id=project_b.id, source_video_id=video_b.id, target_language="id")
    session.add_all([job_a, job_b])
    await session.flush()

    line_a = DialogueLine(
        job_id=job_a.id,
        character_id=character_a.id,
        start_time=0.0,
        end_time=1.0,
        original_text="Xin chào",
    )
    session.add(line_a)
    await session.flush()

    return {
        "owner_a": owner_a,
        "owner_b": owner_b,
        "project_a": project_a,
        "project_b": project_b,
        "video_b": video_b,
        "job_b": job_b,
    }


async def test_delete_project_removes_project_and_dependents(
    session: AsyncSession, two_owners_with_full_project
):
    from api.core.firebase_auth import CurrentUser
    from api.routers import projects as projects_router

    data = two_owners_with_full_project
    owner_a = data["owner_a"]
    project_a_id = data["project_a"].id

    await projects_router.delete_project(
        project_id=project_a_id,
        user=CurrentUser(id=owner_a.id, firebase_uid=owner_a.firebase_uid, email=owner_a.email),
        session=session,
    )

    assert (await session.get(Project, project_a_id)) is None
    assert (
        await session.execute(select(SourceVideo).where(SourceVideo.project_id == project_a_id))
    ).scalar_one_or_none() is None
    assert (
        await session.execute(select(Character).where(Character.project_id == project_a_id))
    ).scalar_one_or_none() is None


async def test_delete_project_does_not_touch_other_users_data(
    session: AsyncSession, two_owners_with_full_project
):
    from api.core.firebase_auth import CurrentUser
    from api.routers import projects as projects_router

    data = two_owners_with_full_project
    owner_a = data["owner_a"]

    await projects_router.delete_project(
        project_id=data["project_a"].id,
        user=CurrentUser(id=owner_a.id, firebase_uid=owner_a.firebase_uid, email=owner_a.email),
        session=session,
    )

    assert (await session.get(Project, data["project_b"].id)) is not None
    assert (await session.get(SourceVideo, data["video_b"].id)) is not None
    assert (await session.get(Job, data["job_b"].id)) is not None


async def test_delete_project_404_for_other_users_project(
    session: AsyncSession, two_owners_with_full_project
):
    """IDOR check: owner_a must NOT be able to delete owner_b's project."""
    from api.core.firebase_auth import CurrentUser
    from api.routers import projects as projects_router

    data = two_owners_with_full_project
    owner_a = data["owner_a"]

    with pytest.raises(HTTPException) as exc_info:
        await projects_router.delete_project(
            project_id=data["project_b"].id,
            user=CurrentUser(id=owner_a.id, firebase_uid=owner_a.firebase_uid, email=owner_a.email),
            session=session,
        )
    assert exc_info.value.status_code == 404
    assert (await session.get(Project, data["project_b"].id)) is not None
