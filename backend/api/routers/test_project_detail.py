"""Tests for GET /projects/{id}/source-videos and /projects/{id}/jobs — used
by the Project Detail page (previously there was no way to view the jobs/videos
of an already-created project, you could only create a new project — see
frontend/src/pages/ProjectDetail.tsx).

Needs a real Postgres to test the ownership SQL join (same reason as test_jobs.py).

Run: DATABASE_URL=postgresql+asyncpg://xivi:xivi@localhost:5433/xivi \
      python -m pytest api/routers/test_project_detail.py
"""

import os
import uuid

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import Job, Project, SourceVideo, User

pytestmark = pytest.mark.skipif(
    "DATABASE_URL" not in os.environ,
    reason="needs a real Postgres to test the SQL join (see docstring)",
)


@pytest_asyncio.fixture
async def two_owners_with_projects(session: AsyncSession):
    """2 users, each with 1 project + 1 source video + 1 job — to test that
    list_source_videos/list_project_jobs does NOT leak another user's data."""
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

    job_a = Job(project_id=project_a.id, source_video_id=video_a.id, target_language="id")
    job_b = Job(project_id=project_b.id, source_video_id=video_b.id, target_language="id")
    session.add_all([job_a, job_b])
    await session.flush()

    return {
        "owner_a": owner_a,
        "project_a": project_a,
        "video_a": video_a,
        "job_a": job_a,
        "project_b": project_b,
    }


async def test_list_source_videos_only_returns_own_project(
    session: AsyncSession, two_owners_with_projects
):
    from api.core.firebase_auth import CurrentUser
    from api.routers import projects as projects_router

    data = two_owners_with_projects
    owner_a = data["owner_a"]

    result = await projects_router.list_source_videos(
        project_id=data["project_a"].id,
        user=CurrentUser(id=owner_a.id, firebase_uid=owner_a.firebase_uid, email=owner_a.email),
        session=session,
    )

    assert len(result) == 1
    assert result[0].id == data["video_a"].id


async def test_list_source_videos_404_for_other_users_project(
    session: AsyncSession, two_owners_with_projects
):
    """IDOR check: owner_a must NOT be able to view project_b's source videos."""
    from api.core.firebase_auth import CurrentUser
    from api.routers import projects as projects_router

    data = two_owners_with_projects
    owner_a = data["owner_a"]

    with pytest.raises(HTTPException) as exc_info:
        await projects_router.list_source_videos(
            project_id=data["project_b"].id,
            user=CurrentUser(id=owner_a.id, firebase_uid=owner_a.firebase_uid, email=owner_a.email),
            session=session,
        )
    assert exc_info.value.status_code == 404


async def test_list_project_jobs_only_returns_own_project(
    session: AsyncSession, two_owners_with_projects
):
    from api.core.firebase_auth import CurrentUser
    from api.routers import projects as projects_router

    data = two_owners_with_projects
    owner_a = data["owner_a"]

    result = await projects_router.list_project_jobs(
        project_id=data["project_a"].id,
        user=CurrentUser(id=owner_a.id, firebase_uid=owner_a.firebase_uid, email=owner_a.email),
        session=session,
    )

    assert len(result) == 1
    assert result[0].id == data["job_a"].id


async def test_list_project_jobs_404_for_other_users_project(
    session: AsyncSession, two_owners_with_projects
):
    from api.core.firebase_auth import CurrentUser
    from api.routers import projects as projects_router

    data = two_owners_with_projects
    owner_a = data["owner_a"]

    with pytest.raises(HTTPException) as exc_info:
        await projects_router.list_project_jobs(
            project_id=data["project_b"].id,
            user=CurrentUser(id=owner_a.id, firebase_uid=owner_a.firebase_uid, email=owner_a.email),
            session=session,
        )
    assert exc_info.value.status_code == 404
