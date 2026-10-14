"""Tests for api/core/ownership.py — regression protection for the fixed IDOR
vulnerability (session.get() directly, with no ownership check). Needs
DATABASE_URL pointing at a real Postgres (not mocked) because the logic here is
a SQL join, and mocking wouldn't catch a wrong-column/wrong-condition join bug.

Run: DATABASE_URL=postgresql+asyncpg://xivi:xivi@localhost:5433/xivi \
      python -m pytest api/core/test_ownership.py
"""

import os
import uuid

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from api.core.ownership import (
    get_owned_character,
    get_owned_dialogue_line,
    get_owned_job,
    get_owned_project,
    get_owned_source_video,
)
from api.db.models import Character, DialogueLine, Job, Project, SourceVideo, User

pytestmark = pytest.mark.skipif(
    "DATABASE_URL" not in os.environ,
    reason="needs a real Postgres to test the SQL join (see docstring)",
)


@pytest_asyncio.fixture
async def two_users_with_resources(session: AsyncSession):
    """Creates 2 users, each with 1 project -> source_video -> job -> dialogue_line,
    and 1 character — to test that the owner can access it while another user cannot."""
    owner = User(firebase_uid=f"owner-{uuid.uuid4()}", email=f"owner-{uuid.uuid4()}@test.local")
    stranger = User(
        firebase_uid=f"stranger-{uuid.uuid4()}", email=f"stranger-{uuid.uuid4()}@test.local"
    )
    session.add_all([owner, stranger])
    await session.flush()

    project = Project(user_id=owner.id, name="p", source_language="vi")
    session.add(project)
    await session.flush()

    video = SourceVideo(project_id=project.id, gcs_uri="gs://b/v.mp4")
    character = Character(project_id=project.id, name="Cat", speaker_tag_original="A")
    session.add_all([video, character])
    await session.flush()

    job = Job(project_id=project.id, source_video_id=video.id, target_language="id")
    session.add(job)
    await session.flush()

    line = DialogueLine(job_id=job.id, start_time=0.0, end_time=1.0, original_text="hi")
    session.add(line)
    await session.flush()

    return {
        "owner_id": owner.id,
        "stranger_id": stranger.id,
        "project_id": project.id,
        "source_video_id": video.id,
        "job_id": job.id,
        "line_id": line.id,
        "character_id": character.id,
    }


async def test_owner_can_access_own_resources(session, two_users_with_resources):
    r = two_users_with_resources
    assert (await get_owned_project(session, r["project_id"], r["owner_id"])).id == r["project_id"]
    assert (await get_owned_source_video(session, r["source_video_id"], r["owner_id"])).id == r[
        "source_video_id"
    ]
    assert (await get_owned_job(session, r["job_id"], r["owner_id"])).id == r["job_id"]
    assert (await get_owned_dialogue_line(session, r["line_id"], r["owner_id"])).id == r["line_id"]
    assert (await get_owned_character(session, r["character_id"], r["owner_id"])).id == r[
        "character_id"
    ]


async def test_stranger_gets_404_not_other_users_resources(session, two_users_with_resources):
    r = two_users_with_resources
    for coro in (
        get_owned_project(session, r["project_id"], r["stranger_id"]),
        get_owned_source_video(session, r["source_video_id"], r["stranger_id"]),
        get_owned_job(session, r["job_id"], r["stranger_id"]),
        get_owned_dialogue_line(session, r["line_id"], r["stranger_id"]),
        get_owned_character(session, r["character_id"], r["stranger_id"]),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await coro
        assert exc_info.value.status_code == 404


async def test_nonexistent_resource_gets_404(session):
    random_id = uuid.uuid4()
    with pytest.raises(HTTPException) as exc_info:
        await get_owned_job(session, random_id, random_id)
    assert exc_info.value.status_code == 404
